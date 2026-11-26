"""验证真实 PDF 中共享字形和补充平面字符的逐字回读。"""
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
import pymupdf as fitz
from pypdf import PdfReader

from paperlocale.qa import _extract_text
from paperlocale.unicode_fonts import (insert_text_exact, repair_default_tounicode,
                                       supplement_default_font, unicode_glyph_aliases)
from paperlocale.workflow import _subset_repair_font


def alias_font(supplementary=(0x1D6F9, 0x1D713)):
    """构造含共享横线、科学负号及补充平面字符的最小测试字体。"""
    builder = FontBuilder(1000, isTTF=True)
    order = ['.notdef', 'A', 'B', 'dash', 'minus', 'italicPsiUpper', 'italicPsiLower']
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({65: 'A', 66: 'B', 0x2012: 'dash',
                               0x2013: 'dash', 0x2212: 'minus',
                               supplementary[0]: 'italicPsiUpper', supplementary[1]: 'italicPsiLower'})
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        pen.moveTo((50, 300)); pen.lineTo((450, 300))
        pen.lineTo((450, 350 if name != 'minus' else 400))
        pen.lineTo((50, 350 if name != 'minus' else 400)); pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (500, 0) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'AliasFixture', 'styleName': 'Regular'})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200,
                    usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    output = BytesIO(); builder.save(output)
    return output.getvalue()


