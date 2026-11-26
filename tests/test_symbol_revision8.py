"""自造 CFF 字体覆盖重叠重音的原坐标、NFC 文字和新旧计划隔离。"""
import hashlib
from io import BytesIO
from unittest.mock import patch

import pymupdf as fitz
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen

from paperlocale.font_geometry import _source_anchor_glyphs
from paperlocale.paragraph_layout import bind_inline_glyphs, verify_inline, write_inline
from paperlocale.source_layout import _parts
from paperlocale.source_symbols import REVIEWED_OVERLAY_OUTLINES, restore_verified_symbols


def overlay_fixture(path):
    """同一原字体内分别绘制控制码重音和基字，不依赖论文或商业字体。"""
    font = FontBuilder(1000, isTTF=False)
    font.setupGlyphOrder(['.notdef', 'FixtureMark', 'FixtureBase'])
    font.setupCharacterMap({1: 'FixtureMark', **{ord(c): 'FixtureBase' for c in 'Kulic'}})
    mark, base = T2CharStringPen(0, None), T2CharStringPen(500, None)
    mark.moveTo((160, 590)); mark.lineTo((355, 740)); mark.lineTo((430, 740)); mark.closePath()
    base.moveTo((30, 0)); base.lineTo((470, 0)); base.lineTo((470, 520)); base.lineTo((30, 520)); base.closePath()
    font.setupCFF('FixtureAccent', {'FullName': 'FixtureAccent', 'FamilyName': 'FixtureAccent',
                                   'Weight': 'Regular'},
                  {'.notdef': T2CharStringPen(0, None).getCharString(),
                   'FixtureMark': mark.getCharString(), 'FixtureBase': base.getCharString()}, {})
    program = BytesIO()
    font.font['CFF '].cff.compile(program, font.font)
    with fitz.open() as document:
        page = document.new_page()

        def obj(value, stream=None):
            ref = document.get_new_xref()
            document.update_object(ref, value)
            if stream is not None:
                document.update_stream(ref, stream)
            return ref

        fontfile = obj('<< /Subtype /Type1C >>', program.getvalue())
        descriptor = obj(f'<< /Type /FontDescriptor /FontName /FixtureAccent /Flags 4 /FontBBox [0 0 500 740] '
                         f'/Ascent 740 /Descent 0 /CapHeight 740 /StemV 80 /ItalicAngle 0 /FontFile3 {fontfile} 0 R >>')
        codes = [ord(c) for c in '\x01Kulic']
        mappings = ' '.join(f'<{code:02x}> <{code:04x}>' for code in codes)
        cmap = obj('<<>>', ('begincmap /CMapType 2 def 1 begincodespacerange <00> <ff> endcodespacerange '
                            f'6 beginbfchar {mappings} endbfchar endcmap').encode())
        widths = ' '.join('500' if code in codes[1:] else '0' for code in range(1, 118))
        differences = ' '.join(f'{code} /FixtureBase' for code in codes[1:])
        native = obj(f'<< /Type /Font /Subtype /Type1 /BaseFont /FixtureAccent /FontDescriptor {descriptor} 0 R '
                     f'/FirstChar 1 /LastChar 117 /Widths [{widths}] '
                     f'/Encoding << /Differences [1 /FixtureMark {differences}] >> /ToUnicode {cmap} 0 R >>')
        document.xref_set_key(page.xref, 'Resources', f'<< /Font << /F {native} 0 R >> >>')
        # 第一行相距 0.02 em，可合成。第二行超过已核验的配对上限。
        commands = '\n'.join(f'BT /F 20 Tf 1 0 0 1 100 {y} Tm <4b756c69> Tj '
                              f'1 0 0 1 {mark_x} {y} Tm <01> Tj 1 0 0 1 140 {y} Tm <63> Tj ET'
                              for y, mark_x in ((742, 140.4), (702, 141.8)))
        content = obj('<<>>', commands.encode())
        document.xref_set_key(page.xref, 'Contents', f'{content} 0 R')
        document.save(path)


def test_acute_is_one_readable_character_and_two_verified_glyphs(tmp_path):
    path, rendered = tmp_path / 'source.pdf', tmp_path / 'readback.pdf'
    overlay_fixture(path)
    with fitz.open(path) as document:
        page = document[0]
        raw = page.get_text('rawdict')['blocks']
        mark = next(c for b in raw for line in b['lines'] for span in line['spans']
                    for c in span['chars'] if c['c'] == '\x01')
        origin = mark['origin']
        cache = {}
        glyph = _source_anchor_glyphs(page, [{'text': mark['c'], 'rect': mark['bbox'],
                                             'origin': origin}], cache)[0]
        pen = RecordingPen()
        cache[glyph['xref']][1][glyph['name']].draw(pen)
        identity = ('cff', 1000, hashlib.sha256(repr(pen.value).encode()).hexdigest())
        with patch.dict(REVIEWED_OVERLAY_OUTLINES, {identity: '\u0301'}):
            restore_verified_symbols(page, raw, cache)
            assert mark['c'] == '\x01'
            restore_verified_symbols(page, raw, cache, reviewed_overlays=True)
            parts = [part for block in raw for part in _parts(block, 1, [], paragraph=True)]
            old = page.get_text('rawdict')['blocks']
            restore_verified_symbols(page, old, cache)
            assert any(c['c'] == '\x01' for b in old for line in b['lines']
                       for span in line['spans'] for c in span['chars'])

    assert any('\x01' in part['text'] for part in parts)
    anchor = next(part for part in parts if part['text'] == 'Kulić')
    assert anchor['fixed']
    mark, base = anchor['chars'][-2:]
    assert (mark['text'], mark['semantic_text'], mark['origin']) == ('\x01', '', list(origin))
    assert (base['text'], base['semantic_text']) == ('c', 'ć')
    bind_inline_glyphs(path, [{'anchors': [anchor], 'slots': [[], []]}])
    assert [g['glyph_id'] for g in anchor['glyphs'][-2:]] == [1, 2]
    placement = {'page': 1, 'inline_anchor': anchor, 'shift': [20, 30]}
    with fitz.open(path) as document:
        document.xref_set_key(document[0].xref, 'Contents', 'null')
        write_inline(document[0], placement, {})
        document.save(rendered)
    assert verify_inline(rendered, [placement])[0]['glyphs'] == 6
    with fitz.open(rendered) as document:
        assert document[0].get_text().strip() == 'Kulić'
