"""正文数值/图号不是书目编号；旧断点必须按旧规则校验，不能静默换分组。"""
import tempfile
from pathlib import Path
import unittest
import pymupdf as fitz
from paperlocale.source_layout import extract_layout, load_plan, save_json


class InlineReferenceNumbersTests(unittest.TestCase):
    def test_inline_negative_threshold_and_figure_number_remain_body(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((40,100), 'Values below - 2. These years include 1983 and 1988. See Fig. 6. Except 2016.', fontsize=10)
                doc.save(source)
            current=extract_layout(source, paragraph=True)
            block=next(b for b in current['blocks'] if 'Values below' in b['text'])
            self.assertEqual(block['kind'], 'body')
            self.assertEqual(current['reference_revision'], 2)
            legacy=extract_layout(source, paragraph=True, reference_line_starts=False)
            self.assertEqual(next(b for b in legacy['blocks'] if 'Values below' in b['text'])['kind'], 'reference')
            save_json(Path(folder)/'legacy.json',legacy)
            loaded,_=load_plan(source,Path(folder)/'legacy.json',paragraph=True)
            self.assertEqual(loaded,legacy)

    def test_real_multiline_numbered_entries_remain_protected(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((40,75), 'References', fontsize=12)
                page.insert_text((40,100), '1. Smith, A. Study of climate (2020).\n2. Jones, B. Another study (2021).', fontsize=10)
                doc.save(source)
            plan=extract_layout(source,paragraph=True)
            entries=[b for b in plan['blocks'] if 'Smith' in b['text'] or 'Jones' in b['text']]
            self.assertTrue(entries)
            self.assertTrue(all(b['kind']=='reference' for b in entries))
