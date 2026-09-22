"""异常 OCR 必须局部、可复核；失败不能变成对原文的猜测性替换。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pymupdf as fitz
from paperlocale.local_ocr import anomalous_lines, diagnose_lines
from paperlocale.source_layout import normalize_traced_spaces

class LocalOCRTest(unittest.TestCase):
    def test_only_unresolved_line_is_cropped_and_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp, fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((30,50), 'Soil moisture changed.')
            page.insert_text((30,90), 'Healthy extraction.')
            raw = page.get_text('rawdict')['blocks']
            char = raw[0]['lines'][0]['spans'][0]['chars'][0]
            char['c'] = '\x07'
            lines = anomalous_lines(raw)
            self.assertEqual(len(lines), 1)
            with patch('paperlocale.local_ocr.recognize_crop', return_value={
                    'engine':'test','text':'Soil moisture changed.','confidence':.99}) as engine:
                report = diagnose_lines(page, lines, Path(tmp))
            self.assertEqual(engine.call_count,1)
            self.assertLess(fitz.Rect(report[0]['rect']).height, page.rect.height/10)
            self.assertTrue(Path(report[0]['image']).is_file())
            self.assertEqual(char['c'],'\x07')
            self.assertFalse(report[0]['applied'])

    def test_trace_proven_space_does_not_require_ocr(self):
        with fitz.open() as doc:
            page=doc.new_page();page.insert_text((30,50),'A B')
            raw=page.get_text('rawdict')['blocks']
            char=raw[0]['lines'][0]['spans'][0]['chars'][1];char['c']='\x07'
            normalize_traced_spaces(raw,page.get_texttrace())
            self.assertEqual(char['c'],' ')
            self.assertEqual(anomalous_lines(raw),[])
