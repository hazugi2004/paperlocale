"""参考文献映射测试使用自有 PDF，并覆盖片段乱序和人工补充。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from reportlab.pdfgen import canvas

from paperlocale.contracts import segment_id, write_jsonl_atomic
from paperlocale.references import (
    REFERENCE_HEADING_RE,
    confirm_reference_review,
    load_reference_map,
    prepare_reference_review,
)


def _build_reference_pdf(path: Path) -> tuple[str, str, str]:
    """生成正文、确定性参考文献块和需要人工确认的乱序块。"""

    body = "The body discusses compound drought and heat waves over eastern China."
    exact_reference = (
        "Wang, A., B. Chen, and C. Liu, 2021: Compound drought methods in China. "
        "J. Climate, 34, 100–120, https://doi.org/10.1000/example.2021."
    )
    first_column = (
        "Perkins, S. E., 2013: Heat-wave measurement. J. Climate, 26, 4500–4517."
    )
    second_column = (
        "Zhang, Y., 2020: Drought indicators. Hydrol. Sci., 12, 10–22."
    )
    ambiguous_reference = second_column + " " + first_column

    document = canvas.Canvas(str(path))
    document.drawString(40, 780, body)
    document.setFont("Helvetica-Bold", 11)
    document.drawString(40, 420, "REFERENCES")
    document.setFont("Helvetica", 8)
    document.drawString(40, 350, exact_reference)
    document.showPage()
    # PDF 阅读区域中是 first -> second，CLITranslator 片段则模拟成 second -> first。
    document.drawString(40, 760, first_column)
    document.drawString(300, 760, second_column)
    document.save()
    return body, exact_reference, ambiguous_reference


def _build_line_numbered_reference_pdf(path: Path) -> tuple[str, str, str, str]:
    """生成标题块含章节号、单数标题和稿件行号的投稿手稿夹具。"""

    body = "The methods cite Reference 353 but this sentence is not a heading."
    exact_reference = (
        "Aas, K., C. Czado, A. Frigessi, et al. (2009). Pair-copula "
        "constructions of multiple dependence. 354 Insur. Math. Econ., "
        "44(2): 182-198. 355"
    )
    heading = "6 Reference 353"
    figure_caption = "Figure 1 shows a compound drought and heat event."

    document = canvas.Canvas(str(path))
    document.drawString(40, 780, body)
    document.showPage()

    # 行号和正文分别绘制但共享基线，复现真实投稿手稿的左侧行号文本流。
    document.setFont("Helvetica", 8)
    document.drawString(40, 780, "353")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(70, 780, "6 Reference")
    reference_lines = (
        ("354", "Aas, K., C. Czado, A. Frigessi, et al. (2009). Pair-copula"),
        ("355", "constructions of multiple dependence. Insur. Math. Econ.,"),
        ("356", "44(2): 182-198."),
    )
    document.setFont("Helvetica", 8)
    for offset, (line_number, text) in enumerate(reference_lines):
        y = 720 - offset * 14
        document.drawString(40, y, line_number)
        document.drawString(70, y, text)
    document.showPage()
    document.drawString(40, 780, "357")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(70, 780, "7 Figure")
    document.setFont("Helvetica", 8)
    document.drawString(40, 740, "358")
    document.drawString(70, 740, figure_caption)
    document.save()
    return body, exact_reference, heading, figure_caption


def _build_shared_block_reference_pdf(path: Path) -> str:
    """生成标题与首条书目可能共享文本块的紧凑版式。"""

    reference = (
        "Sutanto, S. J., 2024: Sensitivity of dry indicators to climate. "
        "Hydrol. Earth Syst. Sci., 28, 100-120. "
        "https://doi.org/10.1000/example.2024."
    )
    document = canvas.Canvas(str(path))
    document.setFont("Helvetica-Bold", 11)
    document.drawString(40, 780, "References")
    document.setFont("Helvetica", 8)
    # 极小行距会让 PDF 提取器将标题和书目归入同一文本块。
    document.drawString(
        40,
        768,
        "Sutanto, S. J., 2024: Sensitivity of dry indicators to climate. ",
    )
    document.drawString(
        40,
        756,
        "Hydrol. Earth Syst. Sci., 28, 100-120. "
        "https://doi.org/10.1000/example.2024.",
    )
    document.save()
    return reference


def _build_right_column_reference_pdf(path: Path) -> tuple[str, str]:
    """生成右栏中途开始参考文献、左栏同高度仍为正文的页面。"""

    body = (
        "In the future, the research should focus on compound-event mechanisms "
        "and their regional ecological consequences under multiple scenarios."
    )
    reference = (
        "Smith, A., B. Jones, and C. White, 2024: Compound drought and heat "
        "assessment across climate regions. Journal of Climate, 37, 100-125."
    )
    document = canvas.Canvas(str(path))
    document.setFont("Helvetica-Bold", 11)
    document.drawString(320, 560, "References")
    document.setFont("Helvetica", 8)
    document.drawString(40, 530, body)
    document.drawString(
        320,
        530,
        "Smith, A., B. Jones, and C. White, 2024: Compound drought and heat",
    )
    document.drawString(
        320,
        518,
        "assessment across climate regions. Journal of Climate, 37, 100-125.",
    )
    document.save()
    return body, reference


class ReferenceReviewTest(unittest.TestCase):
    def test_exact_matches_and_manual_ids_form_bound_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "paper.pdf"
            body, exact_reference, ambiguous_reference = _build_reference_pdf(
                source_pdf
            )
            source_hash = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
            segments = root / "segments.jsonl"
            ordered_sources = (body, ambiguous_reference, exact_reference, "REFERENCES")
            rows = [
                {"id": segment_id(source), "source": source}
                for source in ordered_sources
            ]
            write_jsonl_atomic(segments, rows)

            summary = prepare_reference_review(
                source_pdf=source_pdf,
                source_sha256=source_hash,
                segments_path=segments,
                output_dir=root,
            )
            automatic = set(summary["automatic_reference_segment_ids"])
            self.assertEqual(summary["heading_pages"], [1])
            self.assertIn(segment_id(exact_reference), automatic)
            self.assertIn(segment_id("REFERENCES"), automatic)
            self.assertNotIn(segment_id(body), automatic)
            self.assertNotIn(segment_id(ambiguous_reference), automatic)

            mapping = confirm_reference_review(
                source_pdf=source_pdf,
                source_sha256=source_hash,
                segments_path=segments,
                output_dir=root,
                additional_segment_ids=[segment_id(ambiguous_reference)],
                confirmed_by="reviewer",
            )
            self.assertEqual(mapping["confirmed_by"], "reviewer")
            self.assertEqual(
                set(mapping["reference_segment_ids"]),
                automatic | {segment_id(ambiguous_reference)},
            )
            loaded = load_reference_map(
                source_sha256=source_hash,
                segments_path=segments,
                map_path=root / "reference_map.json",
            )
            self.assertEqual(loaded, mapping)

            write_jsonl_atomic(
                segments,
                rows + [{"id": segment_id("new"), "source": "new"}],
            )
            with self.assertRaisesRegex(ValueError, "segments.jsonl 已变化"):
                load_reference_map(
                    source_sha256=source_hash,
                    segments_path=segments,
                    map_path=root / "reference_map.json",
                )

    def test_numbered_singular_heading_with_manuscript_line_number(self) -> None:
        """章节号和边栏行号不能阻止精确参考文献区域识别。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "line-numbered-paper.pdf"
            body, exact_reference, heading, figure_caption = (
                _build_line_numbered_reference_pdf(source_pdf)
            )
            segments = root / "segments.jsonl"
            rows = [
                {"id": segment_id(source), "source": source}
                for source in (body, exact_reference, heading, figure_caption)
            ]
            write_jsonl_atomic(segments, rows)

            summary = prepare_reference_review(
                source_pdf=source_pdf,
                source_sha256=hashlib.sha256(source_pdf.read_bytes()).hexdigest(),
                segments_path=segments,
                output_dir=root,
            )
            automatic = set(summary["automatic_reference_segment_ids"])
            self.assertEqual(summary["heading_pages"], [2])
            self.assertTrue(summary["automatic_region_available"])
            self.assertEqual(summary["manuscript_line_number_count"], 4)
            self.assertIn(segment_id(exact_reference), automatic)
            self.assertIn(segment_id(heading), automatic)
            self.assertNotIn(segment_id(body), automatic)
            self.assertNotIn(segment_id(figure_caption), automatic)

    def test_heading_pattern_rejects_reference_words_in_prose(self) -> None:
        """扩展标题形式后仍须拒绝带普通语义上下文的正文句子。"""

        accepted = ("Reference", "REFERENCES", "6 Reference 353", "6 References")
        rejected = (
            "See Reference 353",
            "6 Reference methods 353",
            "Reference list follows",
        )
        for value in accepted:
            self.assertIsNotNone(REFERENCE_HEADING_RE.fullmatch(value), value)
        for value in rejected:
            self.assertIsNone(REFERENCE_HEADING_RE.fullmatch(value), value)

    def test_heading_is_found_when_pdf_groups_it_with_reference_text(self) -> None:
        """文本块合并不能导致整个参考文献区域漏检。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "shared-block.pdf"
            reference = _build_shared_block_reference_pdf(source_pdf)
            segments = root / "segments.jsonl"
            rows = [
                {"id": segment_id(source), "source": source}
                for source in ("References", reference)
            ]
            write_jsonl_atomic(segments, rows)

            summary = prepare_reference_review(
                source_pdf=source_pdf,
                source_sha256=hashlib.sha256(source_pdf.read_bytes()).hexdigest(),
                segments_path=segments,
                output_dir=root,
            )
            automatic = set(summary["automatic_reference_segment_ids"])
            self.assertEqual(summary["heading_pages"], [1])
            self.assertTrue(summary["automatic_region_available"])
            self.assertIn(segment_id(reference), automatic)

    def test_right_column_heading_does_not_capture_left_column_body(self) -> None:
        """右栏 References 不能把同页左栏未结束的正文标为书目。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "right-column-references.pdf"
            body, reference = _build_right_column_reference_pdf(source_pdf)
            segments = root / "segments.jsonl"
            write_jsonl_atomic(
                segments,
                [
                    {"id": segment_id(source), "source": source}
                    for source in (body, reference, "References")
                ],
            )

            summary = prepare_reference_review(
                source_pdf=source_pdf,
                source_sha256=hashlib.sha256(source_pdf.read_bytes()).hexdigest(),
                segments_path=segments,
                output_dir=root,
            )
            automatic = set(summary["automatic_reference_segment_ids"])
            self.assertIn(segment_id(reference), automatic)
            self.assertIn(segment_id("References"), automatic)
            self.assertNotIn(segment_id(body), automatic)

    def test_reviewer_can_exclude_only_automatic_false_positives(self) -> None:
        """显式排除必须留在映射中，且不能用来删除人工补充项。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "right-column-references.pdf"
            body, reference = _build_right_column_reference_pdf(source_pdf)
            segments = root / "segments.jsonl"
            write_jsonl_atomic(
                segments,
                [
                    {"id": segment_id(source), "source": source}
                    for source in (body, reference, "References")
                ],
            )
            source_hash = hashlib.sha256(source_pdf.read_bytes()).hexdigest()

            mapping = confirm_reference_review(
                source_pdf=source_pdf,
                source_sha256=source_hash,
                segments_path=segments,
                output_dir=root,
                additional_segment_ids=[],
                excluded_automatic_segment_ids=[segment_id(reference)],
                confirmed_by="reviewer",
            )
            self.assertNotIn(segment_id(reference), mapping["reference_segment_ids"])
            self.assertEqual(
                mapping["excluded_automatic_segment_ids"],
                [segment_id(reference)],
            )
            self.assertEqual(
                load_reference_map(
                    source_sha256=source_hash,
                    segments_path=segments,
                    map_path=root / "reference_map.json",
                ),
                mapping,
            )
            with self.assertRaisesRegex(ValueError, "只能排除自动匹配"):
                confirm_reference_review(
                    source_pdf=source_pdf,
                    source_sha256=source_hash,
                    segments_path=segments,
                    output_dir=root,
                    additional_segment_ids=[],
                    excluded_automatic_segment_ids=[segment_id(body)],
                    confirmed_by="reviewer",
                )


if __name__ == "__main__":
    unittest.main()
