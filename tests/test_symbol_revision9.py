"""用自造 CFF 测试微前缀语义修复、源字形重放与旧计划隔离。"""

import hashlib
from io import BytesIO
from unittest.mock import patch

import pymupdf as fitz
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen

from paperlocale import source_symbols
from paperlocale.paragraph_layout import bind_inline_glyphs, verify_inline, write_inline
from paperlocale.source_layout import extract_layout, load_plan, save_json


def _source_with_encoded_micro_sign(path):
    """制作代码 0x6D/GID 6 的微前缀，原 ToUnicode 故意写成 Latin m。"""
    font = FontBuilder(1000, isTTF=False)
    names = ['.notdef', 'one', 'two', 'three', 'four', 'five', 'm']
    # 第二个 m 是普通 m，使用同一源嵌入字体的不同 GID。须继续原样提取。
    displayed = b'The light level was 1200 m\xffol photons above the plants.'
    codes = sorted(set(displayed))
    encoded = {code: f'g{code}' if code != 109 else 'm' for code in codes}
    font.setupGlyphOrder(names + [name for name in encoded.values() if name != 'm'])
    font.setupCharacterMap(encoded)
    micro = T2CharStringPen(570, None)
    micro.moveTo((60, 510))
    for point in ((155, 510), (155, 110), (390, 110), (390, 510),
                  (490, 510), (490, 0), (155, 0), (155, -210), (60, -210)):
        micro.lineTo(point)
    micro.closePath()
    outlines = {name: T2CharStringPen(0, None).getCharString() for name in names[:-1]}
    outlines['m'] = micro.getCharString()
    for code, name in encoded.items():
        if name == 'm':
            continue
        glyph = T2CharStringPen(570, None)
        if code != 32:
            glyph.moveTo((60, 0))
            glyph.lineTo((490, 0))
            glyph.lineTo((490, 510))
            glyph.lineTo((60, 510))
            glyph.closePath()
        outlines[name] = glyph.getCharString()
    font.setupCFF('AdvP697C', {'FullName': 'AdvP697C', 'FamilyName': 'AdvP697C',
                               'Weight': 'Regular'}, outlines, {})
    program = BytesIO()
    cff = font.font['CFF '].cff
    cff.compile(program, font.font)
    pen = RecordingPen()
    cff[0].CharStrings['m'].draw(pen)

    with fitz.open() as document:
        page = document.new_page()

        def obj(value, stream=None):
            ref = document.get_new_xref()
            document.update_object(ref, value)
            if stream is not None:
                document.update_stream(ref, stream)
            return ref

        fontfile = obj('<< /Subtype /Type1C >>', program.getvalue())
        descriptor = obj(
            f'<< /Type /FontDescriptor /FontName /AdvP697C /Flags 4 /FontBBox [0 -210 570 510] '
            f'/Ascent 510 /Descent -210 /CapHeight 510 /StemV 95 /ItalicAngle 0 /FontFile3 {fontfile} 0 R >>')
        mappings = ' '.join(f'<{code:02x}> <{109 if code == 255 else code:04x}>' for code in codes)
        cmap = obj('<<>>', ('begincmap /CMapType 2 def 1 begincodespacerange <00> <ff> endcodespacerange '
                            f'{len(codes)} beginbfchar {mappings} endbfchar endcmap').encode())
        widths = ' '.join('570' if code in encoded else '0' for code in range(32, 256))
        differences = ' '.join(f'{code} /{encoded[code]}' for code in codes)
        native = obj(
            f'<< /Type /Font /Subtype /Type1 /BaseFont /AdvP697C /FontDescriptor {descriptor} 0 R '
            f'/FirstChar 32 /LastChar 255 /Widths [{widths}] /Encoding << /Differences [{differences}] >> '
            f'/ToUnicode {cmap} 0 R >>')
        document.xref_set_key(page.xref, 'Resources', f'<< /Font << /F {native} 0 R >> >>')
        content = obj('<<>>', f'BT /F 10 Tf 1 0 0 1 70 450 Tm <{displayed.hex()}> Tj ET'.encode())
        document.xref_set_key(page.xref, 'Contents', f'{content} 0 R')
        document.save(path)

    return (hashlib.sha256(program.getvalue()).hexdigest(),
            ('cff', 1000, hashlib.sha256(repr(pen.value).encode()).hexdigest()))


def test_micro_sign_uses_verified_original_glyph_and_preserves_a_quantity(tmp_path):
    source, rendered = tmp_path / 'source.pdf', tmp_path / 'readback.pdf'
    font_sha, outline = _source_with_encoded_micro_sign(source)
    with fitz.open(source) as document:
        original = next((c[2] for span in document[0].get_texttrace() if span['font'] == 'AdvP697C'
                         for c in span['chars'] if c[:2] == (109, 6)))
    unverified = extract_layout(source, paragraph=True)
    assert any('mmol' in b['text'] for b in unverified['blocks'])

    with patch.object(source_symbols, '_CSIRO_MICRO_SIGN_FONT', font_sha), \
            patch.object(source_symbols, '_CSIRO_MICRO_SIGN_OUTLINE', outline):
        old = extract_layout(source, paragraph=True, symbol_revision=8)
        modern = extract_layout(source, paragraph=True)
        assert old['blocks'] == unverified['blocks']
        assert modern['symbol_revision'] == 9
        for name, plan in [('old', old), ('modern', modern)]:
            path = tmp_path / f'{name}.json'
            save_json(path, plan)
            checked, units = load_plan(source, path, paragraph=True)
            assert checked == plan

    anchor = next(a for unit in units for a in unit['anchors'] if a['text'] == '1200 µmol')
    glyph = next(c for c in anchor['chars'] if c.get('semantic_text') == 'µ')
    assert (glyph['text'], tuple(glyph['origin'])) == ('m', original)
    bind_inline_glyphs(source, units)
    drawn = next(g for g in anchor['glyphs'] if g['text'] == 'µ')
    assert (drawn['name'], drawn['glyph_id'], drawn['origin']) == ('m', 6, original)
    placement = {'page': 1, 'inline_anchor': anchor, 'shift': [20, 30]}
    with fitz.open(source) as document:
        document.xref_set_key(document[0].xref, 'Contents', 'null')
        write_inline(document[0], placement, {})
        document.save(rendered)
    assert verify_inline(rendered, [placement])[0]['glyphs'] == 8
    with fitz.open(rendered) as document:
        assert ''.join(document[0].get_text().split()) == '1200µmol'
