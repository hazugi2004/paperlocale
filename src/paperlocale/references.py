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
import unicodedata
from collections import Counter
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
# 正式期刊通常在书目后使用不带编号的独立小节标题。只匹配整行，
# 不把参考文献题名中出现的同名词语当成章节边界。
POST_REFERENCE_HEADING_RE = re.compile(
    r"^(?:Acknowledg(?:e)?ments|Author contributions|Competing interests|"
    r"Additional information|Data availability|Code availability)$",
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
        unicodedata.normalize("NFKC", FORMULA_PLACEHOLDER_RE.sub("", text)).casefold(),
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


def _reference_geometry(
    source_pdf: Path,
) -> tuple[list[int], str | None, tuple[str, ...], list[dict[str, object]]]:
    """返回标题页、标题后区域和由 PDF 坐标确定的左侧稿件行号。

    参考文献标题可能与整页书目被 PyMuPDF 合并为一个文本块，
    因此识别和区域收集都以可见文本行为单位，不依赖块边界。
    """

    document = fitz.open(source_pdf)
    headings: list[tuple[int, float, float, float]] = []
    page_text_lines: list[list[tuple[float, float, float, float, str]]] = []
    page_line_entries: list[list[tuple[float, float, str]]] = []
    two_column_pages: set[int] = set()
    page_sizes: list[tuple[float, float]] = []
    try:
        for page_index, page in enumerate(document):
            page_sizes.append((page.rect.width, page.rect.height))
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
            # 两栏必须同时有多行、各自完全位于中线一侧的正文证据；
            # 居中的公式、行号或一条短页眉不足以触发栏序重排。
            middle = float(page.rect.width) / 2
            left_count = sum(line[2] < middle and len(line[4]) >= 40 for line in text_lines)
            right_count = sum(line[0] >= middle and len(line[4]) >= 40 for line in text_lines)
            if left_count >= 3 and right_count >= 3:
                two_column_pages.add(page_index)
                text_lines.sort(key=lambda line: (line[0] >= middle, line[1], line[0]))
            page_text_lines.append(text_lines)
            page_line_entries.append(line_entries)
            line_numbers = [entry[2] for entry in line_entries]
            for line in text_lines:
                if _heading_block_matches(line[4], line_numbers):
                    headings.append(
                        (page_index, line[0], line[3], float(page.rect.width))
                    )
    finally:
        document.close()

    heading_pages = [page_index + 1 for page_index, _x0, _bottom, _width in headings]
    if len(headings) != 1:
        return heading_pages, None, (), []

    heading_page, heading_x0, heading_bottom, heading_page_width = headings[0]
    margin_counts = Counter(
        line[4] for index, lines in enumerate(page_text_lines) for line in lines
        if line[3] < page_sizes[index][1] * 0.08 or line[1] > page_sizes[index][1] * 0.94
    )
    heading_starts_in_right_column = heading_x0 >= heading_page_width * 0.45
    region_parts: list[str] = []
    selected_lines: dict[tuple[int, int], list[tuple]] = {}
    region_boundary: tuple[int, float] | None = None
    for page_index in range(heading_page, len(page_text_lines)):
        for line in page_text_lines[page_index]:
            width, height = page_sizes[page_index]
            in_margin = line[3] < height * 0.08 or line[1] > height * 0.94
            if in_margin and (margin_counts[line[4]] > 1 or line[4].strip().isdigit()):
                continue
            if page_index == heading_page and line[1] < heading_bottom:
                # References 从左栏下半部开始时，右栏顶部已经是后续书目；
                # 不能用标题的 y 坐标把整张纸上方的右栏书目一起排除。
                follows_in_right_column = (
                    heading_page in two_column_pages
                    and not heading_starts_in_right_column
                    and line[0] >= heading_page_width / 2
                )
                if follows_in_right_column:
                    # 有些期刊在整页正文之后才开始横跨两栏的书目；右栏上方
                    # 此时是出版商说明的续文。只有右栏首段具备作者-书目开头
                    # 的明确结构才允许回溯到标题上方，不能仅因它位于右栏就保留。
                    right_body = [item for item in page_text_lines[page_index]
                                  if item[0] >= heading_page_width / 2 and len(item[4]) >= 40
                                  and not (item[3] < height * 0.08 and margin_counts[item[4]] > 1)]
                    follows_in_right_column = bool(right_body) and bool(re.match(
                        r"^(?:\d+[.)]?\s+)?[A-ZÀ-ÖØ-Þ][\w'’ -]+,?\s+[A-Z]\.", right_body[0][4]
                    ))
                if not follows_in_right_column:
                    continue
            if (
                page_index == heading_page
                and heading_starts_in_right_column
                and line[0] < heading_x0 - 2
            ):
                # 双栏论文可能在首页右栏中途开始 References，此时
                # 左栏同高度仍是正文。只限制这一个首页右栏，后续页
                # 仍保留全宽参考文献，不猜测其他排版。
                continue
            cleaned = _block_without_line_numbers(
                line[4],
                [entry[2] for entry in page_line_entries[page_index]],
            )
            # 参考文献必须止于下一个编号章节，不能把其后的 Figure 或 Table
            # 章节误标为参考文献。标题自身已经在上方排除，不会触发此边界。
            if (
                NUMBERED_SECTION_HEADING_RE.fullmatch(cleaned)
                or POST_REFERENCE_HEADING_RE.fullmatch(cleaned)
            ):
                region_boundary = (page_index, line[1])
                break
            region_parts.append(line[4])
            # 只对真实文字边界取并集；不能把所有页顶文字排除，书目可从页顶开始。
            column = int(line[0] >= width / 2) if page_index in two_column_pages else 0
            selected_lines.setdefault((page_index, column), []).append(line)
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
    # 标题自身属于 preserve 区域；紧凑期刊中译后首条书目可与标题相交，
    # 必须一起恢复而不是把英文 References 当成待保护的正文阻止修复。
    for line in page_text_lines[heading_page]:
        if _heading_block_matches(line[4], [entry[2] for entry in page_line_entries[heading_page]]):
            column = int(line[0] >= heading_page_width / 2) if heading_page in two_column_pages else 0
            if (heading_page, column) in selected_lines:
                selected_lines[(heading_page, column)].append(line)
    regions = [
        {"page": page_index + 1, "rect": [min(x[0] for x in lines) - 1,
         min(x[1] for x in lines) - 1, max(x[2] for x in lines) + 1,
         max(x[3] for x in lines) + 1]}
        for (page_index, _column), lines in selected_lines.items()
    ]
    return heading_pages, "\n".join(region_parts), line_numbers, regions


def _reference_region(source_pdf: Path) -> tuple[list[int], str | None, tuple[str, ...]]:
    """保持既有片段映射接口；区域几何和文本共用一次边界算法。"""

    pages, text, numbers, _regions = _reference_geometry(source_pdf)
    return pages, text, numbers


def preserve_reference_layout(
    source_pdf: Path, translated_pdf: Path, candidate_pdf: Path,
) -> list[dict[str, object]]:
    """把 preserve 书目区域以原始字形复制到独立候选，避免再次排版造成串栏。

    只处理唯一 References 标题确定的区域，不推断没有标题的书目。源页先删除
    区域外文字再导入，防止 PDF 裁切仅隐藏原文却留下整页不可见重复文本。
    译文区域外的文字位置必须完全一致；目标文件始终由工作流原子提交。
    """

    _headings, _text, _numbers, regions = _reference_geometry(source_pdf)
    if not regions:
        return []
    with fitz.open(source_pdf) as source, fitz.open(translated_pdf) as target:
        if len(source) != len(target):
            raise ValueError("参考文献恢复要求源文与译文页数一致")
        for record in regions:
            index = int(record["page"]) - 1
            rectangle = fitz.Rect(record["rect"])
            page = target[index]
            if any(list(p.annots(types=(fitz.PDF_ANNOT_REDACT,)) or [])
                   for p in (source[index], page)):
                raise ValueError("参考文献区域所在页有待应用删除标注，拒绝执行无关删除")
            if source[index].rect != page.rect or source[index].rotation != page.rotation:
                raise ValueError("参考文献恢复要求页面尺寸与旋转一致")
            # 坏排版可能把一整个书目词伸出源区域；只删相交字符会留下半个词。
            # 将相交词纳入修复，但扩展不得碰到源页的非书目文字（正文/标题）。
            original_rectangle = fitz.Rect(rectangle)
            for word in page.get_text("words"):
                if fitz.Rect(word[:4]).intersects(original_rectangle):
                    rectangle |= fitz.Rect(word[:4])
            for word in source[index].get_text("words"):
                bounds = fitz.Rect(word[:4])
                if not bounds.intersects(original_rectangle) and bounds.intersects(rectangle):
                    raise ValueError("书目越界涉及源页正文，不能安全自动恢复")
            record["rect"] = list(rectangle)
            before = [word for word in page.get_text("words")
                      if not fitz.Rect(word[:4]).intersects(rectangle)]
            with fitz.open() as excerpt:
                excerpt.insert_pdf(source, from_page=index, to_page=index)
                clipped = excerpt[0]
                bounds = clipped.rect
                outside = [fitz.Rect(bounds.x0, bounds.y0, bounds.x1, rectangle.y0),
                           fitz.Rect(bounds.x0, rectangle.y1, bounds.x1, bounds.y1),
                           fitz.Rect(bounds.x0, rectangle.y0, rectangle.x0, rectangle.y1),
                           fitz.Rect(rectangle.x1, rectangle.y0, bounds.x1, rectangle.y1)]
                for area in outside:
                    if not area.is_empty:
                        clipped.add_redact_annot(area, fill=False)
                clipped.apply_redactions(images=0, graphics=0)
                page.add_redact_annot(rectangle, fill=False)
                page.apply_redactions(images=0, graphics=0)
                page.show_pdf_page(rectangle, excerpt, 0, clip=rectangle)
            after = [word for word in page.get_text("words")
                     if not fitz.Rect(word[:4]).intersects(rectangle)]
            # 块号/行号会随删除重排；坐标及词文本才是区域外内容的稳定身份。
            def word_identity(words: list) -> Counter:
                return Counter(tuple(round(float(v), 2) for v in word[:4]) + (word[4],)
                               for word in words)
            if word_identity(before) != word_identity(after):
                raise ValueError(f"参考文献恢复改变了第{index + 1}页区域外文字："
                                 f"{list((word_identity(before) - word_identity(after)).elements())[:3]}")
            expected = source[index].get_text(clip=rectangle)
            actual = page.get_text(clip=rectangle)
            if _normalized_match_text(expected) != _normalized_match_text(actual):
                raise ValueError(f"第{index + 1}页参考文献复制后文字不一致")
        target.save(candidate_pdf, garbage=4, deflate=True)
    return regions


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
    excluded_automatic_segment_ids: list[str] | tuple[str, ...] = (),
) -> dict[str, object]:
    """合并自动结果、用户补充和显式排除为绑定当前输入的映射。"""

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
    excluded = set(excluded_automatic_segment_ids)
    unknown = sorted((requested | excluded) - known_ids)
    if unknown:
        raise ValueError(f"参考文献确认包含未知片段 ID：{unknown}")
    automatic = {
        str(segment_id)
        for segment_id in summary["automatic_reference_segment_ids"]
    }
    invalid_exclusions = sorted(excluded - automatic)
    if invalid_exclusions:
        raise ValueError(
            f"只能排除自动匹配的参考文献片段：{invalid_exclusions}"
        )
    conflicting = sorted(requested & excluded)
    if conflicting:
        raise ValueError(f"同一片段不能同时补充和排除：{conflicting}")
    confirmed = (automatic - excluded) | requested
    selected = [
        str(row["id"])
        for row in rows
        if str(row["id"]) in confirmed
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
        "excluded_automatic_segment_ids": [
            str(row["id"])
            for row in rows
            if str(row["id"]) in excluded
        ],
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
    automatic = mapping.get("automatic_reference_segment_ids", [])
    excluded = mapping.get("excluded_automatic_segment_ids", [])
    for field, values in (
        ("automatic_reference_segment_ids", automatic),
        ("excluded_automatic_segment_ids", excluded),
    ):
        if not isinstance(values, list) or any(
            not isinstance(segment_id, str) for segment_id in values
        ):
            raise ValueError(f"参考文献映射的 {field} 字段非法")
        if len(set(values)) != len(values):
            raise ValueError(f"参考文献映射的 {field} 包含重复 ID")
    if set(excluded) - set(automatic):
        raise ValueError("参考文献映射排除了非自动匹配片段")
    if set(excluded) & set(selected):
        raise ValueError("已排除的自动匹配片段仍出现在参考文献映射中")
    if not isinstance(mapping.get("confirmed_by"), str) or not str(
        mapping["confirmed_by"]
    ).strip():
        raise ValueError("参考文献映射缺少 confirmed_by")
    unknown = sorted(set(selected) - known_ids)
    if unknown:
        raise ValueError(f"参考文献映射包含未知片段 ID：{unknown}")
    return mapping
