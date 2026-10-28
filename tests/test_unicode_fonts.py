"""真实 PDF 字形/检索回归：不同 Unicode 共用 GID 时仍逐字可回读。"""
from io import BytesIO
import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
import pymupdf as fitz

from paperlocale.unicode_fonts import unicode_glyph_aliases, insert_text_exact


def alias_font():
    """自造字体：两种横线共享轮廓，科学负号另用字形，不复制商业字体。"""
    builder = FontBuilder(1000, isTTF=True)
    order = ['.notdef', 'A', 'B', 'dash', 'minus']
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({65: 'A', 66: 'B', 0x2012: 'dash',
                               0x2013: 'dash', 0x2212: 'minus'})
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
        self.assertEqual(unicode_glyph_aliases(alias_font()), {'‒': 3, '–': 3})

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
