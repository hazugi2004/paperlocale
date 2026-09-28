"""同名 CFF 子集必须由实际绘制的字符/GID 证据区分，不能任取字体。"""
from io import BytesIO
import unittest

import pymupdf as fitz
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen

from paperlocale.font_geometry import _source_anchor_glyphs


def fixture(swapped):
    """构造同名字体的两份真 CFF，交换字形编号或只改变轮廓宽度。"""
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    refs = []
    for index in range(2):
        order = ['.notdef', 'B', 'A'] if swapped and index else ['.notdef', 'A', 'B']
        builder = FontBuilder(1000, isTTF=False)
        builder.setupGlyphOrder(order)
        builder.setupCharacterMap({65: 'A', 66: 'B'})
        strings = {}
        for name in order:
            pen = T2CharStringPen(500, None)
            if name != '.notdef':
                width = 300 + index * 50
                pen.moveTo((0, 0)); pen.lineTo((width, 0))
                pen.lineTo((width, 700)); pen.lineTo((0, 700)); pen.closePath()
            strings[name] = pen.getCharString()
        builder.setupCFF('Fixture', {'FullName': 'Fixture', 'FamilyName': 'Fixture',
                                  'Weight': 'Regular'}, strings, {})
        builder.setupHorizontalMetrics({name: (500, 0) for name in order})
        buffer = BytesIO()
        builder.font['CFF '].cff.compile(buffer, builder.font)
        program = doc.get_new_xref()
        doc.update_object(program, '<< /Subtype /Type1C >>')
        doc.update_stream(program, buffer.getvalue())
        descriptor = doc.get_new_xref()
        prefix = 'AAAAAA' if index == 0 else 'BBBBBB'
        doc.update_object(descriptor, f'<< /Type /FontDescriptor /FontName /{prefix}+Fixture '
                          f'/Flags 32 /FontBBox [0 0 500 700] /ItalicAngle 0 /Ascent 800 '
                          f'/Descent -200 /CapHeight 700 /StemV 80 /FontFile3 {program} 0 R >>')
        ref = doc.get_new_xref()
        doc.update_object(ref, f'<< /Type /Font /Subtype /Type1 /BaseFont /{prefix}+Fixture '
                          f'/FirstChar 65 /LastChar 66 /Widths [500 500] '
                          f'/Encoding /WinAnsiEncoding /FontDescriptor {descriptor} 0 R >>')
        refs.append(ref)
    resources = int(doc.xref_get_key(page.xref, 'Resources')[1].split()[0])
    doc.xref_set_key(resources, 'Font', f'<< /F0 {refs[0]} 0 R /F1 {refs[1]} 0 R >>')
    stream = doc.get_new_xref()
    doc.update_object(stream, '<< >>')
    doc.update_stream(stream, b'BT /F0 10 Tf 1 0 0 1 30 100 Tm (AB) Tj ET\n'
                             b'BT /F1 10 Tf 1 0 0 1 30 70 Tm (AB) Tj ET')
    doc.xref_set_key(page.xref, 'Contents', f'{stream} 0 R')
    # 重新打开，确保 MuPDF 从最终 PDF 资源而非创建时缓存读取绘制记录。
    result = fitz.open(stream=doc.tobytes(), filetype='pdf')
    doc.close()
    return result, refs


class SourceFontIdentityTests(unittest.TestCase):
    def test_same_family_subsets_follow_actual_glyph_ids(self):
        doc, refs = fixture(True)
        with doc:
            cache = {}
            for index, block in enumerate(doc[0].get_text('rawdict')['blocks']):
                chars = [{'text': c['c'], 'origin': c['origin'], 'rect': c['bbox']}
                         for line in block['lines'] for s in line['spans'] for c in s['chars']]
                glyphs = _source_anchor_glyphs(doc[0], chars, cache)
                self.assertIsNotNone(glyphs)
                self.assertEqual([g['name'] for g in glyphs], ['A', 'B'])
                self.assertEqual({g['xref'] for g in glyphs}, {refs[index]})
                self.assertAlmostEqual(glyphs[0]['rect'].width, 3 + .5*index, places=4)

    def test_same_mapping_with_different_outlines_is_still_rejected(self):
        doc, _ = fixture(False)
        with doc:
            char = doc[0].get_text('rawdict')['blocks'][0]['lines'][0]['spans'][0]['chars'][0]
            self.assertIsNone(_source_anchor_glyphs(doc[0], [{
                'text': char['c'], 'origin': char['origin'], 'rect': char['bbox']}], {}))
