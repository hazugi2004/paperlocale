"""合成 PDF 重现同行拆块及脚本变量紧贴普通谓词，不使用出版论文。"""
from collections import Counter
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, load_plan, save_json, units_from_plan


class InlineFragmentTests(unittest.TestCase):
    def make_source(self, root, *, wide_body=True, title=False, columns=False):
        source = root/'source.pdf'
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            # 故意把下方正文绘制在同行右片段之前，重现真实 PDF 的原生
            # 拆块；若连续绘制左右片段，MuPDF 自己会把它们合成一个块。
            left = 'where i denotes the spatial cell; NCDHE'
            page.insert_text((170,300), left, fontname='cour', fontsize=10)
            if wide_body:
                page.insert_text((170,340),
                    'A long normal body line extends to the right document boundary here.',
                    fontname='cour', fontsize=10)
            if title:
                page.insert_text((170,275),
                    'A full width heading over both text columns.', fontname='cobo', fontsize=14)
            page.insert_text((40,790), '1', fontsize=8)
            page.insert_text((408,300), 'is the number of event days', fontname='cour', fontsize=10)
            if columns:
                # 即使刻意缩小栏间距到一个字号内，没有附近同字号宽正文
                # 证据仍不能合并两栏；通栏标题也不能提供该证据。
                page.insert_text((170,320), 'Left column has its own next line.', fontname='cour', fontsize=10)
                page.insert_text((408,320), 'Right column continues.', fontname='cour', fontsize=10)
            doc.save(source)
        return source

    def sources(self, plan):
        return '\n'.join(u['source'] for u in units_from_plan(plan, paragraph=True))

    def test_single_wide_body_line_keeps_its_inline_predicate_before_next_paragraph(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = self.make_source(root)
            old = extract_layout(source, paragraph=True, inline_fragments=False)
            plan = extract_layout(source, paragraph=True)
            self.assertEqual(plan['inline_fragment_revision'], 2)
            joined = next(b for b in plan['blocks'] if b['text'].startswith('where'))
            self.assertIn('NCDHE is the number of event days', joined['text'])
            text = self.sources(plan)
            self.assertLess(text.index('is the number'), text.index('A long normal'))
            self.assertGreater(self.sources(old).index('is the number'), self.sources(old).index('A long normal'))
            # 合并只改变逻辑块，所有源字符及其位置须逐项相同。
            chars = lambda p: Counter((c['text'], tuple(c['origin']), tuple(c['rect']))
                for b in p['blocks'] for part in b['parts'] for c in part['chars'])
            self.assertEqual(chars(old), chars(plan))
            path = root/'plan.json'; save_json(path, plan)
            self.assertEqual(load_plan(source, path, paragraph=True)[0], plan)

    def test_true_columns_remain_separate_even_below_full_width_heading(self):
        for title in (False, True):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                source = self.make_source(Path(directory), wide_body=False, title=title, columns=True)
                plan = extract_layout(source, paragraph=True)
                left = next(b for b in plan['blocks'] if b['text'].startswith('where'))
                self.assertNotIn('is the number', left['text'])
                self.assertTrue(any(b['text'].startswith('is the number') for b in plan['blocks']))

    def test_no_nearby_wide_body_does_not_invent_an_inline_join(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_source(Path(directory), wide_body=False)
            plan = extract_layout(source, paragraph=True)
            self.assertTrue(any(b['text'] == 'is the number of event days' for b in plan['blocks']))

    def test_old_plan_recomputes_original_fragments_without_mutating_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = self.make_source(root)
            old = extract_layout(source, paragraph=True, inline_fragments=False, symbol_revision=4)
            self.assertNotIn('inline_fragment_revision', old)
            path = root/'old-plan.json'; save_json(path, old); before = path.read_bytes()
            self.assertEqual(load_plan(source, path, paragraph=True)[0], old)
            self.assertEqual(path.read_bytes(), before)

    def make_script_source(self, root, *, math_suffix=False, plain_variable=False,
                           suffix='denotes', suffix_size=10):
        source = root/'script.pdf'
        with fitz.open() as doc:
            page = doc.new_page()
            x = 80
            page.insert_text((x,100), 'where ', fontname='tiro', fontsize=10)
            x += fitz.get_text_length('where ', fontname='tiro', fontsize=10)
            page.insert_text((x,100), 'V', fontname='tiit', fontsize=10)
            x += fitz.get_text_length('V', fontname='tiit', fontsize=10)
            if not plain_variable:
                page.insert_text((x,97), 'wheat', fontname='tiro', fontsize=7)
                page.insert_text((x,102), 'i', fontname='tiit', fontsize=7)
                x += fitz.get_text_length('wheat', fontname='tiro', fontsize=7)
            font = 'tiit' if math_suffix else 'tiro'
            page.insert_text((x,100), suffix+' the final weighted value.', fontname=font, fontsize=suffix_size)
            doc.save(source)
        return source

    def test_script_variable_keeps_its_glyphs_but_adjacent_denotes_is_translatable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_script_source(Path(directory))
            old = extract_layout(source, paragraph=True, inline_fragments=False)
            plan = extract_layout(source, paragraph=True)
            units = units_from_plan(plan, paragraph=True)
            self.assertIn('where {v0} denotes the final weighted value.', units[0]['source'])
            self.assertEqual([a['text'].replace('\n','') for a in units[0]['anchors']], ['Vwheati'])
            self.assertIn('Vwheatidenotes', ' '.join(a['text'].replace('\n','') for u in units_from_plan(old, paragraph=True) for a in u['anchors']))
            chars = lambda p: Counter((c['text'], tuple(c['origin']), tuple(c['rect']))
                for b in p['blocks'] for part in b['parts'] for c in part['chars'])
            self.assertEqual(chars(old), chars(plan))

    def test_without_script_geometry_or_roman_prose_transition_do_not_guess_a_word(self):
        for kwargs in ({'math_suffix': True}, {'plain_variable': True},
                       {'suffix': 'denotestest'}, {'suffix': 'weighted'}, {'suffix_size': 7}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                source = self.make_script_source(Path(directory), **kwargs)
                old = extract_layout(source, paragraph=True, inline_fragments=False)
                new = extract_layout(source, paragraph=True)
                self.assertEqual([(p['text'],p['fixed']) for b in old['blocks'] for p in b['parts']],
                                 [(p['text'],p['fixed']) for b in new['blocks'] for p in b['parts']])


if __name__ == '__main__':
    unittest.main()
