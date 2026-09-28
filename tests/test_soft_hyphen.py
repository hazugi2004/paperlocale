"""软连字符回归：语义复原不能牺牲原字符证据或真实数学符号。"""
import copy
import unittest

import pymupdf as fitz

from paperlocale.local_ocr import anomalous_lines
from paperlocale.paragraph_layout import source_text
from paperlocale.source_layout import _parts, normalize_traced_spaces


class SoftHyphenTests(unittest.TestCase):
    def test_invisible_soft_hyphen_is_not_normalized_as_space(self):
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((30, 80), 'daily- scale')
            raw = page.get_text('rawdict')['blocks']
            raw[0]['lines'][0]['spans'][0]['chars'][6]['c'] = '\u00ad'
            normalize_traced_spaces(raw, page.get_texttrace())
            parts = _parts(raw[0], 1, [], paragraph=True)
            self.assertEqual(source_text(parts), 'daily-scale')
            self.assertIn('\u00ad', parts[0]['text'])

    def test_soft_hyphen_is_editable_with_original_coordinates(self):
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((30, 80), 'reg-')
            raw = page.get_text('rawdict')['blocks']
            char = raw[0]['lines'][0]['spans'][0]['chars'][-1]
            char['c'] = '\u00ad'
            before = copy.deepcopy(raw)
            parts = _parts(raw[0], 1, [], paragraph=True)
            self.assertEqual(anomalous_lines(raw), [])
            self.assertFalse(any(p['fixed'] for p in parts))
            evidence = parts[-1]['chars'][-1]
            self.assertEqual(evidence['text'], '\u00ad')
            self.assertEqual(evidence['rect'], list(char['bbox']))
            self.assertEqual(raw, before)
            # 仅支持已知软连字符；不能顺带放行未知控制码或替代字符。
            for unknown in ('\x01', '\x07', '\ufffd'):
                char['c'] = unknown
                self.assertEqual(len(anomalous_lines(raw)), 1)

    def test_wrapped_words_preserve_explicit_hyphen_and_math(self):
        def part(text, baseline, fixed=False):
            return dict(text=text, baseline=baseline, page=1, size=10,
                        rect=[30, baseline-10, 120, baseline], fixed=fixed)
        for first, second, expected in (
            ('reg\u00ad', 'ulating', 'regulating'),
            ('daily-\u00ad', 'scale', 'daily-scale'),
            ('un\u00adbroken', 'words', 'unbroken words'),
            ('soil', 'moisture', 'soil moisture'),
            ('x −', 'y', 'x − y'),
        ):
            with self.subTest(first=first):
                parts = [part(first, 80), part(second, 92)]
                before = copy.deepcopy(parts)
                self.assertEqual(source_text(parts), expected)
                self.assertEqual(parts, before)
        self.assertEqual(source_text([part('change', 80), part('−', 92, True)]),
                         'change {v0}')
