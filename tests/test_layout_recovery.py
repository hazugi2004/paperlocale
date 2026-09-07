"""0.5.0 版式回归：自有 PDF 验证真实字形/坐标，不调用在线模型。

覆盖单栏、双栏、横向页面、跨栏出版商续文、书目重叠、透明路径与相同边界
不同形状。合成翻译故意破坏书目布局，以确认恢复输出与源区域像素一致。
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz

from paperlocale.references import _reference_geometry, preserve_reference_layout
from paperlocale.qa import inspect_pdf_pair
from paperlocale.workflow import (
    _missing_source_vector_drawings, _replay_vector_drawing,
    _verify_vector_repair, initialize_run, load_manifest, save_manifest,
    restore_reference_layout, rollback_last_repair,
)


def reference_pair(root, columns=2, landscape=False):
    """生成源书目与含重叠译文的候选，保留外部正文用于越界检验。"""
    source, target = root / "source.pdf", root / "target.pdf"
    width, height = (850, 600) if landscape else (600, 800)
    with fitz.open() as doc:
        page = doc.new_page(width=width, height=height)
        page.insert_text((40, 90), "Publisher's note", fontsize=12)
        page.insert_text((40, 115), "These claims belong only to the authors.", fontsize=9)
        if columns == 2:
            page.insert_text((width / 2 + 10, 100),
                             "organizations, or those of the publisher and reviewers.", fontsize=8)
        page.insert_text((40, 180), "References", fontsize=12)
        for x in ([40, width / 2 + 10] if columns == 2 else [40]):
            for i in range(5):
                page.insert_text((x, 210 + i * 14),
                                 f"Smith, A. (202{i}). Climate observations and change.", fontsize=8)
        doc.save(source)
    with fitz.open(source) as doc:
        doc[0].insert_text((40, 214), "OVERLAPPING BAD TRANSLATION", fontsize=13)
        doc.save(target)
    return source, target


class LayoutRecoveryTest(unittest.TestCase):
    def test_explicit_region_review_binds_both_pdfs_and_is_recorded(self):
        """人工坐标不得消费旧PDF身份；通过后仍经正常备份和QA失效路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = reference_pair(root)
            before = target.read_bytes()
            run = root / "run"
            manifest = initialize_run(source_pdf=source, run_dir=run,
                                      source_language="en", target_language="zh-CN")
            manifest.update(status="rendered", rendered_pdf=str(target),
                            rendered_sha256=hashlib.sha256(before).hexdigest())
            save_manifest(run, manifest)
            review = {"source_sha256": manifest["source_sha256"],
                      "translated_sha256": manifest["rendered_sha256"],
                      "reviewed_by": "test reviewer", "regions": _reference_geometry(source)[3]}
            file = root / "review.json"
            for field in ["source_sha256", "translated_sha256", "reviewed_by"]:
                bad = dict(review); bad[field] = ""
                file.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    restore_reference_layout(run, regions_file=file)
                self.assertEqual(target.read_bytes(), before)
            file.write_text(json.dumps(review))
            restore_reference_layout(run, regions_file=file)
            final = load_manifest(run)
            self.assertEqual(final["status"], "rendered")
            self.assertEqual(final["repair_history"][-1]["region_review"], review)
            self.assertNotEqual(target.read_bytes(), before)

    def test_explicit_regions_reject_invalid_or_overlapping_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = reference_pair(root)
            bad_regions = [
                [{"page": 0, "rect": [1, 1, 20, 20]}],
                [{"page": 1, "rect": [1, 1, float("nan"), 20]}],
                [{"page": 1, "rect": [-1, 1, 20, 20]}],
                [{"page": 1, "rect": [40, 190, 200, 290]}] * 2,
            ]
            for regions in bad_regions:
                with self.assertRaises(ValueError):
                    preserve_reference_layout(source, target, root / "candidate.pdf", regions=regions)
                self.assertFalse((root / "candidate.pdf").exists())

    def test_qa_detects_missing_vector_inside_form_xobject(self):
        """图表常封装在 Form 内，顶层绘制操作数量为零也不能漏检。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with fitz.open() as form, fitz.open() as source, fitz.open() as target:
                fp = form.new_page()
                fp.draw_line((20, 20), (100, 100))
                for doc in (source, target):
                    page = doc.new_page()
                    page.insert_text((40, 150), "Scientific observations and model evaluation.\n" * 28)
                source[0].show_pdf_page(source[0].rect, form, 0)
                source.save(root / "source.pdf")
                target.save(root / "target.pdf")
            report = inspect_pdf_pair(source_pdf=root / "source.pdf", translated_pdf=root / "target.pdf",
                                      output_dir=root / "qa", dpi=36)
            self.assertEqual(report["pages"][0]["source_vector_drawings"], 1)
            self.assertTrue(any("矢量绘图减少" in error for error in report["errors"]))

    def test_reference_regions_restore_exact_visible_layout(self):
        for columns, landscape in [(1, False), (2, False), (2, True)]:
            with self.subTest(columns=columns, landscape=landscape), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, target = reference_pair(root, columns, landscape)
                candidate = root / "fixed.pdf"
                before = target.read_bytes()
                regions = preserve_reference_layout(source, target, candidate)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(len(regions), columns)
                with fitz.open(source) as src, fitz.open(candidate) as fixed:
                    for region in regions:
                        rect = fitz.Rect(region["rect"])
                        # 像素比较能发现文字仍重叠、字体变换或残留隐藏译文造成的可见损伤。
                        self.assertEqual(src[0].get_pixmap(clip=rect).samples,
                                         fixed[0].get_pixmap(clip=rect).samples)
                    self.assertNotIn("OVERLAPPING", fixed[0].get_text())
                    self.assertIn("claims belong only", fixed[0].get_text())

    def test_cross_column_publisher_continuation_is_not_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, _target = reference_pair(Path(tmp))
            _pages, text, _numbers, regions = _reference_geometry(source)
            self.assertNotIn("organizations, or those", text)
            self.assertTrue(all(region["rect"][1] > 130 for region in regions))

    def test_repeated_doi_header_does_not_hide_right_column_references(self):
        """复现 Nature 双栏稿：长 DOI 页眉不能冒充右栏首段正文。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "numbered.pdf"
            with fitz.open() as doc:
                for i in range(2):
                    page = doc.new_page(width=600, height=800)
                    page.insert_text((310, 25), "https://doi.org/10.1000/a-long-repeated-journal-header", fontsize=7)
                    for j in range(3):
                        page.insert_text((40, 90+j*15), "Body discussion remains outside all reference entries.", fontsize=8)
                        page.insert_text((310, 90+j*15), f"Tripathy, K. ({2020+j}). Compound drought study and results.", fontsize=7)
                    if i == 0:
                        page.insert_text((40, 350), "References", fontsize=10)
                        page.insert_text((40, 370), "Smith, A. (2020). Scientific observations of drought.", fontsize=8)
                doc.save(path)
            _pages, text, _numbers, regions = _reference_geometry(path)
            self.assertIn("Tripathy", text)
            right = [r for r in regions if r['page'] == 1 and r['rect'][0] > 300]
            self.assertEqual(len(right), 1)
            self.assertLess(right[0]['rect'][1], 100)

    def test_reference_repair_checkpoint_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = reference_pair(root)
            original = target.read_bytes()
            run = root / "run"
            manifest = initialize_run(source_pdf=source, run_dir=run,
                                      source_language="en", target_language="zh-CN")
            manifest.update(status="rendered", rendered_pdf=str(target),
                            rendered_sha256=hashlib.sha256(original).hexdigest())
            save_manifest(run, manifest)
            restore_reference_layout(run)
            repaired = target.read_bytes()
            self.assertNotEqual(original, repaired)
            restore_reference_layout(run)
            self.assertEqual(target.read_bytes(), repaired)
            self.assertEqual(len(load_manifest(run)["repair_history"]), 1)
            rollback_last_repair(run, reason="regression verification")
            self.assertEqual(target.read_bytes(), original)
            self.assertNotIn("reference_layout_preserved", load_manifest(run))

    def test_pending_redactions_are_never_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = reference_pair(root)
            with fitz.open(target) as doc:
                doc[0].add_redact_annot(fitz.Rect(40, 70, 200, 130))
                doc.saveIncr()
            before = target.read_bytes()
            with self.assertRaisesRegex(ValueError, "待应用删除"):
                preserve_reference_layout(source, target, root / "unsafe.pdf")
            self.assertEqual(before, target.read_bytes())

    def test_same_bbox_does_not_hide_a_missing_path(self):
        with fitz.open() as a, fitz.open() as b:
            source, target = a.new_page(), b.new_page()
            source.draw_line((20, 20), (80, 80))
            target.draw_line((20, 80), (80, 20))
            self.assertEqual(len(_missing_source_vector_drawings(source, target)), 1)

    def test_transparency_zero_is_preserved(self):
        with fitz.open() as a, fitz.open() as b:
            source, target = a.new_page(), b.new_page()
            source.draw_rect((20, 20, 80, 80), fill=(1, 0, 0), fill_opacity=0,
                             stroke_opacity=0)
            _replay_vector_drawing(target, source.get_drawings()[0])
            replayed = target.get_drawings()[0]
            self.assertEqual(replayed["fill_opacity"], 0)
            self.assertEqual(replayed["stroke_opacity"], 0)

    def test_more_paths_cannot_hide_changed_original_graphic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with fitz.open() as doc:
                page = doc.new_page()
                page.draw_line((20, 20), (80, 80))
                doc.save(root / "before.pdf")
            with fitz.open() as doc:
                page = doc.new_page()
                page.draw_line((20, 80), (80, 20))
                page.draw_line((10, 100), (100, 100))
                doc.save(root / "after.pdf")
            with self.assertRaisesRegex(ValueError, "已有路径"):
                _verify_vector_repair(root / "before.pdf", root / "after.pdf")
