"""无文字的图表区域不能触发整页解析，也不能伪造已支持的文字掩模。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import pymupdf as fitz

from paperlocale.font_geometry import _source_anchor_glyphs, source_anchor_masks


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
