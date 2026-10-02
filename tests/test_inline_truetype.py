"""真实字体流回归：相同名字的不同 TTF 子集不能互换，重放不得改变数值。"""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from paperlocale.font_geometry import _source_anchor_glyphs, source_anchor_masks
from paperlocale.paragraph_layout import bind_inline_glyphs, paragraph_groups, write_inline, verify_inline
from paperlocale.diagnostics import LocatedError, record_error
from paperlocale.source_layout import save_json


def font_bytes(extra=False, cmap=True):
    """合成可分发的字体，extra 改变 GID 而保留同名，模拟出版社子集。"""
    builder = FontBuilder(1000, isTTF=True)
    chars = '(-278.1'
    order = ['.notdef'] + (['unused'] if extra else []) + [f'uni{ord(c):04X}' for c in chars]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({ord(c): f'uni{ord(c):04X}' for c in chars})
    glyphs = {}
    for index, name in enumerate(order):
        pen = TTGlyphPen(None)
        if name != '.notdef':
            pen.moveTo((50, 0)); pen.lineTo((200+index*15, 0))
            pen.lineTo((200+index*15, 700)); pen.lineTo((50, 700)); pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (500, 0) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'FixtureTT', 'styleName': 'Regular',
                           'fullName': 'FixtureTT', 'psName': 'FixtureTT'})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost(); builder.setupMaxp()
    if not cmap:
        del builder.font['cmap']
    output = BytesIO(); builder.save(output)
    return output.getvalue()


