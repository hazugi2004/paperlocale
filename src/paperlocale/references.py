"""参考文献片段的确定性发现、人工确认与运行绑定。

CLITranslator 返回的片段没有页码，而且顺序不等于 PDF 阅读顺序。本模块只把
能够完整落入源 PDF 参考文献区域的长片段自动标记；其余片段必须由用户查看
本地复核清单后显式确认，不能依赖作者年份或 DOI 密度等猜测性规则。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pymupdf as fitz

from .contracts import read_jsonl, write_jsonl_atomic

REFERENCE_POLICIES = ("preserve", "translate-titles")
# 投稿手稿常把章节号和边栏行号并入同一 PDF 文本块，例如
# ``6 Reference\n353``。只允许标题词前后各一个整数，并坚持 fullmatch，
# 避免把正文里的 ``see Reference 353`` 或书目内容误判为区域标题。
REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:\d+\s+)?REFERENCES?(?:\s+\d+)?\s*$",
    re.IGNORECASE,
)
# 参考文献之后可能还有独立的图表章节。只把“章节号 + 简短英文标题”的完整
# 文本块视为下一个章节边界；年份、页码和普通参考文献条目都不满足该结构。
NUMBERED_SECTION_HEADING_RE = re.compile(
    r"^\s*\d+\s+[A-Za-z][A-Za-z ]{0,80}\s*$",
    re.IGNORECASE,
)
MINIMUM_EXACT_MATCH_CHARACTERS = 80
MINIMUM_IN_ORDER_EXACT_COVERAGE = 0.99
FORMULA_PLACEHOLDER_RE = re.compile(r"\{v\d+\}", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_match_text(
    text: str,
    *,
    ignored_tokens: tuple[str, ...] = (),
) -> str:
    """为确定性区域比对删除排版差异，不改其余字母数字顺序。

    CLITranslator 用 ``{vN}`` 代替源 PDF 中的公式或富文本对象；投稿手稿还会
    把左侧行号混入片段。两者都由当前 PDF 本身确定，因此可从区域和候选中
    同步移除，而不使用作者、年份或 DOI 等内容猜测。
    """

    normalized = re.sub(
        r"[^a-z0-9]+",
        "",
        FORMULA_PLACEHOLDER_RE.sub("", text).casefold(),
    )
    for token in ignored_tokens:
        normalized = normalized.replace(token.casefold(), "")
    return normalized


def _in_order_exact_coverage(candidate: str, region: str) -> float:
    """返回候选中按原顺序落入区域的精确字符比例。"""

    if not candidate or not region:
        return 0.0
    matcher = difflib.SequenceMatcher(None, candidate, region, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(candidate)


def _block_without_line_numbers(text: str, line_numbers: list[str]) -> str:
    """删除由页面坐标确认的边栏行号，并保留其余文本顺序。"""

    known_line_numbers = set(line_numbers)
    return " ".join(
        line.strip()
        for line in text.splitlines()
        if line.strip() and line.strip() not in known_line_numbers
    )


def _heading_block_matches(text: str, line_numbers: list[str]) -> bool:
    """判断文本块是否仅由参考文献标题和已定位的边栏行号组成。

    PyMuPDF 对同一视觉行的抽取顺序并不固定：标题块可能是
    ``6 Reference\n353``，也可能是 ``353\n6 Reference``。这里只删除已经由
    页面左侧坐标确认的纯数字行，再对剩余完整文本应用严格标题正则；正文中的
    年份或编号不会因为内容相同而被忽略。
    """

    cleaned = _block_without_line_numbers(text, line_numbers)
    return REFERENCE_HEADING_RE.fullmatch(cleaned) is not None


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _reference_region(
    source_pdf: Path,
) -> tuple[list[int], str | None, tuple[str, ...]]:
    """返回标题页、标题后区域和由 PDF 坐标确定的左侧稿件行号。

    参考文献标题可能与整页书目被 PyMuPDF 合并为一个文本块，
    因此识别和区域收集都以可见文本行为单位，不依赖块边界。
    """

    document = fitz.open(source_pdf)
    headings: list[tuple[int, float]] = []
    page_text_lines: list[list[tuple[float, float, float, float, str]]] = []
    page_line_entries: list[list[tuple[float, float, str]]] = []
    try:
        for page_index, page in enumerate(document):
            text_lines: list[tuple[float, float, float, float, str]] = []
            line_entries: list[tuple[float, float, str]] = []
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    bbox = line["bbox"]
                    content_parts: list[str] = []
                    for span in line.get("spans", []):
                        span_text = str(span.get("text", ""))
                        span_bbox = span.get("bbox", bbox)
                        # 同一视觉行内也可能同时包含左侧行号和正文。
                        # 依据 span 坐标分离行号，避免依赖提取器是否换行。
                        if (
                            span_text.strip().isdigit()
                            and float(span_bbox[2]) <= float(page.rect.width) * 0.12
                        ):
                            line_entries.append(
                                (
                                    float(span_bbox[1]),
                                    float(span_bbox[3]),
                                    span_text.strip(),
                                )
                            )
                            continue
                        content_parts.append(span_text)
                    line_text = "".join(content_parts).strip()
                    if not line_text:
                        continue
                    text_lines.append(
                        (
                            float(bbox[0]),
                            float(bbox[1]),
                            float(bbox[2]),
                            float(bbox[3]),
                            line_text,
                        )
                    )
            page_text_lines.append(text_lines)
            page_line_entries.append(line_entries)
            line_numbers = [entry[2] for entry in line_entries]
            for line in text_lines:
                if _heading_block_matches(line[4], line_numbers):
                    headings.append((page_index, line[3]))
    finally:
        document.close()

    heading_pages = [page_index + 1 for page_index, _bottom in headings]
    if len(headings) != 1:
        return heading_pages, None, ()

    heading_page, heading_bottom = headings[0]
    region_parts: list[str] = []
    region_boundary: tuple[int, float] | None = None
    for page_index in range(heading_page, len(page_text_lines)):
        for line in page_text_lines[page_index]:
            if page_index == heading_page and line[1] < heading_bottom:
                continue
            cleaned = _block_without_line_numbers(
                line[4],
                [entry[2] for entry in page_line_entries[page_index]],
            )
            # 参考文献必须止于下一个编号章节，不能把其后的 Figure 或 Table
            # 章节误标为参考文献。标题自身已经在上方排除，不会触发此边界。
            if NUMBERED_SECTION_HEADING_RE.fullmatch(cleaned):
                region_boundary = (page_index, line[1])
                break
            region_parts.append(line[4])
        if region_boundary is not None:
            break

    last_region_page = (
        region_boundary[0] if region_boundary is not None else len(page_text_lines) - 1
    )
    boundary_top = region_boundary[1] if region_boundary is not None else None
    line_numbers = tuple(
        text
        for page_index in range(heading_page, last_region_page + 1)
        for _top, bottom, text in page_line_entries[page_index]
        # 边界页只纳入下一个章节标题上方的行号，避免 Figure 章节的行号
        # 参与候选归一化；其余参考文献页全部纳入。
        if boundary_top is None
        or page_index < last_region_page
        or bottom <= boundary_top
    )
    return heading_pages, "\n".join(region_parts), line_numbers


def prepare_reference_review(
    *,
    source_pdf: Path,
    source_sha256: str,
    segments_path: Path,
    output_dir: Path,
) -> dict[str, object]:
    """生成全片段复核清单和只含确定性结果的摘要。"""

    source = source_pdf.expanduser().resolve()
    segments = segments_path.expanduser().resolve()
    root = output_dir.expanduser().resolve()
    if _sha256(source) != source_sha256:
        raise ValueError("源 PDF 哈希与运行清单不一致")

    rows = read_jsonl(segments)
    heading_pages, region_text, manuscript_line_numbers = _reference_region(source)
    normalized_region = (
        _normalized_match_text(
            region_text,
            ignored_tokens=manuscript_line_numbers,
        )
        if region_text is not None
        else ""
    )
    review_rows: list[dict[str, object]] = []
    automatic_ids: list[str] = []
    automatic_characters = 0
    for index, row in enumerate(rows, 1):
        segment_id = str(row.get("id", ""))
        source_text = str(row.get("source", ""))
        normalized = _normalized_match_text(
            source_text,
            ignored_tokens=manuscript_line_numbers,
        )
        heading_match = REFERENCE_HEADING_RE.fullmatch(source_text) is not None
        exact_coverage = _in_order_exact_coverage(normalized, normalized_region)
        region_match = (
            len(normalized) >= MINIMUM_EXACT_MATCH_CHARACTERS
            and bool(normalized_region)
            and exact_coverage >= MINIMUM_IN_ORDER_EXACT_COVERAGE
        )
        automatic = heading_match or region_match
        if automatic:
            automatic_ids.append(segment_id)
            if region_match:
                automatic_characters += round(len(normalized) * exact_coverage)
        review_rows.append(
            {
                "index": index,
                "id": segment_id,
                "source": source_text,
                "automatic_exact_match": automatic,
                "in_order_exact_coverage": round(exact_coverage, 6),
            }
        )

    review_path = root / "reference_review.jsonl"
    summary_path = root / "reference_review_summary.json"
    write_jsonl_atomic(review_path, review_rows)
    summary: dict[str, object] = {
        "schema_version": 1,
        "created_at": _utc_now(),
        "source_sha256": source_sha256,
        "segments_sha256": _sha256(segments),
        "heading_pages": heading_pages,
        "automatic_region_available": region_text is not None,
        "manuscript_line_number_count": len(manuscript_line_numbers),
        "minimum_in_order_exact_coverage": MINIMUM_IN_ORDER_EXACT_COVERAGE,
        "reference_region_sha256": (
            hashlib.sha256(normalized_region.encode("utf-8")).hexdigest()
            if normalized_region
            else None
        ),
        "automatic_reference_segment_ids": automatic_ids,
        "automatic_reference_character_coverage": (
            round(min(automatic_characters / len(normalized_region), 1.0), 6)
            if normalized_region
            else 0.0
        ),
        "review_jsonl": str(review_path),
    }
    _write_json_atomic(summary_path, summary)
    return summary


def confirm_reference_review(
    *,
    source_pdf: Path,
    source_sha256: str,
    segments_path: Path,
    output_dir: Path,
    additional_segment_ids: list[str],
    confirmed_by: str,
) -> dict[str, object]:
    """把自动结果与用户补充 ID 合并为绑定当前输入字节的参考文献映射。"""

    if not confirmed_by.strip():
        raise ValueError("confirmed_by 不能为空")
    summary = prepare_reference_review(
        source_pdf=source_pdf,
        source_sha256=source_sha256,
        segments_path=segments_path,
        output_dir=output_dir,
    )
    rows = read_jsonl(segments_path)
    known_ids = {str(row.get("id", "")) for row in rows}
    requested = set(additional_segment_ids)
    unknown = sorted(requested - known_ids)
    if unknown:
        raise ValueError(f"参考文献确认包含未知片段 ID：{unknown}")
    automatic = {
        str(segment_id)
        for segment_id in summary["automatic_reference_segment_ids"]
    }
    selected = [
        str(row["id"])
        for row in rows
        if str(row["id"]) in automatic | requested
    ]
    mapping: dict[str, object] = {
        "schema_version": 1,
        "confirmed_at": _utc_now(),
        "confirmed_by": confirmed_by.strip(),
        "source_sha256": source_sha256,
        "segments_sha256": summary["segments_sha256"],
        "reference_segment_ids": selected,
        "automatic_reference_segment_ids": list(
            summary["automatic_reference_segment_ids"]
        ),
    }
    _write_json_atomic(output_dir / "reference_map.json", mapping)
    return mapping


def load_reference_map(
    *,
    source_sha256: str,
    segments_path: Path,
    map_path: Path,
) -> dict[str, object]:
    """读取并核对人工映射仍绑定当前源 PDF 和当前片段文件。"""

    if not map_path.is_file():
        raise FileNotFoundError(f"参考文献映射不存在：{map_path}")
    mapping = json.loads(map_path.read_text(encoding="utf-8"))
    if mapping.get("source_sha256") != source_sha256:
        raise ValueError("参考文献映射不属于当前源 PDF")
    if mapping.get("segments_sha256") != _sha256(segments_path):
        raise ValueError("segments.jsonl 已变化；请重新复核参考文献映射")
    rows = read_jsonl(segments_path)
    known_ids = {str(row.get("id", "")) for row in rows}
    selected = mapping.get("reference_segment_ids")
    if not isinstance(selected, list) or any(
        not isinstance(segment_id, str) for segment_id in selected
    ):
        raise ValueError("参考文献映射的 reference_segment_ids 字段非法")
    if len(set(selected)) != len(selected):
        raise ValueError("参考文献映射包含重复片段 ID")
    if not isinstance(mapping.get("confirmed_by"), str) or not str(
        mapping["confirmed_by"]
    ).strip():
        raise ValueError("参考文献映射缺少 confirmed_by")
    unknown = sorted(set(selected) - known_ids)
    if unknown:
        raise ValueError(f"参考文献映射包含未知片段 ID：{unknown}")
    return mapping
