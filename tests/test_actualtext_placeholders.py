"""出版社内部公式 ID 不能替代原科学字符；修复前后画面必须逐像素相同。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pymupdf as fitz
from paperlocale.font_geometry import open_source_pdf, verify_canonical_source_pixels


class ActualTextTests(unittest.TestCase):
    def test_pixel_gate_rejects_a_missing_source_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.pdf'
            with fitz.open() as doc:
                doc.new_page().insert_text((50, 100), 'A line that must never disappear')
                doc.save(path)
            verify_canonical_source_pixels(path)
            broken = fitz.open(path)
            for ref in broken[0].get_contents():
                broken.update_stream(ref, b'')
            with patch('paperlocale.font_geometry.open_source_pdf', return_value=broken):
                with self.assertRaisesRegex(ValueError, '第1页源页归一化改变了原文像素'):
                    verify_canonical_source_pixels(path)

    def test_repeated_zero_width_and_decorated_ligature_tags(self):
        for actual in ['\u200b\u200b', '\u200bf\u200bi\u200b', 'Temperature\u200bC']:
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'source.pdf'
                with fitz.open() as doc:
                    page = doc.new_page(); page.insert_text((50, 100), 'fi')
                    ref = page.get_contents()[0]
                    tag = fitz.get_pdf_str(actual).encode('ascii')
                    doc.update_stream(ref, b'/Span <</ActualText ' + tag + b'>> BDC\n' +
                                      doc.xref_stream(ref) + b'\nEMC')
                    doc.save(path)
                with fitz.open(path) as before, open_source_pdf(path) as after:
                    expected = actual if actual.startswith('Temperature') else 'fi'
                    self.assertEqual(after[0].get_text().strip(), expected)
                    self.assertEqual(before[0].get_pixmap().samples, after[0].get_pixmap().samples)

    def test_text_operands_cross_content_stream_boundaries(self):
        # 出版社按大小拆分流时，文字数组和 TJ 可以落在相邻流中。
        # 清理另一处 ActualText 不能吞掉跨流行，也不能改变原字形画面。
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((50, 100), 'First line')
                first = page.get_contents()[0]
                program = doc.xref_stream(first)
                split = program.index(b'TJ')
                doc.update_stream(first, program[:split])
                second = doc.get_new_xref()
                doc.update_object(second, '<<>>')
                doc.update_stream(second, program[split:] +
                    b'\n/Span <</ActualText (inline-eq-IEq1)>> BDC\nEMC\n')
                doc.xref_set_key(page.xref, 'Contents', f'[{first} 0 R {second} 0 R]')
                doc.save(path)
            with fitz.open(path) as before, open_source_pdf(path) as after:
                self.assertIn('First line', after[0].get_text())
                self.assertEqual(before[0].get_pixmap().samples, after[0].get_pixmap().samples)

    def test_stream_and_structure_placeholders_preserve_ink(self):
        for structure, label in [(False, 'inline-eq-IEq1'), (True, 'inline-eq-IEq1'), (False, 'display-eq-Equ1'), (True, 'display-eq-Equ1')]:
            with self.subTest(structure=structure),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'source.pdf'
                with fitz.open() as doc:
                    page=doc.new_page();page.insert_text((50,100),'T')
                    content=page.get_contents()[0]
                    if structure:
                        def obj(value):
                            ref=doc.get_new_xref();doc.update_object(ref,value);return ref
                        root=obj('<< /Type /StructTreeRoot >>')
                        elem=obj(f'<< /Type /StructElem /S /Figure /P {root} 0 R /Pg {page.xref} 0 R /K 0 /ActualText ({label}) >>')
                        tree=obj(f'<< /Nums [0 [{elem} 0 R]] >>')
                        doc.xref_set_key(root,'K',f'[{elem} 0 R]');doc.xref_set_key(root,'ParentTree',f'{tree} 0 R')
                        doc.xref_set_key(doc.pdf_catalog(),'StructTreeRoot',f'{root} 0 R')
                        doc.xref_set_key(doc.pdf_catalog(),'MarkInfo','<< /Marked true >>')
                        doc.xref_set_key(page.xref,'StructParents','0')
                        prefix=b'/Figure <</MCID 0>> BDC\n'
                    else:
                        prefix=f'/Span <</ActualText ({label})>> BDC\n'.encode()
                    doc.update_stream(content,prefix+doc.xref_stream(content)+b'\nEMC')
                    doc.save(path)
                original=path.read_bytes()
                with fitz.open(path) as before,open_source_pdf(path) as after:
                    self.assertIn(label,before[0].get_text())
                    self.assertEqual(after[0].get_text().strip(),'T')
                    self.assertEqual(before[0].get_pixmap(matrix=fitz.Matrix(3,3)).samples,
                                     after[0].get_pixmap(matrix=fitz.Matrix(3,3)).samples)
                self.assertEqual(path.read_bytes(),original)

    def test_legitimate_actualtext_is_not_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page();page.insert_text((50,100),'T')
                content=page.get_contents()[0]
                doc.update_stream(content,b'/Span <</ActualText (Temperature)>> BDC\n'+doc.xref_stream(content)+b'\nEMC')
                doc.save(path)
            with open_source_pdf(path) as doc:
                self.assertIn('Temperature',doc[0].get_text())

    def test_unallocated_xref_slot_is_not_an_object(self):
        # 自造经典 xref 的编号 6 没有条目，/Size 仍合法覆盖到 8。
        # 不能把 1..Size 都当成可读取对象，也不能屏蔽真正对象损坏。
        objects={1:b'<< /Type /Catalog /Pages 2 0 R >>',
                 2:b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                 3:b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F 5 0 R >> >> /Contents 4 0 R >>',
                 5:b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
                 7:b'<< /Unreferenced true >>'}
        stream=b'BT /F 12 Tf 20 150 Td (T) Tj ET'
        objects[4]=b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream'
        data=bytearray(b'%PDF-1.7\n');offsets={}
        for number,obj in sorted(objects.items()):
            offsets[number]=len(data);data.extend(f'{number} 0 obj\n'.encode()+obj+b'\nendobj\n')
        offset=len(data);data.extend(b'xref\n0 6\n0000000000 65535 f \n')
        for n in range(1,6):data.extend(f'{offsets[n]:010d} 00000 n \n'.encode())
        data.extend(f'7 1\n{offsets[7]:010d} 00000 n \ntrailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n{offset}\n%%EOF\n'.encode())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source.pdf';path.write_bytes(data)
            with open_source_pdf(path) as doc:
                self.assertEqual(doc[0].get_text().strip(),'T')
