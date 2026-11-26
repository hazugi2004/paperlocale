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
    def test_left_column_references_include_right_top_and_stop_at_acknowledgements(self) -> None:
        """左栏下方开始的书目包括右栏顶部，且不能吞掉无编号致谢正文。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "columns.pdf"
            document = canvas.Canvas(str(pdf), pagesize=(600, 800))
            document.setFont("Helvetica", 7)
            body = "This is ordinary scientific discussion outside references."
            reference_lines = [
                "Smith A. Scientific observations and climate responses.",
                "Journal of Climate volume 20 pages 100 to 120 (2024).",
                "https://doi.org/10.1000/scientific-reference-example",
            ]
            # 特意先绘右栏，再绘左栏，证明结果来自明确栏坐标而非对象插入顺序。
            for i, line in enumerate(reference_lines):
                document.drawString(320, 760-i*12, line)
                document.drawString(40, 760-i*12, body)
            document.drawString(40, 400, "References")
            for i, line in enumerate(reference_lines):
                document.drawString(40, 380-i*12, line.replace("Smith", "Jones"))
            document.drawString(320, 680, "Acknowledgements")
            thanks = "We thank the researchers and institutions for open data and financial support for this scientific study."
            document.drawString(320, 668, thanks)
            document.save()
            ref = " ".join(reference_lines)
            sources = [ref, thanks, body]
            segments = root / "segments.jsonl"
            write_jsonl_atomic(segments, [{"id": segment_id(s), "source": s} for s in sources])
            result = prepare_reference_review(
                source_pdf=pdf, source_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
                segments_path=segments, output_dir=root,
            )
            self.assertIn(segment_id(ref), result["automatic_reference_segment_ids"])
            self.assertNotIn(segment_id(thanks), result["automatic_reference_segment_ids"])
            self.assertNotIn(segment_id(body), result["automatic_reference_segment_ids"])

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

    def test_ecological_monographs_resolves_fragmented_heading_and_stops_at_supporting_info(self) -> None:
        """正文被按词拆行时，刊物标题样式只消歧，书目仍由下节标题结束。"""
        from paperlocale.source_layout import extract_layout, load_plan, save_json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ambiguous-reference-heading.pdf"
            document = canvas.Canvas(str(source))
            document.drawString(40, 765, "Ecological Monographs, 95, 10.1002/ecm.70123")
            document.drawString(40, 660, "The body considers the availability of")
            document.drawString(40, 635, "reference")
            document.drawString(40, 610, "barcode libraries in biodiversity surveys.")
            document.showPage()
            document.setFont("Helvetica-Bold", 11)
            document.drawString(40, 735, "REFERENCES")
            document.setFont("Helvetica", 10)
            document.drawString(40, 715, "Avery, S. 2019. A bibliography specimen from field surveys.")
            document.showPage()
            document.drawString(40, 730, "Brown, T. 2020. A bibliography specimen from another page.")
            document.setFont("Helvetica-Bold", 11)
            document.drawString(40, 670, "SUPPORTING INFORMATION")
            document.setFont("Helvetica", 10)
            document.drawString(40, 645, "Additional materials are available through the repository.")
            document.save()

            old = extract_layout(source, paragraph=True, publisher_layout=2)
            modern = extract_layout(source, paragraph=True, publisher_layout=3)
            self.assertEqual(modern['journal']['name'], 'Ecological Monographs')
            self.assertFalse(any(r['kind'] == 'reference' for r in old['protected_regions']))
            self.assertEqual({r['page'] for r in modern['protected_regions'] if r['kind'] == 'reference'}, {2, 3})
            specimens = [b for b in modern['blocks'] if 'bibliography specimen' in b['text']]
            self.assertEqual([(b['page'], b['kind']) for b in specimens], [(2, 'reference'), (3, 'reference')])
            additional = next(b for b in modern['blocks'] if 'available through' in b['text'])
            self.assertNotEqual(additional['kind'], 'reference')
            for name, plan in (('old', old), ('modern', modern)):
                path = root / f'{name}.json'
                save_json(path, plan)
                checked, _ = load_plan(source, path, paragraph=True)
                self.assertEqual(checked, plan)

    def test_ecological_monographs_preserves_linked_orcid_continuation(self) -> None:
        """名录续页须有斜体姓名和完整链接，错误的折行链接不能保护正文。"""
        from paperlocale.source_layout import extract_layout, load_plan, save_json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "orcid-continuation.pdf"
            document = canvas.Canvas(str(source), pagesize=(600, 800))
            first = "https://orcid.org/0000-0002-1234-123X"
            second = "https://orcid.org/0000-0001-1234-5678"
            for number in (1, 2, 3):
                document.drawString(45, 745, "Ecological Monographs, 95, 10.1002/ecm.70123")
                document.drawString(45, 690, "This field study uses multiple sites to assess changes in vegetation.")
                rows = [("Anna Avery", "Helvetica-Oblique", None),
                        (first[:-4], "Helvetica", first),
                        (first[-4:], "Helvetica", first if number < 3 else second),
                        ("Beatriz Álvarez", "Helvetica-Oblique", None),
                        (second, "Helvetica", second)]
                if number == 1:
                    rows.insert(0, ("ORCID", "Helvetica-Bold", None))
                for index, (value, font, uri) in enumerate(rows):
                    y = 450 - index * 10
                    document.setFont(font, 10)
                    document.drawString(45, y, value)
                    if uri:
                        right = 45 + document.stringWidth(value, font, 10)
                        document.linkURL(uri, (45, y - 3, right, y + 10), thickness=0)
                document.showPage()
            document.save()

            approved = 'Ecological Monographs 作者 ORCID 名录原样保留'
            old = extract_layout(source, paragraph=True, publisher_layout=2)
            modern = extract_layout(source, paragraph=True, publisher_layout=3)
            self.assertFalse(any(b.get('preserve_reason') == approved for b in old['blocks']))
            accepted = [b for b in modern['blocks'] if b.get('preserve_reason') == approved]
            self.assertEqual({b['page'] for b in accepted}, {1, 2})
            self.assertTrue(all(b['kind'] == 'preserve' for b in accepted))
            self.assertIn('Anna Avery', ''.join(b['text'] for b in accepted if b['page'] == 2))
            self.assertIn('Beatriz Álvarez', ''.join(b['text'] for b in accepted if b['page'] == 2))
            self.assertTrue(any(b['page'] == 3 and b['kind'] == 'body' and 'Anna Avery' in b['text']
                                for b in modern['blocks']))
            plan_path = root / 'plan.json'
            save_json(plan_path, modern)
            checked, _ = load_plan(source, plan_path, paragraph=True)
            self.assertEqual(checked, modern)

    def test_global_change_biology_preserves_independent_orcid_rows(self) -> None:
        """独立姓名行需由同一 URI 的徽标和可见链接佐证，旧计划保持原分类。"""
        import pymupdf as fitz
        from paperlocale.source_layout import extract_layout, load_plan, save_json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'gcb-orcid.pdf'
            identifiers = ('0000-0002-1234-123X', '0000-0001-1234-5678')
            with fitz.open() as doc:
                for page_index in (0, 1):
                    page = doc.new_page(width=600, height=800)
                    page.insert_text((45, 60), 'Global Change Biology, 10.1111/gcb.70000', fontsize=9)
                    page.insert_text((45, 98), 'The observation data are available from the authors.', fontsize=8)
                    page.insert_text((45, 124), 'ORCID', fontname='hebo', fontsize=8)
                    for index, (name, identifier) in enumerate(zip(('María Martin', 'Wen Li'), identifiers)):
                        uri = 'https://orcid.org/' + identifier
                        y = 137 + 13 * index
                        edge = 45 + fitz.get_text_length(name, fontname='heit', fontsize=8)
                        page.insert_text((45, y), name, fontname='heit', fontsize=8)
                        page.draw_circle((edge + 9.75, y - 4.25), 4.25, color=None, fill=(.647, .804, .224))
                        page.insert_text((edge + 17, y), uri, fontsize=8)
                        # 第二页的第一个圆形仍在原位，但点击会链接到另一位作者。
                        badge_uri = 'https://orcid.org/' + identifiers[1] if page_index and not index else uri
                        page.insert_link({'kind': fitz.LINK_URI, 'uri': badge_uri,
                                          'from': fitz.Rect(edge + 4, y - 10, edge + 15, y + 1)})
                        page.insert_link({'kind': fitz.LINK_URI, 'uri': uri,
                                          'from': fitz.Rect(edge + 17, y - 9, edge + 160, y + 2)})
                    if page_index:
                        page.insert_text((45, 183), 'REFERENCES', fontname='hebo', fontsize=8)
                        page.insert_text((45, 199), 'Avery, A. (2020). An original test article.', fontsize=8)
                doc.save(source)

            approved = 'Global Change Biology 作者 ORCID 名录原样保留'
            for revision in (1, 2, 3):
                plan = extract_layout(source, paragraph=True, publisher_layout=revision)
                self.assertEqual(plan['journal']['name'], 'Global Change Biology')
                accepted = [b for b in plan['blocks'] if b.get('preserve_reason') == approved]
                if revision == 3:
                    self.assertEqual([(b['page'], b['kind']) for b in accepted], [(1, 'preserve')] * 3)
                    self.assertEqual(accepted[0]['text'], 'ORCID')
                    self.assertIn('María Martin', accepted[1]['text'])
                    self.assertIn('Wen Li', accepted[2]['text'])
                    self.assertTrue(any(b['page'] == 2 and b['kind'] == 'body' and 'María Martin' in b['text']
                                        for b in plan['blocks']))
                else:
                    self.assertFalse(accepted)
                self.assertTrue(any(b['kind'] == 'body' and 'observation data' in b['text'] for b in plan['blocks']))
                self.assertTrue(any(b['kind'] == 'reference' and 'REFERENCES' in b['text'] for b in plan['blocks']))
                plan_path = root / f'plan-{revision}.json'
                save_json(plan_path, plan)
                checked, _ = load_plan(source, plan_path, paragraph=True)
                self.assertEqual(checked, plan)

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
