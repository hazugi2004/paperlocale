"""Provider 的最小公共合同和结构化输出解析。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..contracts import (
    FORMULA_RE,
    match_glossary,
    protected_counts,
    scientific_literal_spans,
    scientific_quantities,
)
from ..domains import DomainPack


@dataclass(frozen=True)
class Segment:
    """交给 Provider 的稳定片段。"""

    id: str
    source: str


@dataclass(frozen=True)
class Translation:
    """Provider 返回的单条译文。"""

    id: str
    target: str


@dataclass(frozen=True)
class TranslationContext:
    """一次翻译请求共享的语言和领域约束。"""

    source_language: str
    target_language: str
    domain: DomainPack
    reference_policy: str = "preserve"
    reference_segment_ids: frozenset[str] = frozenset()
    # 首轮候选未通过内容合同时，流水线只把失败片段、上一版候选和具体错误
    # 交还同一 Provider。映射为空表示普通首轮翻译，不改变既有调用方。
    repair_feedback: Mapping[str, tuple[str, tuple[str, ...]]] = field(
        default_factory=dict
    )
    # 源版面中的占位符仍写回原对象。提供原文含义及标点，避免模型把
    # 已藏在锚点内的右括号再补一次；普通流水线默认没有此映射。
    anchor_text: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


class TranslationProvider(ABC):
    """所有真实 Provider 必须实现的同步批量接口。"""

    # 大多数聊天模型可接收多个结构化片段；专用翻译模型可覆盖
    # 声明更严格的单次段数或源文字符数上限，由公共流水线统一切批。
    max_batch_segments: int | None = None
    max_batch_characters: int | None = None

    def provenance(self) -> dict[str, object]:
        """返回可写入运行清单的非敏感 Provider 身份。

        第三方 Provider 即使尚未实现专用元数据，也会留下明确的 Python 类型；
        内置 Provider 会覆盖此方法并记录稳定名称、模型和运行时版本。
        """

        provider_type = type(self)
        return {
            "provider": f"{provider_type.__module__}.{provider_type.__qualname__}",
            "model": None,
        }

    @abstractmethod
    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        """翻译一个非空批次，并返回相同 ID 集合。"""


def output_schema() -> dict[str, Any]:
    """Codex 等支持 JSON Schema 的 Provider 共用的严格结构。"""

    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "target": {"type": "string"},
                    },
                    "required": ["id", "target"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    }


def build_prompt(segments: list[Segment], context: TranslationContext) -> str:
    """把领域提示、固定术语和待译 JSON 合并成可审计的单一提示。"""

    if not segments:
        raise ValueError("Provider 批次不能为空")
    payload: list[dict[str, object]] = []
    for segment in segments:
        is_reference = segment.id in context.reference_segment_ids
        counts = protected_counts(segment.source, domain=None if is_reference else context.domain)
        item: dict[str, object] = {
            "id": segment.id,
            "kind": (
                "reference"
                if is_reference
                else "body"
            ),
            "source": segment.source,
            # 精确列出当前片段必须保留的标记及次数，避免模型把 US、NDVI、
            # 图号或带连字符变量自然改写后再被门禁稳定拒绝。
            "must_preserve": {
                category: dict(values)
                for category, values in counts.items()
                if values
            },
        }
        # 逐段传实际命中的词形。放在所属片段内，避免大词表挤占请求上下文，
        # 或将某一段的多义推荐译名误套到其他段落和书目信息中。
        if not is_reference:
            item["terminology"] = [
                {"source": surface, "target": entry.target, "required": entry.required,
                 **({"note": entry.note} if entry.note else {})}
                for surface, entry in match_glossary(segment.source, context.domain)
            ]
        anchors = context.anchor_text.get(segment.id)
        item["scientific_literals"] = [segment.source[a:b] for a,b in scientific_literal_spans(segment.source, anchor_text=anchors)]
        quantities = scientific_quantities(segment.source)
        if quantities:
            item["scientific_quantities"] = [
                {
                    "source": segment.source[q.start:q.end],
                    "signature": q.signature,
                    **({"denominator_for_review": q.unknown_denominator} if q.unknown_denominator else {}),
                }
                for q in quantities
            ]
        feedback = context.repair_feedback.get(segment.id)
        if anchors is not None:
            # 锚点字典描述各标记的原 PDF 内容。顺序和重复次数按源文实际出现位置提供。
            item['fixed_anchor_text'] = dict(anchors)
            item['fixed_anchor_order'] = FORMULA_RE.findall(segment.source)
        if feedback is not None:
            previous_target, errors = feedback
            item["previous_target"] = previous_target
            item["validation_errors"] = list(errors)
        payload.append(item)
    reference_instruction = ""
    if context.reference_policy == "translate-titles":
        reference_instruction = """
5. 对 kind=reference 的条目，只把作品标题译为目标语言；作者、年份、期刊或出版社、
   卷期页码、DOI、URL 和其他书目信息必须保持原样，不套用正文固定术语。
"""
    repair_instruction = ""
    if context.repair_feedback:
        repair_instruction = """
