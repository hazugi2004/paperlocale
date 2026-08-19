"""从源 PDF 可见文本确定性识别不宜直接送入模型的片段。

CLITranslator 可能只把一个英文单词的中段交给翻译器，而把前后残片留在固定
版面对象中。例如源文 ``Figure ... period`` 可能变成固定 ``Figu``、待译
``re ... perio`` 和固定 ``d``。在没有相邻对象上下文时翻译中段会产生
``Figu图...d``。本模块只用源 PDF 的精确可见文本边界发现这类案例，并生成
人工复核清单；它不猜测语义、不自动修改译文。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pymupdf as fitz

from .contracts import read_jsonl, segment_id, write_jsonl_atomic

FORMULA_PLACEHOLDER_RE = re.compile(r"\{v\d+\}", re.IGNORECASE)
SHORT_ASCII_TEXT_RE = re.compile(r"[A-Za-z][A-Za-z\s.,'’&()/-]{0,31}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _normalize_visible_text(text: str) -> str:
    """只折叠排版空白；字符和标点仍须逐字匹配。"""

    return re.sub(r"\s+", " ", text).strip()


def _is_ascii_letter(value: str) -> bool:
    return len(value) == 1 and value.isascii() and value.isalpha()


def _literal_left_prefix(text: str, start: int) -> str:
    """提取候选左侧同一 ASCII 单词中未交给翻译器的固定前缀。"""

    cursor = start
    while cursor > 0 and _is_ascii_letter(text[cursor - 1]):
        cursor -= 1
    return text[cursor:start]


def _literal_right_suffix(text: str, end: int) -> str:
    """提取候选右侧同一 ASCII 单词中未交给翻译器的固定后缀。"""

    cursor = end
    while cursor < len(text) and _is_ascii_letter(text[cursor]):
        cursor += 1
    return text[end:cursor]


def _split_occurrences(
    page_texts: list[str],
    source: str,
) -> tuple[int, list[dict[str, object]]]:
    """返回精确出现次数和落在 ASCII 单词内部的出现位置。"""

    occurrence_count = 0
    split_rows: list[dict[str, object]] = []
    for page_number, page_text in enumerate(page_texts, 1):
        start = 0
        while True:
            index = page_text.find(source, start)
            if index < 0:
                break
            occurrence_count += 1
            end = index + len(source)
            left_prefix = _literal_left_prefix(page_text, index)
            right_suffix = _literal_right_suffix(page_text, end)
            if left_prefix or right_suffix:
                context_start = max(index - 24, 0)
                context_end = min(end + 24, len(page_text))
                split_rows.append(
                    {
                        "page": page_number,
                        "literal_prefix": left_prefix,
                        "literal_suffix": right_suffix,
                        "visible_context": page_text[context_start:context_end],
                    }
                )
            start = index + 1
    return occurrence_count, split_rows


def prepare_segment_safety_review(
    *,
    source_pdf: Path,
    source_sha256: str,
    segments_path: Path,
    output_dir: Path,
) -> dict[str, object]:
    """生成必须人工透传的确定性片段安全复核清单。"""

    source = source_pdf.expanduser().resolve()
    segments = segments_path.expanduser().resolve()
    root = output_dir.expanduser().resolve()
    if _sha256(source) != source_sha256:
        raise ValueError("源 PDF 哈希与运行清单不一致")

    document = fitz.open(source)
    try:
        page_texts = [
            _normalize_visible_text(page.get_text("text")) for page in document
        ]
    finally:
        document.close()

    review_rows: list[dict[str, object]] = []
    required_ids: list[str] = []
    seen: set[str] = set()
    for row in read_jsonl(segments):
        raw_source = str(row.get("source", ""))
        sid = str(row.get("id", ""))
        if not raw_source or sid != segment_id(raw_source):
            raise ValueError(f"片段缺少原文或 ID 不一致：{sid!r}")
        if sid in seen:
            raise ValueError(f"片段包含重复 ID：{sid}")
        seen.add(sid)

        normalized_source = _normalize_visible_text(raw_source)
        occurrence_count, split_rows = _split_occurrences(
            page_texts,
            normalized_source,
        )
        kind: str | None = None
        if split_rows:
            kind = "split-ascii-word-boundary"
        elif (
            occurrence_count == 0
            and not FORMULA_PLACEHOLDER_RE.search(raw_source)
            and SHORT_ASCII_TEXT_RE.fullmatch(normalized_source) is not None
        ):
            # BabelDOC 能收集但 PyMuPDF 的可见页文本完全找不到的短 ASCII 片段，
            # 可能来自不可见或非页面对象。翻成另一种文字后才显现的风险高于收益。
            kind = "unlocated-short-ascii-text"

        if kind is None:
            continue
        required_ids.append(sid)
        review_rows.append(
            {
                "id": sid,
                "source": raw_source,
                "kind": kind,
                "visible_occurrence_count": occurrence_count,
                "split_occurrences": split_rows,
            }
        )

    review_path = root / "segment_safety_review.jsonl"
    summary_path = root / "segment_safety_summary.json"
    write_jsonl_atomic(review_path, review_rows)
    summary: dict[str, object] = {
        "schema_version": 1,
        "algorithm": "exact-visible-text-boundary-v1",
        "source_sha256": source_sha256,
        "segments_sha256": _sha256(segments),
        "required_passthrough_segment_ids": required_ids,
        "required_passthrough_count": len(required_ids),
        "split_token_candidate_count": sum(
            row["kind"] == "split-ascii-word-boundary" for row in review_rows
        ),
        "unlocated_short_text_count": sum(
            row["kind"] == "unlocated-short-ascii-text" for row in review_rows
        ),
        "review_jsonl": str(review_path),
        "review_jsonl_sha256": _sha256(review_path),
    }
    _write_json_atomic(summary_path, summary)
    return summary


def load_segment_safety_summary(
    *,
    source_sha256: str,
    segments_path: Path,
    summary_path: Path,
) -> dict[str, object]:
    """核对安全复核摘要仍绑定当前源 PDF 与片段字节。"""

    if not summary_path.is_file():
        raise FileNotFoundError(f"片段安全复核摘要不存在：{summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict) or summary.get("schema_version") != 1:
        raise ValueError("片段安全复核摘要格式非法")
    if summary.get("source_sha256") != source_sha256:
        raise ValueError("片段安全复核摘要不属于当前源 PDF")
    if summary.get("segments_sha256") != _sha256(segments_path):
        raise ValueError("segments.jsonl 已变化；请重新生成片段安全复核")
    selected = summary.get("required_passthrough_segment_ids")
    if not isinstance(selected, list) or any(
        not isinstance(sid, str) for sid in selected
    ):
        raise ValueError("片段安全复核 required_passthrough_segment_ids 字段非法")
    if len(selected) != len(set(selected)):
        raise ValueError("片段安全复核包含重复片段 ID")
    known_ids: set[str] = set()
    for row in read_jsonl(segments_path):
        raw_source = str(row.get("source", ""))
        sid = str(row.get("id", ""))
        if not raw_source or sid != segment_id(raw_source):
            raise ValueError(f"片段缺少原文或 ID 不一致：{sid!r}")
        if sid in known_ids:
            raise ValueError(f"片段包含重复 ID：{sid}")
        known_ids.add(sid)
    unknown = sorted(set(selected) - known_ids)
    if unknown:
        raise ValueError(f"片段安全复核包含未知片段 ID：{unknown}")
    if summary.get("required_passthrough_count") != len(selected):
        raise ValueError("片段安全复核候选数量不一致")
    review_value = summary.get("review_jsonl")
    expected_review_hash = summary.get("review_jsonl_sha256")
    if not isinstance(review_value, str) or not isinstance(expected_review_hash, str):
        raise ValueError("片段安全复核摘要缺少 review_jsonl 哈希")
    review_path = Path(review_value)
    if not review_path.is_file() or _sha256(review_path) != expected_review_hash:
        raise ValueError("片段安全复核 JSONL 在生成后发生变化")
    return summary
