"""图注边界只能在条带不含图形时收紧，不能隐藏真实图内变化。"""
import tempfile
import unittest
from pathlib import Path
import pymupdf as fitz
from paperlocale.safe_text import separate_caption_edges


class CaptionEdgeTests(unittest.TestCase):
    def test_only_caption_strip_can_be_removed(self):
        for has_graphic in [False, True]:
            with self.subTest(has_graphic=has_graphic), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / 'source.pdf'
                with fitz.open() as doc:
                    page = doc.new_page()
                    page.insert_text((40, 100), 'Figure caption', fontsize=10)
                    box = list(page.get_text('blocks')[0][:4])
                    if has_graphic:
                        page.draw_line((20, box[1]+.5), (30, box[1]+.5), width=.2)
                    doc.save(source)
                region = {'page': 1, 'kind': 'figure', 'rect': [10, 10, 200, box[1]+1]}
                caption = {'page': 1, 'kind': 'caption', 'rect': box}
                result = separate_caption_edges(source, [region], [caption])[0]
                self.assertEqual(result['rect'][3], region['rect'][3] if has_graphic else box[1])
                self.assertEqual(region['rect'][3], box[1]+1)
