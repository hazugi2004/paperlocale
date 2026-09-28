"""长空格页眉不能成为待译正文；旧计划必须仍可复现以安全迁移缓存。"""
from pathlib import Path
import tempfile
import unittest
import pymupdf as fitz
from paperlocale.source_layout import extract_layout, load_plan, save_json


class HeaderWhitespaceTests(unittest.TestCase):
    def test_wide_abstract_does_not_reorder_lower_two_columns(self):
        from paperlocale.paragraph_layout import paragraph_groups
        def block(name, rect, text):
            return {'id': name, 'page': 1, 'page_width': 600, 'kind': 'body',
                    'rect': rect, 'text': text, 'parts': [
                        {'fixed': False, 'size': 10, 'bold': False, 'rect': rect, 'text': text}]}
        blocks = [block('keywords', [40, 180, 150, 230], 'Keywords'),
                  block('abstract', [200, 180, 560, 450], 'Abstract'),
                  block('left', [40, 540, 290, 700], 'This introductory paragraph continues with three'),
                  block('right', [310, 500, 560, 650], 'events in the study region.'),
                  block('next', [320, 648, 560, 665], 'Existing studies examine drought.')]
        groups = paragraph_groups(blocks)
        self.assertEqual([key for group in groups for key in group],
                         ['keywords', 'abstract', 'left', 'right', 'next'])
        self.assertIn(['left', 'right'], groups)
        legacy = paragraph_groups(blocks, mixed_first_page=False)
        self.assertNotEqual([key for group in legacy for key in group],
                            ['keywords', 'abstract', 'left', 'right', 'next'])

    def test_short_header_with_page_wide_spaces_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((40, 42), 'C. Zhao et al.' + ' ' * 170, fontsize=7)
                page.insert_text((40, 100), 'The main paragraph describes climate changes.', fontsize=10)
                doc.save(source)
            detections = [{'page': 1, 'kind': 'abandon', 'rect': [39, 35, 82, 45]}]
            current = extract_layout(source, detections, paragraph=True)
            self.assertEqual(next(b for b in current['blocks'] if b['text']=='C. Zhao et al.')['kind'], 'header-footer')
            self.assertEqual(next(b for b in current['blocks'] if 'main paragraph' in b['text'])['kind'], 'body')
            self.assertEqual(current['geometry_revision'], 2)
            legacy = extract_layout(source, detections, paragraph=True, content_geometry=False)
            self.assertEqual(next(b for b in legacy['blocks'] if b['text']=='C. Zhao et al.')['kind'], 'body')
            save_json(Path(directory) / 'legacy.json', legacy)
            loaded, _ = load_plan(source, Path(directory) / 'legacy.json', detections, paragraph=True)
            self.assertEqual(loaded, legacy)
