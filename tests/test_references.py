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


if __name__ == "__main__":
    unittest.main()
