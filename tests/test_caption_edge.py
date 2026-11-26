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

    def test_only_standalone_continuation_label_can_trim_a_table(self):
        cases = [('table', '(Continues)', False, True), ('figure', '(Continues)', False, False),
                 ('table', 'Text (Continues)', False, False), ('table', '(Continues)', True, False)]
        for kind, text, other_text, should_trim in cases:
            with self.subTest(kind=kind, text=text, other_text=other_text), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / 'source.pdf'
                with fitz.open() as doc:
                    page = doc.new_page()
                    page.insert_text((95, 100), text, fontsize=10)
                    box = list(page.get_text('blocks')[0][:4])
                    if other_text:
                        page.insert_text((40, 100), 'Cell', fontsize=10)
                    doc.save(source)
                region = {'page': 1, 'kind': kind, 'rect': [10, 10, 200, box[1]+1]}
                label = {'page': 1, 'kind': 'body', 'text': text, 'rect': box}
                result = separate_caption_edges(source, [region], [label])[0]
                self.assertEqual(result['rect'][3], box[1] if should_trim else region['rect'][3])

    def test_table_top_is_trimmed_only_across_the_caption(self):
        for obstruction in ('none', 'rule', 'cell', 'body label', 'figure'):
            with self.subTest(obstruction=obstruction), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / 'source.pdf'
                with fitz.open() as doc:
                    page = doc.new_page()
                    page.insert_text((40, 100), 'Table 1. Observed taxa', fontsize=10)
                    box = list(page.get_text('blocks')[0][:4])
                    edge = box[3] - 2
                    page.draw_line((20, 110), (300, 110), width=.2)
                    page.insert_text((40, 125), 'First row', fontsize=10)
                    if obstruction == 'rule':
                        page.draw_line((20, edge + 1), (300, edge + 1), width=.2)
                    if obstruction == 'cell':
                        page.insert_text((250, 100), 'Cell', fontsize=10)
                    doc.save(source)
                region = {'page': 1, 'kind': 'figure' if obstruction == 'figure' else 'table',
                          'rect': [10, edge, 300, 200]}
                label = {'page': 1, 'kind': 'body' if obstruction == 'body label' else 'caption', 'rect': box}
                result = separate_caption_edges(source, [region], [label])[0]
                self.assertEqual(result['rect'][1], box[3] if obstruction == 'none' else edge)
                self.assertEqual(result['rect'][3], 200)
                self.assertEqual(region['rect'][1], edge)

    def test_csiro_box_contact_clearance_requires_native_evidence(self):
        for obstruction in ('none', 'rule', 'other text', 'no box title', 'other journal'):
            with self.subTest(obstruction=obstruction), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / 'source.pdf'
                with fitz.open() as doc:
                    page = doc.new_page()
                    title = 'Box 1. Sampling traits across open ecosystems'
                    leadin = 'The corresponding authors (and email addresses) are as follows: {AB}, A. Baker (a@example.org);'
                    page.insert_text((30, 78), title, fontsize=11)
                    page.insert_text((30, 100), leadin, fontsize=9)
                    rows = page.get_text('blocks')
                    title_box, line_box = [list(next(b for b in rows if b[4].strip() == s)[:4])
                                           for s in (title, leadin)]
                    edge = line_box[3] - 3
                    if obstruction == 'rule':
                        page.draw_line((20, edge + 1), (300, edge + 1), width=.2)
                    if obstruction == 'other text':
                        page.insert_text((510, 101.5), 'Data', fontsize=9)
                    page.insert_text((30, 119), '{CD}, C. Diaz (c@example.org);', fontsize=9)
                    doc.save(source)
                region = {'page': 1, 'kind': 'figure', 'rect': [20, edge, 550, 250]}
                blocks = [{'page': 1, 'kind': 'body', 'text': leadin, 'rect': line_box}]
                if obstruction != 'no box title':
                    blocks.append({'page': 1, 'kind': 'caption', 'text': title, 'rect': title_box})
                family = 'wiley' if obstruction == 'other journal' else 'csiro'
                result = separate_caption_edges(source, [region], blocks, journal_family=family)[0]
                self.assertEqual(result['rect'][1], line_box[3] if obstruction == 'none' else edge)
                self.assertEqual(result['rect'][3], 250)
