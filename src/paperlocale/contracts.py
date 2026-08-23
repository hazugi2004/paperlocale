"""翻译片段的稳定身份与科学信息完整性门禁。

PDF 版面引擎通常把公式和富文本替换为占位符后交给翻译器。如果模型删除
占位符而流水线继续渲染，最终 PDF 会直接缺公式。本模块把这类风险提升为
硬错误，并同时保护数字、单位、缩写、URL、DOI 和领域术语。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from .domains import DomainPack

FORMULA_RE = re.compile(r"\{v\d+\}")
STYLE_RE = re.compile(r"<style\s+id=['\"]\d+['\"]>|</style>", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s)\]}>,;，。；：！？）】》]+", re.IGNORECASE)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)\]}>,;，。；：！？）】》]+", re.IGNORECASE)
# PDF 文本层常把引文号和产品版本紧贴在英文名后，如
# ``productivity3,17,18`` 和 ``NDVI3g``。第二个零宽分支允许数字紧跟
# ASCII 字母；正负号仍只能由第一分支接管，因此 ``mid-1960s``
# 中的连字符不会被误判为负号。
NUMBER_RE = re.compile(
    r"(?:(?<![\dA-Za-z])[-+−]?|(?<=[A-Za-z]))"
    r"\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?"
)
ABBREVIATION_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z][A-Z0-9-]{1,})(?![A-Za-z0-9])")
ABBREVIATION_EXCLUSIONS = frozenset({"ABSTRACT", "KEYWORDS", "REFERENCES"})
UNIT_RE = re.compile(
    r"(?<![A-Za-z])(?:%|°[CF]?|mm|cm|m|km|Pa|hPa|K|W\s*m-?2|"
    r"g\s*C\s*m-?2(?:\s*d-?1)?|µmol\s*m-?2\s*s-?1)(?![A-Za-z])",
    re.IGNORECASE,
)
CJK_RE = re.compile(r"[\u3400-\u9fff]")
ENGLISH_RE = re.compile(r"[A-Za-z]")
TRAILING_PUNCTUATION = ".,;:!?。，、；：！？"


def normalize_text(text: str) -> str:
    """统一换行和外围空白，避免平台差异改变片段身份。"""

    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def segment_id(text: str) -> str:
    """用规范化原文生成稳定 SHA-256；相同片段可安全命中断点译文。"""

    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _clean_identifiers(values: list[str]) -> Counter[str]:
    """去除 URL/DOI 末尾普通句末标点，允许中英文句号自然替换。"""

    return Counter(value.rstrip(TRAILING_PUNCTUATION) for value in values)


def protected_counts(text: str) -> dict[str, Counter[str]]:
    """提取必须保留的标记及出现次数。"""

    abbreviations = (
        value for value in ABBREVIATION_RE.findall(text)
        if value not in ABBREVIATION_EXCLUSIONS
    )
    return {
        "formula": Counter(FORMULA_RE.findall(text)),
        "style": Counter(STYLE_RE.findall(text)),
        "url": _clean_identifiers(URL_RE.findall(text)),
        "doi": _clean_identifiers(DOI_RE.findall(text)),
        "number": Counter(NUMBER_RE.findall(text)),
        "abbreviation": Counter(abbreviations),
        "unit": Counter(match.group(0) for match in UNIT_RE.finditer(text)),
    }


def validate_translation(
    source: str,
    target: str,
    domain: DomainPack | None = None,
    *,
    require_cjk: bool = True,
) -> list[str]:
    """返回全部合同错误；空列表才允许译文进入渲染阶段。"""

    errors: list[str] = []
    if not target.strip():
        return ["译文为空"]

    source_counts = protected_counts(source)
    target_counts = protected_counts(target)
    for category in ("formula", "style", "url", "doi"):
        if source_counts[category] != target_counts[category]:
            errors.append(
                f"{category} 标记不一致：期望 {dict(source_counts[category])!r}，"
                f"实际 {dict(target_counts[category])!r}"
            )

    # 数字、缩写和单位允许译文新增，但不允许丢失原文已有项目。
    for category in ("number", "abbreviation", "unit"):
        missing = source_counts[category] - target_counts[category]
        if missing:
            errors.append(f"{category} 标记缺失：{dict(missing)!r}")

    if STYLE_RE.findall(source) != STYLE_RE.findall(target):
        errors.append("style 标签顺序改变")

    if require_cjk and len(ENGLISH_RE.findall(source)) >= 40 and not CJK_RE.search(target):
        errors.append("长正文片段缺少中文译文")

    if domain is not None:
        source_folded = source.casefold()
        target_folded = target.casefold()
        for entry in domain.glossary:
            if entry.required and entry.source.casefold() in source_folded:
                if entry.target.casefold() not in target_folded:
                    errors.append(
                        f"领域术语缺失：{entry.source!r} 必须译为包含 {entry.target!r}"
                    )
    return errors


def read_jsonl(path: Path) -> list[dict[str, object]]:
    """严格读取 JSONL；坏行必须阻止后续阶段。"""

    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是合法 JSONL：{exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number} 每行必须是 JSON 对象")
            rows.append(row)
    return rows


def write_jsonl_atomic(path: Path, rows: list[dict[str, object]]) -> None:
    """同目录临时写入后原子替换，避免中断留下半份译文。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def validate_translation_files(
    segments_path: Path,
    translations_path: Path,
    domain: DomainPack | None = None,
    *,
    reference_segment_ids: set[str] | frozenset[str] = frozenset(),
    reference_policy: str = "preserve",
    passthrough_segment_ids: set[str] | frozenset[str] = frozenset(),
) -> None:
    """核对两个 JSONL 的身份闭合和逐片段内容合同。"""

    if reference_policy not in {"preserve", "translate-titles"}:
        raise ValueError(f"参考文献策略非法：{reference_policy}")
    segments = read_jsonl(segments_path)
    translations = read_jsonl(translations_path)
    expected = {str(row.get("id")): row for row in segments}
    actual = {str(row.get("id")): row for row in translations}
    if len(expected) != len(segments) or len(actual) != len(translations):
        raise ValueError("片段或译文包含重复 ID")
    if set(expected) != set(actual):
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        raise ValueError(f"ID 集合不闭合：missing={missing}, unexpected={unexpected}")
    unknown_passthrough_ids = set(passthrough_segment_ids) - set(expected)
    if unknown_passthrough_ids:
        raise ValueError(f"透传映射包含未知片段 ID：{sorted(unknown_passthrough_ids)}")
    overlap = set(reference_segment_ids) & set(passthrough_segment_ids)
    if overlap:
        raise ValueError(f"参考文献与透传映射不能包含相同片段：{sorted(overlap)}")

    failures: dict[str, list[str]] = {}
    for sid, source_row in expected.items():
        source = str(source_row.get("source", ""))
        if sid != segment_id(source):
            raise ValueError(f"片段 ID 与规范化原文不一致：{sid}")
        target_row = actual[sid]
        if str(target_row.get("source", "")) != source:
            raise ValueError(f"译文记录的 source 与片段原文不一致：{sid}")
        target = str(target_row.get("target", ""))
        if sid in passthrough_segment_ids:
            errors = [] if target == source else ["人工透传片段必须与原文完全相同"]
        elif sid in reference_segment_ids and reference_policy == "preserve":
            errors = [] if target == source else ["preserve 策略要求参考文献原样保留"]
        elif sid in reference_segment_ids:
            errors = validate_translation(source, target, None)
        else:
            errors = validate_translation(source, target, domain)
        if errors:
            failures[sid] = errors
    if failures:
        raise ValueError(f"存在 {len(failures)} 个未通过门禁的片段：{failures!r}")