class UnicodeFontTests(unittest.TestCase):
    def test_alias_detection_does_not_equate_scientific_minus(self):
        self.assertEqual(unicode_glyph_aliases(alias_font()), {'‒': 3, '–': 3, '𝛹': 5, '𝜓': 6})

    def test_default_font_supplements_latin_and_math_for_strict_pdf_readback(self):
        primary_buffer = alias_font((0x103FF, 0x10400))
        primary = fitz.Font(fontbuffer=primary_buffer)
        self.assertIsNone(supplement_default_font(primary, 'AB‒'))
        text = 'AŠ𝛹𝜓B'
        with TemporaryDirectory() as directory:
            base = Path(directory)/'base.ttf'
            base.write_bytes(primary_buffer)
            with self.assertRaisesRegex(ValueError, '缺少.*字形'):
                _subset_repair_font(base, text)
            latin = Path(directory)/'latin.ttf'
            latin.write_bytes(alias_font((0x0160, 0x10400)))
            # 基础测试环境没有 layout 依赖。只替换字体来源，后续合并与 PDF 回读仍然实测。
            asset_module = ModuleType('babeldoc.assets.assets')
            asset_module.get_font_and_metadata = Mock(return_value=(latin, {}))
            with patch.dict(sys.modules, {asset_module.__name__: asset_module}):
                data = supplement_default_font(primary, text, bold=True)
            asset_module.get_font_and_metadata.assert_called_once_with('NotoSerif-Bold.ttf')
            with TTFont(BytesIO(primary_buffer)) as before, TTFont(BytesIO(data)) as after:
                original, added = before.getBestCmap(), after.getBestCmap()
                self.assertEqual(set(added)-set(original), set(map(ord, 'Š𝛹𝜓')))
                self.assertEqual({cp: added[cp] for cp in original}, original)
                self.assertEqual(after['hmtx']['A'], before['hmtx']['A'])
            reduced, evidence = _subset_repair_font(base, text, font_data=data)
            self.assertIn('font_program_sha256_before_subset', evidence)
        final = fitz.Font(fontbuffer=reduced)
        self.assertEqual(final.text_length(text), fitz.Font(fontbuffer=data).text_length(text))
        with fitz.open() as document:
            page = document.new_page()
            ref = page.insert_font(fontname='Supplemented', fontbuffer=reduced)
            aliases = unicode_glyph_aliases(reduced)
            repair_default_tounicode(document, ref, aliases)
            insert_text_exact(page, (40, 100), text, fontname='Supplemented',
                              font_xref=ref, font=final, fontsize=12, aliases=aliases, font_refs={})
            pdf = document.tobytes()
        with fitz.open(stream=pdf, filetype='pdf') as saved:
            self.assertEqual(saved[0].get_text().strip(), text)
        self.assertEqual(_extract_text(PdfReader(BytesIO(pdf)).pages[0]), (text, 0))

    def test_saved_pdf_roundtrips_both_aliases_and_minus(self):
        buffer = alias_font(); font = fitz.Font(fontbuffer=buffer)
        text = 'A‒B–A−B‒–'
        with fitz.open() as document:
            page = document.new_page()
            reference = page.insert_font(fontname='Fixture', fontbuffer=buffer)
            refs = {}
            insert_text_exact(page, (40, 100), text, fontname='Fixture',
                              font_xref=reference, font=font, fontsize=12,
                              aliases=unicode_glyph_aliases(buffer), font_refs=refs)
            # 字体程序、字宽表、CID映射仍引用同一对象，只改变检索映射。
            self.assertEqual(len(refs), 2)
            for cloned in refs.values():
                self.assertEqual(document.xref_get_key(reference, 'DescendantFonts'),
                                 document.xref_get_key(cloned, 'DescendantFonts'))
            with fitz.open(stream=document.tobytes(), filetype='pdf') as saved:
                self.assertEqual(saved[0].get_text().strip(), text)
                traces = [c for span in saved[0].get_texttrace() for c in span['chars']]
                self.assertEqual([c[1] for c in traces], [1, 3, 2, 3, 1, 4, 2, 3, 3])
                self.assertEqual([round(c[2][0], 4) for c in traces],
                                 [40 + 6 * i for i in range(len(text))])

    def test_supplementary_symbols_roundtrip_with_utf16_surrogate_pairs(self):
        buffer = alias_font()
        font = fitz.Font(fontbuffer=buffer)
        text = 'A𝛹B𝜓A'
        with fitz.open() as document:
            page = document.new_page()
            reference = page.insert_font(fontname='Fixture', fontbuffer=buffer)
            refs = {}
            repair_default_tounicode(document, reference, unicode_glyph_aliases(buffer))
            insert_text_exact(page, (40, 100), text, fontname='Fixture',
                              font_xref=reference, font=font, fontsize=12,
                              aliases=unicode_glyph_aliases(buffer), font_refs=refs)
            pdf = document.tobytes()
            with fitz.open(stream=pdf, filetype='pdf') as saved:
                self.assertEqual(saved[0].get_text().strip(), text)
                default = int(saved.xref_get_key(reference, 'ToUnicode')[1].split()[0])
                self.assertIn(b'<0001> <0002> <0041>', saved.xref_stream(default))
                for character, code, value in [('𝛹', b'0005', b'd835def9'), ('𝜓', b'0006', b'd835df13')]:
                    cmap = int(saved.xref_get_key(refs[reference, character], 'ToUnicode')[1].split()[0])
                    self.assertIn(b'<' + code + b'> <' + value + b'>', saved.xref_stream(default))
                    self.assertIn(b'<' + code + b'> <' + value + b'>', saved.xref_stream(cmap))
            self.assertEqual(_extract_text(PdfReader(BytesIO(pdf)).pages[0]), (text, 0))

    def test_default_tounicode_range_can_cross_surrogate_boundary(self):
        codepoints = (0x103FF, 0x10400)
        buffer = alias_font(codepoints)
        text = 'AB' + ''.join(map(chr, codepoints))
        with fitz.open() as document:
            page = document.new_page()
            reference = page.insert_font(fontname='Fixture', fontbuffer=buffer)
            repair_default_tounicode(document, reference, unicode_glyph_aliases(buffer))
            page.insert_text((40, 100), text, fontname='Fixture')
            pdf = document.tobytes()
        self.assertEqual(_extract_text(PdfReader(BytesIO(pdf)).pages[0]), (text, 0))
        with fitz.open(stream=pdf, filetype='pdf') as saved:
            self.assertEqual(saved[0].get_text().strip(), text)

    def test_plain_text_uses_original_font_and_cross_page_alias_reuses_dictionary(self):
        buffer = alias_font(); font = fitz.Font(fontbuffer=buffer)
        with fitz.open() as document:
            refs = {}
            for text in ['AB', '‒', '‒']:
                page = document.new_page()
                reference = page.insert_font(fontname='Fixture', fontbuffer=buffer)
                insert_text_exact(page, (40, 100), text, fontname='Fixture',
                                  font_xref=reference, font=font, fontsize=12,
                                  aliases=unicode_glyph_aliases(buffer), font_refs=refs)
            self.assertEqual(len(refs), 1)
            with fitz.open(stream=document.tobytes(), filetype='pdf') as saved:
                self.assertEqual([p.get_text().strip() for p in saved], ['AB', '‒', '‒'])


if __name__ == '__main__':
    unittest.main()
