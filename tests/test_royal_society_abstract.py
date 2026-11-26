"""以原生矩形和文字重现 Royal Society 跨栏摘要底色的表格误判。"""
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.journals import royal_society_abstract_tables
from paperlocale.source_layout import extract_layout, load_plan, save_json


LEFT_PANEL = (42, 700, 284, 792)
RIGHT_PANEL = (312, 42, 554, 303)
RULED_TABLE = (42, 410, 284, 446)
LEFT_LINES = ['The field experiment examines forest recovery.'] * 6 + ['We used']
RIGHT_LINES = ['observations from independent monitoring sites.'] * 20 + ['These data document summer conditions.']


def _panel(page, rect, lines):
    x0, y0, x1, y1 = rect
    color = (.88, .86, .81)
    for tile in ((x0, y0, x0 + 6, y1), (x0 + 6, y0, x1 - 6, y1 - 8),
                 (x1 - 6, y0, x1, y1), (x0 + 6, y1 - 8, x1 - 6, y1)):
        page.draw_rect(tile, color=None, fill=color)
    page.insert_text((x0 + 6, y0 + 12), '\n'.join(lines), fontsize=9, lineheight=4 / 3)


def _sample(path):
    with fitz.open() as doc:
        first = doc.new_page(width=595, height=842)
        first.insert_text((42, 48), 'doi:10.1098/rstb.2030.0001', fontsize=9)
        second = doc.new_page(width=595, height=842)
        _panel(second, LEFT_PANEL, LEFT_LINES)
        _panel(second, RIGHT_PANEL, RIGHT_LINES)
        second.insert_text((312, 333), '1. Introduction', fontsize=16)
        for x in (42, 163, 284):
            second.draw_line((x, 410), (x, 446), width=.5)
        for y in (410, 428, 446):
            second.draw_line((42, y), (284, y), width=.5)
        for x in (48, 168):
            for y in (422, 440):
                second.insert_text((x, y), 'Value', fontsize=9)
        doc.save(path)
    return [{'page': 2, 'kind': 'plain text', 'rect': rect, 'confidence': .99}
            for rect in (LEFT_PANEL, RIGHT_PANEL)]


class RoyalSocietyAbstractTests(unittest.TestCase):
    def test_new_plan_reads_abstract_across_columns_and_replays_old_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, saved = root / 'native.pdf', root / 'layout_plan.json'
            detections = _sample(source)
            old = extract_layout(source, detections, paragraph=True, publisher_layout=1)
            new = extract_layout(source, detections, paragraph=True, publisher_layout=2)
            self.assertEqual(new['journal']['family'], 'royal-society')
            self.assertEqual((old['publisher_layout_revision'], new['publisher_layout_revision']), (1, 2))
            for plan, expected in ((old, {LEFT_PANEL, RIGHT_PANEL, RULED_TABLE}), (new, {RULED_TABLE})):
                tables = {tuple(region['rect']) for region in plan['protected_regions'] if region['kind'] == 'table'}
                self.assertEqual(tables, expected)
                save_json(saved, plan)
                replay, _ = load_plan(source, saved, detections, paragraph=True)
                self.assertEqual(replay, plan)
            blocks = {block['id']: block for block in new['blocks']}
            text_groups = [[blocks[bid]['text'] for bid in group] for group in new['groups']]
            abstract = next(group for group in text_groups if group[0].startswith(LEFT_LINES[0]))
            self.assertTrue(abstract[0].endswith('We used'))
            self.assertTrue(abstract[1].startswith(RIGHT_LINES[0]))
            self.assertIn(['1. Introduction'], text_groups)

    def test_ambiguous_or_model_table_evidence_keeps_both_native_boxes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'native.pdf'
            detections = _sample(source)
            with fitz.open(source) as doc:
                page = doc[1]
                tables = page.find_tables().tables
                self.assertEqual(royal_society_abstract_tables(page, tables, detections), {LEFT_PANEL, RIGHT_PANEL})
                self.assertEqual(royal_society_abstract_tables(page, tables, detections[:1]), set())
                conflict = [*detections, {**detections[1], 'kind': 'table'}]
                self.assertEqual(royal_society_abstract_tables(page, tables, conflict), set())


if __name__ == '__main__':
    unittest.main()
