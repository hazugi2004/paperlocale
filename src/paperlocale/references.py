"""参考文献片段的确定性发现、人工确认与运行绑定。

CLITranslator 返回的片段没有页码，而且顺序不等于 PDF 阅读顺序。本模块只把
能够完整落入源 PDF 参考文献区域的长片段自动标记；其余片段必须由用户查看
本地复核清单后显式确认，不能依赖作者年份或 DOI 密度等猜测性规则。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pymupdf as fitz

from .contracts import read_jsonl, write_jsonl_atomic

REFERENCE_POLICIES = ("preserve", "translate-titles")
REFERENCE_HEADING_RE = re.compile(r"^\s*REFERENCES\s*$", re.IGNORECASE)
MINIMUM_EXACT_MATCH_CHARACTERS = 80


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_match_text(text: str) -> str:
    """仅用于确定性包含判断；删除排版空白和标点，不改字母数字顺序。"""

    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _reference_region(source_pdf: Path) -> tuple[list[int], str | None]:
    """返回精确标题页列表；仅在标题唯一时提取标题之后的参考文献区域。"""

    document = fitz.open(source_pdf)
    headings: list[tuple[int, float]] = []
    page_blocks: list[list[tuple[float, float, float, float, str]]] = []
    try:
        for page_index, page in enumerate(document):
            blocks = [
                (
                    float(block[0]),
                    float(block[1]),
                    float(block[2]),
                    float(block[3]),
                    str(block[4]),
                )
                for block in page.get_text("blocks")
            ]
            page_blocks.append(blocks)
            for block in blocks:
                if REFERENCE_HEADING_RE.fullmatch(block[4]):
                    headings.append((page_index, block[3]))
    finally:
        document.close()

    heading_pages = [page_index + 1 for page_index, _bottom in headings]
    if len(headings) != 1:
        return heading_pages, None

    heading_page, heading_bottom = headings[0]
    region_parts = [
        block[4]
        for block in page_blocks[heading_page]
        if block[1] >= heading_bottom
    ]
    for blocks in page_blocks[heading_page + 1 :]:
        region_parts.extend(block[4] for block in blocks)
    return heading_pages, "\n".join(region_parts)


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
    heading_pages, region_text = _reference_region(source)
    normalized_region = (
        _normalized_match_text(region_text) if region_text is not None else ""
    )
    review_rows: list[dict[str, object]] = []
    automatic_ids: list[str] = []
    automatic_characters = 0
    for index, row in enumerate(rows, 1):
        segment_id = str(row.get("id", ""))
        source_text = str(row.get("source", ""))
        normalized = _normalized_match_text(source_text)
        heading_match = REFERENCE_HEADING_RE.fullmatch(source_text) is not None
        region_match = (
            len(normalized) >= MINIMUM_EXACT_MATCH_CHARACTERS
            and bool(normalized_region)
            and normalized in normalized_region
        )
        automatic = heading_match or region_match
        if automatic:
            automatic_ids.append(segment_id)
            if region_match:
                automatic_characters += len(normalized)
        review_rows.append(
            {
                "index": index,
                "id": segment_id,
                "source": source_text,
                "automatic_exact_match": automatic,
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
