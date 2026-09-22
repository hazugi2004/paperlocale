"""用自造 CFF 和一字形多 Unicode 映射验证公式重放，不使用论文字体。"""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from paperlocale.font_geometry import _source_anchor_glyphs
from paperlocale.paragraph_layout import write_inline, verify_inline


class InlineLigatureTests(unittest.TestCase):
    def test_one_outline_keeps_multiple_unicode_characters(self):
        # 方块轮廓便于逐像素比较；两个 Unicode 字符只对应一个实际字形。
        builder = FontBuilder(1000, isTTF=False)
        builder.setupGlyphOrder(['.notdef', 'P'])
        builder.setupCharacterMap({80: 'P'})
        strings = {}
        for name in ['.notdef', 'P']:
            pen = T2CharStringPen(500, None)
            if name == 'P':
                pen.moveTo((50, 0)); pen.lineTo((450, 0))
                pen.lineTo((450, 700)); pen.lineTo((50, 700)); pen.closePath()
            strings[name] = pen.getCharString()
        font_name = 'ABCDEF+FixtureMathCalligraphy-Regular'
        builder.setupCFF(font_name, {'FullName': 'FixtureMath', 'FamilyName': 'FixtureMath',
                                       'Weight': 'Regular'}, strings, {})
        data = BytesIO()
        builder.font['CFF '].cff.compile(data, builder.font)
        with tempfile.TemporaryDirectory() as folder, fitz.open() as doc:
            page = doc.new_page()
            def obj(value, stream=None):
                ref = doc.get_new_xref()
                doc.update_object(ref, value)
                if stream is not None:
                    doc.update_stream(ref, stream)
                return ref
            program = obj('<< /Subtype /Type1C >>', data.getvalue())
            descriptor = obj(f'<< /Type /FontDescriptor /FontName /{font_name} /Flags 4 '
                             f'/FontBBox [0 0 500 700] /ItalicAngle 0 /Ascent 700 /Descent 0 '
                             f'/CapHeight 700 /StemV 80 /FontFile3 {program} 0 R >>')
            # 非 BMP 数学字符的重复映射复现本次实际输入；必须保持文本而不重绘两次。
            encoded = '𝑃𝑃'.encode('utf-16-be').hex()
            cmap = obj('<<>>', ('begincmap /CMapType 2 def '
                '1 begincodespacerange <00> <ff> endcodespacerange '
                f'1 beginbfchar <50> <{encoded}> endbfchar endcmap').encode())
            font = obj(f'<< /Type /Font /Subtype /Type1 /BaseFont /{font_name} '
                       f'/FirstChar 80 /LastChar 80 /Widths [500] /Encoding /WinAnsiEncoding '
                       f'/FontDescriptor {descriptor} 0 R /ToUnicode {cmap} 0 R >>')
            doc.xref_set_key(page.xref, 'Resources', f'<< /Font << /F {font} 0 R >> >>')
            content = obj('<<>>', f'BT /F 10 Tf 1 0 0 1 100 {page.rect.height-100} Tm <50> Tj ET'.encode())
            doc.xref_set_key(page.xref, 'Contents', f'{content} 0 R')
            page = doc.reload_page(page)
            self.assertEqual(page.get_texttrace()[0]['font'], font_name[:31][7:])
            chars = [{'text': c['c'], 'rect': c['bbox'], 'origin': c['origin']}
                     for b in page.get_text('rawdict')['blocks'] for l in b['lines']
                     for s in l['spans'] for c in s['chars']]
            duplicate = obj(doc.xref_object(font))
            doc.xref_set_key(page.xref, 'Resources/Font/Duplicate', f'{duplicate} 0 R')
            glyphs = _source_anchor_glyphs(page, chars, {})
            self.assertEqual(len(glyphs), 1)
            self.assertEqual(glyphs[0]['text'], '𝑃𝑃')
            self.assertIsNone(_source_anchor_glyphs(page, chars[1:], {}))
            # 同名但描述符不同不能凭名称归并；拒绝可能不同的字形程序。
            changed = obj(doc.xref_object(descriptor))
            doc.xref_set_key(changed, 'Flags', '6')
            doc.xref_set_key(duplicate, 'FontDescriptor', f'{changed} 0 R')
            self.assertIsNone(_source_anchor_glyphs(page, chars, {}))
            doc.xref_set_key(duplicate, 'FontDescriptor', f'{descriptor} 0 R')
            # 删除检查也必须处理 rawdict 与 trace 续字符文本不一致；
            # 只有片段内包含主字形时才可用它证明零宽续字符会一起删除。
            from paperlocale.safe_text import safe_erase_rectangles
            source = Path(folder) / 'source.pdf'
            doc.save(source)
            mismatched = [chars[0], {**chars[1], 'text': 'Q'}]
            part = {'page': 1, 'rect': [100, 90, 105, 103], 'chars': mismatched}
            self.assertTrue(safe_erase_rectangles([part], [], source))
            with self.assertRaisesRegex(ValueError, '没有安全删除范围'):
                safe_erase_rectangles([{**part, 'chars': mismatched[1:]}], [], source)
            before = page.get_pixmap().samples
            # 清空旧绘制流后在同一点重放；逐像素及落盘字形检查同时通过才成功。
            doc.update_stream(content, b'')
            item = {'page': 1, 'shift': [0, 0], 'inline_anchor':
                    {'page': 1, 'text': '𝑃𝑃', 'glyphs': glyphs}}
            write_inline(page, item, {})
            target = Path(folder) / 'replayed.pdf'
            doc.save(target)
            verify_inline(target, [item])
            with fitz.open(target) as result:
                self.assertEqual(result[0].get_pixmap().samples, before)
                self.assertEqual(result[0].get_text().strip(), '𝑃𝑃')
