"""合成 PDF 回归：作者贡献侧栏不能被迫翻译姓名，也不能吞掉科学正文。"""
from collections import Counter
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, load_plan, save_json, units_from_plan


class AuthorSidebarTests(unittest.TestCase):
    def make_source(self, root, *, narrative=False, unknown_after=False, journal=False,
                    wide_body=True):
        source = root/'source.pdf'
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.insert_text((210,80), 'Compound climate extremes', fontname='hebo', fontsize=17)
            page.insert_text((210,110), 'Jane Smith and John Wang', fontsize=10)
            page.insert_text((40,155), 'Key Points:', fontname='hebo', fontsize=8)
            page.insert_text((40,175), 'Climate extremes affect crop yields.', fontsize=7)
            page.insert_text((210,170), 'Abstract', fontname='hebo', fontsize=11)
            page.insert_textbox(fitz.Rect(210,185,560,300),
                'The study compares observations across several agricultural regions. '
                'We evaluate compound drought and heat events during the growing season.', fontsize=10)
            page.insert_text((40,340), 'Author Contributions:', fontname='hebo', fontsize=8)
            if narrative:
                lines = [(365, 'The authors designed the study.'),
                         (385, 'They analysed the regional observations.')]
            else:
                lines = [(365, 'Conceptualization: Jane Smith,'), (379, 'John Wang'),
                         (400, 'Formal analysis: Jane Smith,'), (414, 'John Wang'),
                         (435, 'Writing - review & editing: Jane Smith')]
            for y, text in lines:
                page.insert_text((40,y), text, fontsize=7)
            if unknown_after:
                page.insert_text((40,460), 'Regional observations reveal spatial changes.', fontsize=7)
            page.insert_textbox(fitz.Rect(40,500,180,610),
                '© 2026. The Author(s). This is an open access article under the terms '
                'of the Creative Commons Attribution License.', fontsize=8)
            if wide_body:
                page.insert_textbox(fitz.Rect(210,350,560,480),
                    'Independent observations show the regional climate response. '
                    'This scientific paragraph must remain available for translation.', fontsize=10)
            page.insert_text((40,650), 'Acknowledgments', fontname='hebo', fontsize=8)
            page.insert_text((40,674), 'We thank the observing network.', fontsize=7)
            if journal:
                doc.set_metadata({'subject': "Earth's Future; 10.1029/2025EF012345"})
            doc.save(source)
        return source

    def block(self, plan, prefix):
        return next(b for b in plan['blocks'] if b['text'].startswith(prefix))

    def test_credit_sidebar_preserved_but_key_points_abstract_body_and_ack_translated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = self.make_source(root)
            old = extract_layout(source, paragraph=True, author_details=False)
            plan = extract_layout(source, paragraph=True)
            self.assertEqual(plan['author_details_revision'], 1)
            for prefix in ('Author Contributions:', 'Conceptualization:', 'John Wang',
                           'Formal analysis:', 'Writing - review & editing:', '© 2026.'):
                block = self.block(plan, prefix)
                self.assertEqual(block['kind'], 'preserve', prefix)
                self.assertIn('侧栏', block['preserve_reason'])
            for prefix in ('Key Points:', 'Climate extremes', 'Abstract', 'The study',
                           'Independent observations', 'Acknowledgments', 'We thank'):
                self.assertEqual(self.block(plan, prefix)['kind'], 'body', prefix)
            sources = '\n'.join(u['source'] for u in units_from_plan(plan, paragraph=True))
            self.assertNotIn('John Wang', sources)
            self.assertNotIn('Creative Commons', sources)
            self.assertIn('Independent observations', sources)
            # 分类只能改变翻译范围，不能删除、改写或移动任何原文字符。
            chars = lambda p: Counter((c['text'], tuple(c['origin']), tuple(c['rect']))
                for b in p['blocks'] for part in b['parts'] for c in part['chars'])
            self.assertEqual(chars(old), chars(plan))
            path = root/'plan.json'; save_json(path, plan)
            self.assertEqual(load_plan(source, path, paragraph=True)[0], plan)

    def test_narrative_contributions_remain_translatable(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = extract_layout(self.make_source(Path(directory), narrative=True), paragraph=True)
            for prefix in ('Author Contributions:', 'The authors', 'They analysed'):
                self.assertEqual(self.block(plan, prefix)['kind'], 'body')

    def test_unknown_text_after_credit_records_is_not_silently_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = extract_layout(self.make_source(Path(directory), unknown_after=True), paragraph=True)
            self.assertEqual(self.block(plan, 'Conceptualization:')['kind'], 'preserve')
            self.assertEqual(self.block(plan, 'Regional observations')['kind'], 'body')
            self.assertEqual(self.block(plan, '© 2026.')['kind'], 'body')

    def test_short_role_lines_without_adjacent_wide_body_are_not_a_sidebar(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = extract_layout(self.make_source(Path(directory), wide_body=False), paragraph=True)
            self.assertEqual(self.block(plan, 'Author Contributions:')['kind'], 'body')
            self.assertEqual(self.block(plan, 'Formal analysis:')['kind'], 'body')

    def test_old_plan_recomputes_original_classification_and_journal_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = self.make_source(root, journal=True)
            old = extract_layout(source, paragraph=True, author_details=False)
            self.assertNotIn('author_details_revision', old)
            self.assertEqual(old['journal']['name'], 'unknown')
            self.assertEqual(self.block(old, 'Formal analysis:')['kind'], 'body')
            path = root/'old-plan.json'; save_json(path, old)
            before = path.read_bytes()
            self.assertEqual(load_plan(source, path, paragraph=True)[0], old)
            self.assertEqual(path.read_bytes(), before)
            modern = extract_layout(source, paragraph=True)
            self.assertEqual(modern['journal']['family'], 'agu')
            self.assertEqual(self.block(modern, 'Formal analysis:')['kind'], 'preserve')


if __name__ == '__main__':
    unittest.main()
