"""用独立竖排图注验证预检、断点复核和最终原页保护。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

from paperlocale.diagnostics import LocatedError
from paperlocale.domains import load_domain_pack
from paperlocale.preserved_workflow import run_preserved
from paperlocale.providers import Translation, TranslationProvider
from paperlocale.rotated_caption import prepare_caption_space
from paperlocale.source_layout import extract_layout, save_json
from paperlocale.workflow import initialize_run


def _source(path, *, overlapping_mark=False, body_overhang=False):
    figure = fitz.Rect(45, 65, 601, 253)
    caption = fitz.Rect(46, 265, 599, 315)
    to_original = fitz.Matrix(0, -1, 1, 0, 0, 640)
    with fitz.open() as document:
        page = document.new_page(width=640, height=450)
        page.draw_rect(figure, color=None, fill=(.2, .5, .7))
        page.insert_text((50, 280), 'Fig. 1 | Water and vegetation response.', fontsize=10)
        page.insert_text((50, 296), 'These responses remained visible throughout the study.', fontsize=10)
        if overlapping_mark:
            page.draw_circle((90, 290), 2, color=(1, 0, 0))
        page.set_rotation(270)
        page.remove_rotation()
        other = document.new_page(width=400, height=400)
        other.draw_circle((200, 200), 12, fill=(.8, .3, .1))
        other.insert_text((50, 35), 'Protected page header', fontsize=9)
        other.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(48, 22, 139, 39),
                           'uri': 'https://example.org/second-page'})
        if body_overhang:
            # 嵌入斜体的 j 伸出字符框左边，整页核验需要原字形才能扣除这部分墨迹。
            other.insert_font(fontname='BodyItalic', fontbuffer=fitz.Font('tiit').buffer)
            other.insert_text((100, 150), 'jumps in water level remained visible.',
                              fontname='BodyItalic', fontsize=18)
        document.save(path)
    return [{'page': 1, 'kind': 'figure', 'rect': list(figure * to_original)},
            {'page': 1, 'kind': 'table_footnote', 'rect': list(caption * to_original)},
            {'page': 2, 'kind': 'abandon', 'rect': [44, 20, 150, 40]}]


class _OfflineCaptionProvider(TranslationProvider):
    def translate(self, segments, context):
        return [Translation(segment.id, '水位突变仍然可见。' if segment.source.startswith('jumps') else
                            'Fig. 1 | 水分与植被响应。在整个研究期间均可观察到这些响应。')
                for segment in segments]


class RotatedCaptionTests(unittest.TestCase):
    def test_preflight_replay_and_render_restore_original_viewport(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, run = root / 'source.pdf', root / 'run'
            detections = _source(source, body_overhang=True)
            original = initialize_run(source_pdf=source, run_dir=run, source_language='en',
                                      target_language='zh-CN')
            font = root / 'font.ttf'
            font.write_bytes(fitz.Font('china-s').buffer)
            settings = {'plan_path': None, 'font_file': font, 'paragraph': True, 'generate_qa': False}
            with patch('paperlocale.layout_detection.detect_regions', return_value=detections):
                preflight = run_preserved(run, provider=None, domain=None, preflight_only=True, **settings)
                self.assertEqual(preflight['logical_group_count'], 2)
                self.assertEqual(preflight['rotated_captions'][0]['angle'], 90)
                result = run_preserved(run, provider=_OfflineCaptionProvider(),
                                       domain=load_domain_pack('ecology'), **settings)

            plan = json.loads((run / 'layout_plan.json').read_text())
            preservation = json.loads((run / 'preservation_report.json').read_text())
            locations = json.loads((run / 'source_locations.json').read_text())
            self.assertEqual(result['source_pdf'], str(source.resolve()))
            for record in (plan, preflight, result, preservation):
                self.assertEqual(record['source_sha256'], original['source_sha256'])
            self.assertEqual(result['status'], 'rendered')
            self.assertTrue(preservation['rotated_captions'][0]['outside_pixels_equal'])
            self.assertGreater(preservation['rotated_captions'][0]['glyph_positions_verified'], 0)
            self.assertTrue(preservation['pages'][1]['outside_pixels_equal'])
            caption_region = next(region for location in locations for region in location['regions']
                                  if region['page'] == 1)
            region = fitz.Rect(caption_region['rect'])
            self.assertLess(region.width, region.height)
            with fitz.open(source) as before, fitz.open(result['rendered_pdf']) as after:
                self.assertEqual(before[0].rect, after[0].rect)
                self.assertFalse(after[0].rotation)
                self.assertIn('水分与植被响应', after[0].get_text().replace('\n', ''))
                self.assertNotIn('throughout the study', after[0].get_text())
                directions = {tuple(line['dir']) for block in after[0].get_text('dict')['blocks']
                              for line in block.get('lines', [])}
                self.assertIn((0, -1), directions)
                self.assertIn('水位突变仍然可见', after[1].get_text().replace('\n', ''))
                self.assertNotIn('jumps in water level', after[1].get_text())
                old_link, = before[1].get_links()
                kept_link, = after[1].get_links()
                self.assertEqual((old_link['from'], old_link['uri']), (kept_link['from'], kept_link['uri']))

    def test_old_plan_does_not_change_coordinates_on_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, run = root / 'source.pdf', root / 'run'
            detections = _source(source)
            initialize_run(source_pdf=source, run_dir=run, source_language='en', target_language='zh-CN')
            plan = extract_layout(source, detections, paragraph=True)
            self.assertNotIn('rotated_captions', plan)
            save_json(run / 'layout_plan.json', plan)
            font = root / 'font.ttf'
            font.write_bytes(fitz.Font('china-s').buffer)
            with patch('paperlocale.layout_detection.detect_regions', return_value=detections), \
                    patch('paperlocale.rotated_caption.prepare_caption_space') as prepare:
                with self.assertRaises(LocatedError):
                    run_preserved(run, provider=None, domain=None, plan_path=None,
                                  font_file=font, paragraph=True, preflight_only=True)
                prepare.assert_not_called()
            self.assertEqual(json.loads((run / 'layout_plan.json').read_text()), plan)

    def test_intersecting_vector_object_disqualifies_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.pdf'
            detections = _source(source, overlapping_mark=True)
            self.assertIsNone(prepare_caption_space(source, detections, root))
