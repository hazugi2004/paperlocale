"""自造字体核验精确小数点、正常冒号和旧计划的语义隔离。

小圆点仅用已目视核验的几何指令重建，不保存论文或出版社字体程序。
真实extract/load用这些临时PDF运行，避免mock掩盖计划身份变化。
"""
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import copy
import hashlib
import tempfile
import unittest

import pymupdf as fitz
from fontTools import t1Lib
from fontTools.cffLib import CFFFontSet
from fontTools.misc.psCharStrings import T1CharString
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen

from paperlocale.font_geometry import _source_anchor_glyphs
from paperlocale.source_layout import extract_layout, load_plan, save_json
from paperlocale.source_symbols import REVIEWED_DECIMAL_OUTLINES, restore_verified_symbols
from test_corpus_encoding import fixture
from test_type1_geometry import program


POINT_SIGNATURE = 'ec06e4629f0944bbf2f97b7d5bead5409629361263ce74b794d06caa358c5079'


def _draw_point(pen, offset=0):
    """单闭合基线小圆点；offset只用于另造真正的双点冒号负例。"""
    pen.moveTo((139, offset))
    pen.curveTo((164, offset), (191, 18 + offset), (191, 52 + offset))
    pen.curveTo((191, 86 + offset), (164, 105 + offset), (139, 105 + offset))
    pen.curveTo((115, 105 + offset), (87, 86 + offset), (87, 52 + offset))
    pen.curveTo((87, 18 + offset), (115, offset), (139, offset))
    pen.closePath()


def _decimal_fixture(path, *, kind='cff', scale=.001, outline='point'):
    """构造文本映射为冒号、真实墨迹由参数决定的简单Type1临时PDF。"""
    fixture(path)
    matrix = [scale, 0, 0, scale, 0, 0]
    if kind == 'cff':
        cff = CFFFontSet()
        cff.decompile(BytesIO(program('cff', matrix)), None)
        top = cff[0]
        pen = T2CharStringPen(500, None)
        if outline == 'unknown':
            # 同字体名、同Unicode、同em，但轮廓不同，必须拒绝恢复。
            pen.moveTo((80, 0)); pen.lineTo((200, 0)); pen.lineTo((140, 180)); pen.closePath()
        else:
            _draw_point(pen)
            if outline == 'colon':
                _draw_point(pen, 300)
        top.CharStrings['P'] = pen.getCharString(private=top.Private)
        buffer = BytesIO()
        cff.compile(buffer, SimpleNamespace(recalcBBoxes=False))
        data, name = buffer.getvalue(), 'FixtureCFF'
    else:
        # 相同完整轮廓、1000em的PFA仍不是审定的CFF类型，须保持冒号。
        font = t1Lib.T1Font.__new__(t1Lib.T1Font)
        font.data, font.encoding = program('pfa', matrix), 'ascii'
        font.parse()
        font.font['CharStrings']['P'] = T1CharString(program=[
            0, 500, 'hsbw', 139, 0, 'rmoveto',
            25, 0, 27, 18, 0, 34, 'rrcurveto',
            0, 34, -27, 19, -25, 0, 'rrcurveto',
            -24, 0, -28, -19, 0, -34, 'rrcurveto',
            0, -34, 28, -18, 24, 0, 'rrcurveto', 'closepath', 'endchar'])
        data, name = font.createData(), 'FixturePFA'
    with fitz.open(path) as document:
        page = document[0]
        reference = page.get_fonts()[0][0]
        descriptor = int(document.xref_get_key(reference, 'FontDescriptor')[1].split()[0])
        stream = int(document.xref_get_key(descriptor, 'FontFile3')[1].split()[0])
        document.update_stream(stream, data)
        if kind == 'pfa':
            length1 = data.index(b'eexec ') + len(b'eexec ')
            length2 = len(data) - length1 - len(data[data.index(b'\n' + b'0' * 64):])
            document.update_object(stream, f'<< /Length1 {length1} /Length2 {length2} /Length3 0 >>')
            document.update_stream(stream, data)
            document.xref_set_key(descriptor, 'FontFile3', 'null')
            document.xref_set_key(descriptor, 'FontFile', f'{stream} 0 R')
        document.xref_set_key(reference, 'BaseFont', '/' + name)
        document.xref_set_key(descriptor, 'FontName', '/' + name)
        cmap = int(document.xref_get_key(reference, 'ToUnicode')[1].split()[0])
        document.update_stream(cmap, document.xref_stream(cmap).replace(b'E001', b'003A'))
        page.insert_text((40, 60), 'This paragraph explains water and vegetation.', fontsize=10)
        data = document.tobytes()
    path.write_bytes(data)


def _colon(raw):
    return next(c for b in raw for l in b.get('lines', []) for s in l['spans']
                for c in s['chars'] if c['c'] == ':')