6. 本批是合同修复重试。逐条依据 validation_errors 修正 previous_target；
   must_preserve 中每个表面形式必须至少保留指定次数，不要省略重复图号、
   变量、单位或引文。若领域术语缺失，在 source 找到该术语对应的正文或括号释义，
   将 previous_target 该处的英文或异译改为 terminology 指定的译名，保留相关缩写与定义。
   若锚点顺序失败，以 source 为准重写有反序的分句，改写普通连接语使顺序合规，
   维持各锚点的修饰归属，不能只搬动占位符而把限定挂到另一对象。
   若仪器被前置，按固定锚点说明用两个分句恢复被测量与仪器的顺序。
   若把变量写到了对应数值之前，按原句重写为先给出数值，再说明对应变量。
   若还原锚点后括号不配对，逐一代回 fixed_anchor_text 检查 source 中的配对位置，
   在相应的普通正文恢复缺少的半边括号，只删除锚点已经提供的重复半边。
   保留与错误无关的合格分句；修完后重新核对全段的括号与锚点顺序。
   若一个标记被重复，后续从句可用指代同一概念的词，不得再复制标记或改变对象。
   仍需返回完整 target，不能只返回差异或解释。
"""
    anchor_instruction = ("fixed_anchor_text 说明占位符在 PDF 中原样显示的内容（含括号）。占位符前后的信息须保持对应，不能把数值、单位或整句移过固定锚点来改变所属位置。"
                          "fixed_anchor_order 是 target 必须逐项保留的完整序列；相邻锚点也不得因中文语序交换。"
                          "原文先出现带锚点的被测量、后出现带锚点的仪器时，用两个分句：先写完整被测量，再说明所用仪器。"
                          "例如“测量了该通量，所用仪器为……”。"
                          "嵌套在评估或检验的长句中时，后半句用普通名词复指同一量，例如“检验了通量的差异（这些通量由相应仪器测得），并评估了另一指标”。"
                          "原文先给出数值再指明变量时，保留完整数值在前，例如“取值为……（对应变量……）”。"
                          "保持仪器与该量的关联，仪器型号的文字前缀与锚点仍相连。输出前按段核对占位符实际次序。"
                          "target 仍保留占位符，锚点的文字与标点由占位符提供。"
                          "若原文的括号一边在锚点内、另一边在普通正文，保留正文中的配对半边，并让它仍包围同一内容。"
                          "例如锚点自带右括号时写“（见模型{vN}”，锚点自带左括号时写“{vN}，即相应测量值）”；省去的只是锚点已提供的半边。"
                          "原文跨段的括号按原样保留，输出前将各锚点代回检查配对。\n"
                          if context.anchor_text else "")
    return f"""{context.domain.prompt}

硬性输出合同：
1. 每个输入 ID 必须且只能返回一次，ID 原样保留。
2. 原样、原次数、原顺序保留所有 {{vN}} 公式占位符和 <style id='N'>...</style> 标签。
3. 保留所有数字、正负号、单位、变量缩写、数据集名、URL、DOI 和引文标记。
   scientific_quantities 中数值与单位必须成对保留；允许km/千米、m/s/m s−1等
   等价表示，此规则优先于单位的表面形式要求；不得丢单位或进行单位倍率/温度换算。
   统计分母的限定语须完整翻译；例如 event-occurring summer 指发生过事件的夏季。
   如有 denominator_for_review，须保留该原文分母短语，便于复核统计口径。
   scientific_literals 中的小数点、变量及运算符必须原样保留，只允许排版空白变化。
4. 逐句完整翻译，不得用省略号、空缺或半句话代替原文；源文自身的异常标点需保留。
   每个输入段落输出一个连续自然段，不插入段内换行、缩进或汉字之间的空格。
   只返回符合约定结构的 JSON，不添加解释、Markdown 或原文之外的信息。
{anchor_instruction}{reference_instruction}
{repair_instruction}

每段 terminology 仅列原文命中的术语。required=true 时，即使术语出现在括号释义或较长词组中，也须使用给定译名。
原文同时提供缩写及其普通术语释义时，保留缩写，并把释义完整译为目标语言。
required=false 时参考推荐译名及备注，按原句语境消歧。保留源文限定语与缩写。

源语言：{context.source_language}
目标语言：{context.target_language}
待翻译 JSON：
{json.dumps(payload, ensure_ascii=False)}
"""


def parse_payload(payload: object, expected: list[Segment]) -> list[Translation]:
    """拒绝缺 ID、重复 ID、额外 ID 和非字符串译文。"""

    if not isinstance(payload, dict) or not isinstance(payload.get("translations"), list):
        raise ValueError("Provider 输出缺少 translations 数组")
    received: dict[str, str] = {}
    for item in payload["translations"]:
        if not isinstance(item, dict):
            raise ValueError("translations 的成员必须是对象")
        sid = item.get("id")
        target = item.get("target")
        if not isinstance(sid, str) or not sid or not isinstance(target, str):
            raise ValueError("Provider 输出包含非法 id 或 target")
        if sid in received:
            raise ValueError(f"Provider 输出重复 ID：{sid}")
        received[sid] = target

    expected_ids = [segment.id for segment in expected]
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("输入批次包含重复 ID")
    if set(received) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(received))
        unexpected = sorted(set(received) - set(expected_ids))
        raise ValueError(f"Provider ID 集合不闭合：missing={missing}, unexpected={unexpected}")
    return [Translation(id=sid, target=received[sid]) for sid in expected_ids]
