"""Provider 的最小公共合同和结构化输出解析。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

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


class TranslationProvider(ABC):
    """所有真实 Provider 必须实现的同步批量接口。"""

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
    glossary = "\n".join(
        f"- {entry.source} => {entry.target}"
        for entry in context.domain.glossary
    )
    payload = [
        {
            "id": segment.id,
            "kind": (
                "reference"
                if segment.id in context.reference_segment_ids
                else "body"
            ),
            "source": segment.source,
        }
        for segment in segments
    ]
    reference_instruction = ""
    if context.reference_policy == "translate-titles":
        reference_instruction = """
5. 对 kind=reference 的条目，只把作品标题译为目标语言；作者、年份、期刊或出版社、
   卷期页码、DOI、URL 和其他书目信息必须保持原样，不套用正文固定术语。
"""
    return f"""{context.domain.prompt}

硬性输出合同：
1. 每个输入 ID 必须且只能返回一次，ID 原样保留。
2. 原样、原次数、原顺序保留所有 {{vN}} 公式占位符和 <style id='N'>...</style> 标签。
3. 保留所有数字、正负号、单位、变量缩写、数据集名、URL、DOI 和引文标记。
4. 只返回符合约定结构的 JSON，不添加解释、Markdown 或原文之外的信息。
{reference_instruction}

固定术语（仅适用于 kind=body）：
{glossary}

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
