"""QA uses the pixel-proven canonical source for rotated preserved pages."""
from pathlib import Path
import hashlib
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.font_geometry import open_source_pdf
from paperlocale.qa import inspect_pdf_pair
from paperlocale.source_layout import verify_unchanged
from paperlocale.workflow import initialize_run, load_manifest, qa_run, save_manifest


class RotatedPageQaTests(unittest.TestCase):
    def _fixture(self, root):
        source, target = root/'source.pdf', root/'translated.pdf'
        with fitz.open() as doc:
            page = doc.new_page(width=200, height=300)
            page.draw_rect(fitz.Rect(20, 20, 180, 260), fill=(.9, .9, .9))
            for index in range(10):
                page.insert_text((30, 40+20*index), f'Table row {index+1}: {index*7}', fontsize=10)
                page.draw_line((20, 45+20*index), (180, 45+20*index))
            page.draw_rect(fitz.Rect(25, 265, 55, 280), fill=(0, 0, 0))
            page.set_rotation(90)
            doc.save(source)
        with open_source_pdf(source) as canonical:
            canonical.save(target)
        return source, target

    def test_preserved_rotated_table_passes_protected_pixels_and_full_qa(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = self._fixture(root)
            verify_unchanged(source, target, [])
            run = root/'run'
            manifest = initialize_run(source_pdf=source, run_dir=run,
                                      source_language='en', target_language='zh-CN')
            manifest.update(status='rendered', layout_mode='paragraph', rendered_pdf=str(target),
                            rendered_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
            save_manifest(run, manifest)
            report = qa_run(run, dpi=72)
            self.assertEqual(report['errors'], [])
            self.assertEqual(load_manifest(run)['status'], 'qa_generated')
            page = report['pages'][0]
            self.assertEqual(page['raw_source_media_box'], (0., 0., 200., 300.))
            self.assertEqual(page['source_media_box'], page['translated_media_box'])
            self.assertEqual(page['source_effective_rect'], (0., 0., 300., 200.))
            self.assertEqual(page['raw_source_rotation'], 90)
            self.assertEqual(page['translated_rotation'], 0)
            self.assertTrue(page['source_rotation_pixels_verified'])
            self.assertTrue(Path(page['comparison']).is_file())

    def test_changed_real_dimensions_crop_and_rotation_are_rejected(self):
        for change, expected in [('media', 'MediaBox'), ('crop', 'CropBox'), ('direction', '旋转方向')]:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, target = self._fixture(root)
                wrong = root/'wrong.pdf'
                with fitz.open(target) as doc:
                    if change == 'media':
                        doc[0].set_mediabox(fitz.Rect(0, 0, 301, 200))
                    elif change == 'crop':
                        doc[0].set_cropbox(fitz.Rect(0, 0, 299, 200))
                    else:
                        # 180 degrees preserves the effective page dimensions.
                        doc[0].set_rotation(180)
                    doc.save(wrong)
                with self.assertRaisesRegex(ValueError, '几何'):
                    verify_unchanged(source, wrong, [])
                report = inspect_pdf_pair(source_pdf=source, translated_pdf=wrong,
                    output_dir=root/'qa', dpi=72, canonical_source_geometry=True)
                self.assertTrue(any(expected in error for error in report['errors']))

    def test_legacy_qa_keeps_raw_box_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = self._fixture(root)
            report = inspect_pdf_pair(source_pdf=source, translated_pdf=target,
                                      output_dir=root/'qa', dpi=72)
            self.assertIn('第1页 MediaBox 不一致', report['errors'])

