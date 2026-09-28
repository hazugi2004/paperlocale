"""用自造字体验证原 Type 1 程序重放，不依赖系统或受版权保护的论文字体。"""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz
from fontTools import t1Lib
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.psCharStrings import T1CharString
from fontTools.pens.t2CharStringPen import T2CharStringPen

from paperlocale.font_geometry import _source_anchor_glyphs
from paperlocale.paragraph_layout import write_inline, verify_inline


def program(kind, matrix):
    """P 使用独有方框轮廓；PFA 故意让字典次序与原生 GID 次序相反。"""
    if kind == 'pfa':
        font = t1Lib.T1Font.__new__(t1Lib.T1Font)
        font.encoding = 'ascii'
        font.font = {'FontName': 'FixturePFA', 'FontType': 1, 'PaintType': 0,
                     'FontMatrix': matrix, 'FontBBox': [0, 0, 500, 700],
                     'Encoding': ['.notdef'] * 256,
                     'Private': {'RD': t1Lib.RD_value, 'ND': t1Lib.ND_values[0],
                                 'NP': t1Lib.PD_values[0], 'lenIV': 4, 'Subrs': []},
                     'CharStrings': {
                         'P': T1CharString(program=[0, 500, 'hsbw', 50, 0, 'rmoveto',
                              400, 0, 'rlineto', 0, 700, 'rlineto', -400, 0, 'rlineto',
                              'closepath', 'endchar']),
                         '.notdef': T1CharString(program=[0, 500, 'hsbw', 'endchar'])}}
        font.font['Encoding'][80] = 'P'
        return font.createData()
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
    builder.setupCFF('FixtureCFF', {'FullName': 'FixtureCFF', 'FamilyName': 'Fixture',
                                  'Weight': 'Regular', 'FontMatrix': matrix}, strings, {})
    data = BytesIO()
    builder.font['CFF '].cff.compile(data, builder.font)
    return data.getvalue()


class Type1GeometryTests(unittest.TestCase):
    def replay(self, kind, matrix):
        data = program(kind, matrix)
        with tempfile.TemporaryDirectory() as folder, fitz.open() as doc:
            page = doc.new_page()
            def obj(value, stream=None):
                ref = doc.get_new_xref()
                doc.update_object(ref, value)
                if stream is not None:
                    doc.update_stream(ref, stream)
                return ref
            name = 'FixturePFA' if kind == 'pfa' else 'FixtureCFF'
            # 明确构造简单 Type 1；insert_font 会自动改用 CID 容器，无法
            # 覆盖真实论文的 /FontFile + /Type1 资源路径。
            if kind == 'pfa':
                length1 = data.index(b'eexec ') + len(b'eexec ')
                length2 = len(data) - length1 - len(data[data.index(b'\n' + b'0'*64):])
                stream = obj(f'<< /Length1 {length1} /Length2 {length2} /Length3 0 >>', data)
                file_key = 'FontFile'
            else:
                stream = obj('<< /Subtype /Type1C >>', data)
                file_key = 'FontFile3'
            descriptor = obj(f'<< /Type /FontDescriptor /FontName /{name} /Flags 4 '
                             f'/FontBBox [0 0 500 700] /ItalicAngle 0 /Ascent 700 /Descent 0 '
                             f'/CapHeight 700 /StemV 80 /{file_key} {stream} 0 R >>')
            ref = obj(f'<< /Type /Font /Subtype /Type1 /BaseFont /{name} '
                      f'/FirstChar 80 /LastChar 80 /Widths [{500*matrix[0]*1000}] '
                      f'/Encoding /WinAnsiEncoding /FontDescriptor {descriptor} 0 R >>')
            doc.xref_set_key(page.xref, 'Resources', f'<< /Font << /F {ref} 0 R >> >>')
            content = obj('<<>>', f'BT /F 20 Tf 1 0 0 1 100 {page.rect.height-100} Tm <50> Tj ET'.encode())
            doc.xref_set_key(page.xref, 'Contents', f'{content} 0 R')
            # 重开后再提取，避免对象缓存掩盖字体落盘后的资源差异。
            with fitz.open(stream=doc.tobytes(), filetype='pdf') as source:
                page = source[0]
                chars = [{'text': c['c'], 'rect': c['bbox'], 'origin': c['origin']}
                         for b in page.get_text('rawdict')['blocks'] for l in b['lines']
                         for s in l['spans'] for c in s['chars']]
                glyphs = _source_anchor_glyphs(page, chars, {})
                self.assertEqual(len(glyphs), 1)
                self.assertEqual(glyphs[0]['name'], 'P')
                expected = [100+50*20*matrix[0], 100-700*20*matrix[0],
                            100+450*20*matrix[0], 100]
                for actual, target in zip(glyphs[0]['rect'], expected):
                    self.assertAlmostEqual(actual, target, places=4)
                before = page.get_pixmap(matrix=fitz.Matrix(3, 3)).samples
                original_program = source.extract_font(ref)[3]
                for content in page.get_contents():
                    source.update_stream(content, b'')
                item = {'page': 1, 'shift': [0, 0], 'inline_anchor':
                        {'page': 1, 'text': 'P', 'glyphs': glyphs}}
                write_inline(page, item, {})
                target = Path(folder) / 'replay.pdf'
                source.save(target)
                verify_inline(target, [item])
                with fitz.open(target) as result:
                    self.assertEqual(result[0].get_pixmap(matrix=fitz.Matrix(3, 3)).samples, before)
                    self.assertEqual(result.extract_font(ref)[3], original_program)
                    self.assertEqual(result[0].get_text().strip(), 'P')

    def test_pfa_reuses_actual_gid_and_original_font_program(self):
        self.replay('pfa', [.001, 0, 0, .001, 0, 0])

    def test_cff_uses_original_nonstandard_units(self):
        self.replay('cff', [1/2048, 0, 0, 1/2048, 0, 0])