def _identity(page, char):
    cache = {}
    glyph = _source_anchor_glyphs(page, [{'text': char['c'], 'rect': char['bbox'],
                                        'origin': char['origin']}], cache)[0]
    _, outlines, scale, kind = cache[glyph['xref']]
    pen = RecordingPen()
    outlines[glyph['name']].draw(pen)
    return kind, 1 / scale, hashlib.sha256(repr(pen.value).encode()).hexdigest()


class SymbolRevision6Tests(unittest.TestCase):
    def test_exact_point_requires_switch_and_preserves_native_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'point.pdf'
            _decimal_fixture(path)
            with fitz.open(path) as document:
                page = document[0]
                raw = page.get_text('rawdict')['blocks']
                char = _colon(raw)
                before = copy.deepcopy(char)
                self.assertEqual(_identity(page, char), ('cff', 1000, POINT_SIGNATURE))
                self.assertEqual(REVIEWED_DECIMAL_OUTLINES[_identity(page, char)], '.')
                restore_verified_symbols(page, raw, {}, reviewed_brackets=True)
                self.assertEqual(char, before)
                restore_verified_symbols(page, raw, {}, reviewed_decimals=True)
                self.assertEqual((char['c'], char['native_text']), ('.', ':'))
                self.assertEqual((char['bbox'], char['origin']), (before['bbox'], before['origin']))

    def test_real_colon_unknown_outline_wrong_em_and_type_are_not_decimals(self):
        cases = [{'outline': 'colon'}, {'outline': 'unknown'}, {'scale': .002}, {'kind': 'pfa'}]
        for options in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'negative.pdf'
                _decimal_fixture(path, **options)
                with fitz.open(path) as document:
                    raw = document[0].get_text('rawdict')['blocks']
                    char = _colon(raw)
                    identity = _identity(document[0], char)
                    if 'scale' in options:
                        self.assertEqual(identity, ('cff', 500, POINT_SIGNATURE))
                    if 'kind' in options:
                        self.assertEqual(identity, ('pfa', 1000, POINT_SIGNATURE))
                    restore_verified_symbols(document[0], raw, {}, reviewed_decimals=True)
                    self.assertEqual(char['c'], ':')
                    self.assertNotIn('native_text', char)

    def test_normal_text_colon_stays_colon(self):
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((40, 60), 'Normal colon: time 12:30 and a: b.')
            raw = page.get_text('rawdict')['blocks']
            before = copy.deepcopy(raw)
            restore_verified_symbols(page, raw, {}, reviewed_decimals=True)
            self.assertEqual(raw, before)

    def test_cache_isolates_switch_and_noncolon_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'point.pdf'
            _decimal_fixture(path)
            with fitz.open(path) as document:
                page, cache = document[0], {}
                # 同一原点/GID的控制码候选先产生旧规则None；新开关不能被
                # 旧缓存遮住。反向再调用旧规则，也不能借用新增点映射。
                control = page.get_text('rawdict')['blocks']
                _colon(control)['c'] = '\x01'
                restore_verified_symbols(page, control, cache, reviewed_brackets=True)
                modern = page.get_text('rawdict')['blocks']
                restore_verified_symbols(page, modern, cache, reviewed_brackets=True, reviewed_decimals=True)
                self.assertNotIn(':', ''.join(c['c'] for b in modern for l in b.get('lines', [])
                                             for s in l['spans'] for c in s['chars']))
                legacy = page.get_text('rawdict')['blocks']
                restore_verified_symbols(page, legacy, cache, reviewed_brackets=True)
                self.assertEqual(_colon(legacy)['c'], ':')
                # 即使新开关启用，映射也只接纳冒号，不能污染同GID控制码。
                alias = page.get_text('rawdict')['blocks']
                _colon(alias)['c'] = '\x01'
                restore_verified_symbols(page, alias, cache, reviewed_brackets=True, reviewed_decimals=True)
                self.assertTrue(any(c['c'] == '\x01' for b in alias for l in b.get('lines', [])
                                    for s in l['spans'] for c in s['chars']))

    def test_real_extract_and_load_keep_revisions_zero_through_five(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'point.pdf'
            _decimal_fixture(source)
            for revision in range(7):
                with self.subTest(revision=revision):
                    plan = extract_layout(source, paragraph=True, symbol_revision=revision)
                    text = ' '.join(b['text'] for b in plan['blocks'])
                    self.assertEqual(':' in text, revision < 6)
                    path = root / f'plan-{revision}.json'
                    save_json(path, plan)
                    checked, _ = load_plan(source, path, paragraph=True)
                    self.assertEqual(checked, plan)
            modern = extract_layout(source, paragraph=True)
            self.assertEqual(modern['symbol_revision'], 6)
