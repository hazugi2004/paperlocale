"""用自造 PDF 验证脚本逗号不拆开变量，正文变量列表仍保留可译标点。"""
from collections import Counter
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, load_plan, save_json, units_from_plan


class SubscriptCommaTests(unittest.TestCase):
    def make_source(self, root, *, comma_size=10, comma_baseline=102,
                    right_size=7, right_baseline=102, gap=0, plain=False, compact=False):
        source = root/'source.pdf'
        with fitz.open() as doc:
            page = doc.new_page()
            x = 80
            page.insert_text((x,100), 'where ', fontname='tiro', fontsize=10)
            x += fitz.get_text_length('where ', fontname='tiro', fontsize=10)
            if plain:
                # 普通基线 x, y 即使两边斜体变量都已 fixed，逗号仍是
                # 可译标点；不能用“左右都是变量”推定它属于一个下标。
                page.insert_text((x,100), 'x', fontname='tiit', fontsize=10)
                x += fitz.get_text_length('x', fontname='tiit', fontsize=10)
                separator = ',' if compact else ', '
                page.insert_text((x,100), separator, fontname='tiro', fontsize=10)
                x += fitz.get_text_length(separator, fontname='tiro', fontsize=10)
                page.insert_text((x,100), 'y', fontname='tiit', fontsize=10)
                x += fitz.get_text_length('y', fontname='tiit', fontsize=10)
            else:
                page.insert_text((x,100), 'N', fontname='tiit', fontsize=10)
                x += fitz.get_text_length('N', fontname='tiit', fontsize=10)
                page.insert_text((x,97), 'GS', fontname='tiro', fontsize=7)
                page.insert_text((x,102), 'i', fontname='tiit', fontsize=7)
                comma_x = x+fitz.get_text_length('i', fontname='tiit', fontsize=7)+gap
                page.insert_text((comma_x,comma_baseline), ',', fontname='tiro', fontsize=comma_size)
                right_x = comma_x+fitz.get_text_length(',', fontname='tiro', fontsize=comma_size)+gap
                page.insert_text((right_x,right_baseline), 'y', fontname='tiit', fontsize=right_size)
                x = max(x+fitz.get_text_length('GS', fontname='tiro', fontsize=7),
                        right_x+fitz.get_text_length('y', fontname='tiit', fontsize=right_size))
            page.insert_text((x+1,100), ' describes the growing season.', fontname='tiro', fontsize=10)
            doc.save(source)
        return source

    def chars(self, plan):
        return Counter((c['text'], tuple(c['origin']), tuple(c['rect']))
            for b in plan['blocks'] for p in b['parts'] for c in p['chars'])

    def fixed(self, plan):
        return [p['text'].replace('\n','').replace(' ','')
                for b in plan['blocks'] for p in b['parts'] if p['fixed']]

    def test_low_comma_with_normal_or_small_font_stays_inside_one_variable_anchor(self):
        for size in (10, 7.5):
            with self.subTest(comma_size=size), tempfile.TemporaryDirectory() as directory:
                source = self.make_source(Path(directory), comma_size=size)
                old = extract_layout(source, paragraph=True, inline_fragments=1)
                plan = extract_layout(source, paragraph=True)
                self.assertIn('NGSi,y', self.fixed(plan))
                self.assertNotIn('NGSi,y', self.fixed(old))
                units = units_from_plan(plan, paragraph=True)
                self.assertEqual(len(units[0]['anchors']), 1)
                self.assertIn('where {v0} describes', units[0]['source'])
                self.assertEqual(self.chars(old), self.chars(plan))

    def test_plain_variable_list_and_mismatched_geometry_keep_original_punctuation_rules(self):
        for kwargs in ({'plain': True}, {'plain': True, 'compact': True}, {'comma_baseline': 100},
                       {'right_baseline': 100, 'right_size': 10}, {'gap': 8}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                source = self.make_source(Path(directory), **kwargs)
                old = extract_layout(source, paragraph=True, inline_fragments=1)
                plan = extract_layout(source, paragraph=True)
                self.assertEqual([(p['text'],p['fixed']) for b in old['blocks'] for p in b['parts']],
                                 [(p['text'],p['fixed']) for b in plan['blocks'] for p in b['parts']])
                self.assertTrue(any(',' in p['text'] and not p['fixed']
                                    for b in plan['blocks'] for p in b['parts']))

    def test_all_recorded_revisions_recompute_without_changing_plan_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = self.make_source(root)
            for revision in (0,1,2):
                with self.subTest(revision=revision):
                    plan = extract_layout(source, paragraph=True, inline_fragments=revision)
                    self.assertEqual(plan.get('inline_fragment_revision',0), revision)
                    path = root/f'plan-{revision}.json'; save_json(path, plan)
                    before = path.read_bytes()
                    self.assertEqual(load_plan(source, path, paragraph=True)[0], plan)
                    self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
