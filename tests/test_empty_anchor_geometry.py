"""无文字的图表区域不能触发整页解析，也不能伪造已支持的文字掩模。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import pymupdf as fitz

from paperlocale.font_geometry import _source_anchor_glyphs, source_anchor_masks
from paperlocale.safe_text import verify_fixed_ink


class EmptyAnchorGeometryTests(unittest.TestCase):
    def test_empty_characters_need_no_page_resources_or_trace(self):
        page = Mock()
        page.get_fonts.side_effect = AssertionError('empty anchor parsed fonts')
        page.get_texttrace.side_effect = AssertionError('empty anchor replayed page')
        for include_empty in (False, True):
            self.assertIsNone(_source_anchor_glyphs(page, [], {}, include_empty=include_empty))
        page.get_fonts.assert_not_called()
        page.get_texttrace.assert_not_called()

    def test_real_figure_without_fixed_text_is_not_marked_as_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                page.draw_rect(fitz.Rect(40, 40, 80, 80), fill=(0, 0, 1))
                doc.save(path)
            # 没有文字掩模不代表保护区被豁免；保持原来的空supported集合，
            # 完整图表区域仍交由现有像素/矢量门禁核验。
            masks, supported = source_anchor_masks(path, [
                {'page': 1, 'kind': 'figure', 'rect': [40, 40, 80, 80]}])
            self.assertEqual(masks, {})
            self.assertEqual(supported, set())

    def test_blank_soft_hyphen_does_not_hide_overlapping_body_ink(self):
        with tempfile.TemporaryDirectory() as directory, fitz.open() as document:
            source = Path(directory) / 'source.pdf'
            candidate = Path(directory) / 'candidate.pdf'
            font = fitz.Font('helv')
            page = document.new_page(width=160, height=160)
            page.insert_font(fontname='fixture', fontbuffer=font.buffer)
            page.insert_text((53, 100), '_', fontname='fixture', fontsize=20)
            page.insert_text((50, 108), 'm ', fontname='fixture', fontsize=20, color=(.14, .14, .14))
            # 复现同一原点有不同宽度的字形：源空格的语义是换行软连字符，
            # 但另一个真实字形使仅凭原点无法识别它。
            space_x = 50 + font.text_length('m', fontsize=20)
            page.insert_text((space_x, 108), 'Z', fontname='fixture', fontsize=20)
            fixed_stream, body_stream, _ = page.get_contents()
            text, space = [f'{font.has_glyph(ord(char)):04x}'.encode() for char in 'm ']
            stream = document.xref_stream(body_stream)
            document.update_stream(body_stream, stream.replace(b'[<' + text + space + b'>]TJ',
                b'[<' + text + b'>]TJ /Span << /ActualText <FEFF00AD> >> BDC [<' + space + b'>]TJ EMC'))
            document.save(source)
            with fitz.open(source) as original:
                chars = [{'text': c['c'], 'origin': c['origin'], 'rect': c['bbox']}
                         for block in original[0].get_text('rawdict')['blocks'] for line in block['lines']
                         for span in line['spans'] for c in span['chars']]
                fixed = [{'page': 1, 'chars': [c for c in chars if c['text'] == '_']}]
                editable = [{'page': 1, 'chars': [c for c in chars if c['text'] in ('m', '\u00ad')]}]
                self.assertEqual([c['text'] for c in editable[0]['chars']], ['m', '\u00ad'])
                self.assertIsNone(_source_anchor_glyphs(original[0], editable[0]['chars'], {}))

            document.update_stream(body_stream, b'')
            reference = document.tobytes()
            candidate.write_bytes(reference)
            supported, evidence = verify_fixed_ink(source, candidate, reference, fixed, editable)
            self.assertEqual(supported, {0})
            self.assertGreater(evidence[0]['body_overlap_pixels'], 0)
            document.update_stream(fixed_stream, b'')
            candidate.write_bytes(document.tobytes())
            with self.assertRaisesRegex(ValueError, '固定字形墨迹像素改变'):
                verify_fixed_ink(source, candidate, reference, fixed, editable)