class InlineTrueTypeTests(unittest.TestCase):
    def test_replay_uses_cropbox_transform_and_serialized_glyph_positions(self):
        """真实非零 CropBox：原字形平移必须落在页面坐标，不能偏移裁切边距。"""
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.pdf';candidate=Path(folder)/'candidate.pdf'
            with fitz.open() as doc:
                page=doc.new_page(width=600,height=800)
                page.insert_font(fontname='fixture',fontbuffer=font_bytes())
                page.insert_text((100,100),'2',fontname='fixture',fontsize=12)
                page.set_cropbox(fitz.Rect(8,9,592,791));doc.save(source)
            with fitz.open(source) as doc:
                native=doc[0].get_text('rawdict')['blocks'][0]['lines'][0]['spans'][0]['chars']
                chars=[{'text':c['c'],'origin':c['origin'],'rect':c['bbox']} for c in native]
                glyphs=_source_anchor_glyphs(doc[0],chars,{})
                item={'page':1,'shift':[50,30],'inline_anchor':{'page':1,'text':'2','glyphs':glyphs}}
                write_inline(doc[0],item,{})
                doc.save(candidate)
            self.assertEqual(verify_inline(candidate,[item])[0]['glyphs'],1)

    def test_skipped_multiline_text_does_not_cover_independent_heading(self):
        from paperlocale.safe_text import safe_erase_rectangles
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'skip.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((40, 70), 'Supplementary', fontsize=10)
                page.insert_text((150, 70), 'Online material', fontsize=10)
                page.insert_text((40, 82), 'https://example.org/supplement', fontsize=10)
                doc.save(source)
            with fitz.open(source) as doc:
                lines = [line for block in doc[0].get_text('rawdict')['blocks'] for line in block['lines']]
                def chars(line):
                    return [{'text': c['c'], 'origin': c['origin'], 'rect': c['bbox']}
                            for span in line['spans'] for c in span['chars']]
                editable = {'page': 1, 'rect': lines[0]['bbox'], 'chars': chars(lines[0])}
                skipped = {'page': 1, 'kind': 'preserve', 'preserve_reason': '用户选择跳过，保留原文',
                           'rect': [40, 58, 300, 85], 'parts': [{'chars': chars(l)} for l in lines[1:]]}
                patches = safe_erase_rectangles([editable], [skipped], source)
                for patch in patches:
                    doc[0].add_redact_annot(patch['rect'], fill=False, cross_out=False)
                doc[0].apply_redactions()
                text = doc[0].get_text()
                self.assertNotIn('Supplementary', text)
                self.assertIn('Online material', text)
                self.assertIn('https://example.org/supplement', text)

    def test_ligature_uses_pdf_width_without_relaxing_position_tolerance(self):
        from paperlocale.safe_text import safe_erase_rectangles
        from paperlocale.source_symbols import restore_source_symbols
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'ligature.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                ref = page.insert_font(fontname='actual', fontbuffer=font_bytes(), set_simple=True)
                page.insert_text((40, 70), '2', fontname='actual', fontsize=12)
                # 一个轮廓映射 fi；PDF 宽度取整与字体内部宽度相差 0.0024 pt。
                cmap = doc.get_new_xref(); doc.update_object(cmap, '<<>>')
                doc.update_stream(cmap, b'1 begincodespacerange <00> <ff> endcodespacerange\n'
                                  b'1 beginbfchar <32> <00660069> endbfchar')
                doc.xref_set_key(ref, 'ToUnicode', f'{cmap} 0 R')
                doc.xref_set_key(ref, 'FirstChar', '50'); doc.xref_set_key(ref, 'LastChar', '50')
                doc.xref_set_key(ref, 'Widths', '[499.8]'); doc.save(source)
            with fitz.open(source) as doc:
                raw = doc[0].get_text('rawdict')['blocks']
                native = raw[0]['lines'][0]['spans'][0]['chars']
                chars = [{'text': c['c'], 'origin': c['origin'], 'rect': c['bbox']} for c in native]
                glyphs = _source_anchor_glyphs(doc[0], chars, {})
                self.assertEqual(len(glyphs), 1)
                self.assertEqual(glyphs[0]['text'], 'fi')
                restore_source_symbols(doc[0], raw, {})
                self.assertEqual(''.join(c['c'] for c in native), 'fi')
            part = {'page': 1, 'rect': list(native[0]['bbox']), 'chars': chars}
            self.assertTrue(safe_erase_rectangles([part], [], source))
            with self.assertRaisesRegex(ValueError, '没有安全删除范围'):
                safe_erase_rectangles([{**part, 'chars': chars[1:]}], [], source)

    def test_matching_subset_replays_same_pixels_and_searchable_numbers(self):
        self.check_replay(simple=True)

    def test_cid_font_without_cmap_replays_original_gids(self):
        self.check_replay(simple=False)

    def check_replay(self, simple):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'source.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=250, height=150)
                page.insert_font(fontname='unused', fontbuffer=font_bytes(True), set_simple=True)
                page.insert_font(fontname='actual', fontbuffer=font_bytes(), set_simple=simple)
                page.insert_text((40, 70), '(-278.1', fontname='actual', fontsize=12)
                if not simple:
                    from paperlocale.font_geometry import _font_descriptor
                    ref = next(f[0] for f in page.get_fonts() if f[4] == 'actual')
                    descriptor = _font_descriptor(doc, ref)
                    stream = int(doc.xref_get_key(descriptor, 'FontFile2')[1].split()[0])
                    doc.update_stream(stream, font_bytes(cmap=False))
                doc.save(source)
            with fitz.open(source) as doc:
                page = doc[0]
                from paperlocale.source_symbols import restore_source_symbols
                raw = page.get_text('rawdict')['blocks']
                restore_source_symbols(page, raw, {})
                self.assertEqual(''.join(c['c'] for b in raw for line in b['lines']
                                         for span in line['spans'] for c in span['chars']), '(-278.1')
                chars = [{'text': c['c'], 'origin': c['origin'], 'rect': c['bbox']}
                         for b in page.get_text('rawdict')['blocks'] for line in b['lines']
                         for span in line['spans'] for c in span['chars']]
                glyphs = _source_anchor_glyphs(page, chars, {})
                self.assertEqual(len(glyphs), 7)
                self.assertTrue(all(g['truetype'] for g in glyphs))
                before = page.get_pixmap(matrix=fitz.Matrix(3, 3)).samples
                for content in page.get_contents():
                    doc.update_stream(content, b'')
                item = {'page': 1, 'shift': [0, 0], 'inline_anchor':
                        {'page': 1, 'text': '(-278.1', 'glyphs': glyphs}}
                write_inline(page, item, {})
                target = Path(folder)/'replayed.pdf'; doc.save(target)
            verify_inline(target, [item])
            with fitz.open(target) as result:
                self.assertEqual(result[0].get_pixmap(matrix=fitz.Matrix(3, 3)).samples, before)
                self.assertEqual(result[0].get_text().strip(), '(-278.1')
            masks, supported = source_anchor_masks(source, [{'page': 1, 'chars': chars}], scale=3)
            self.assertEqual(supported, {0})
            self.assertIsNotNone(masks[1].getbbox())

    def test_unsupported_font_error_has_exact_paragraph_and_only_relevant_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root/'source.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                # 旋转的行内重排不在水平字形支持范围；必须给出准确定位。
                page.insert_font(fontname='composite', fontbuffer=font_bytes())
                page.insert_text((40, 70), '2', fontname='composite', rotate=90)
                doc.save(source)
            with fitz.open(source) as doc:
                c = doc[0].get_text('rawdict')['blocks'][0]['lines'][0]['spans'][0]['chars'][0]
            anchor = {'page': 1, 'text': '2', 'rect': list(c['bbox']), 'baseline': 70,
                      'chars': [{'text': '2', 'origin': c['origin'], 'rect': c['bbox']}]}
            unit = {'id': 'paragraph', 'source': 'The value is {v0}.', 'anchors': [anchor],
                    'frames': [{'page': 1, 'rect': [40, 50, 200, 80], 'size': 11}]}
            with self.assertRaises(LocatedError) as raised:
                bind_inline_glyphs(source, [unit])
            save_json(root/'run_manifest.json', {'source_pdf': str(source)})
            report = record_error(raised.exception, root)
            self.assertEqual(report['items'][0]['source'], 'The value is 2.')
            self.assertEqual(report['items'][0]['pages'], [1])
            self.assertEqual(report['items'][0]['anchor_text'], '2')
            self.assertEqual(report['page_context'], [])
            self.assertEqual([a['key'] for a in report['actions']], ['r', 's', 'q'])

    def test_abstract_is_not_joined_to_authors_with_matching_style(self):
        def block(key, text, y):
            return {'id': key, 'kind': 'body', 'page': 1, 'text': text,
                    'rect': [40, y, 300, y+10], 'parts': [{'fixed': False, 'size': 10,
                    'bold': True, 'text': text, 'rect': [40, y, 300, y+10]}]}
        blocks = [block('authors', 'First Author and Second Author', 100),
                  block('heading', 'Abstract', 170), block('body', 'The increasing frequency.', 182)]
        self.assertEqual(paragraph_groups(blocks), [['authors'], ['heading'], ['body']])
        self.assertEqual(paragraph_groups(blocks, section_boundaries=False)[0], ['authors', 'heading', 'body'])
