"""源版面的真实 PDF 回归：跨页绕图、锚点保留、溢出和全量候选原子性。"""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image

from paperlocale.source_layout import extract_layout, load_plan, save_json, fit_unit, digest, CITATION, anchor_errors
from paperlocale.preserved_workflow import run_preserved
from paperlocale.providers import TranslationProvider, Translation
from paperlocale.domains import load_domain_pack
from paperlocale.workflow import initialize_run, load_manifest
from paperlocale.image_geometry import visible_image_regions
from paperlocale.safe_text import fixed_text_rectangles


def make_fixture(root):
    """自有三页样例，正文跨页后被图片再次分开；保留一张表与公式/引用。"""
    path = root / 'source.pdf'
    with fitz.open() as doc:
        p = doc.new_page(width=600, height=800)
        # 自有图形页眉使小型稀疏夹具与真实论文一样具有足够非白内容，
        # 空白页门禁仍按生产阈值运行，不为测试改动或跳过 QA。
        p.draw_rect((40, 20, 560, 45), fill=(.2, .4, .6))
        p.insert_textbox((40, 650, 560, 740),
            'A detailed experiment describes the response of plants across many regions. The result depends on the', fontsize=10)
        p = doc.new_page(width=600, height=800)
        # 自有图形页眉使小型稀疏夹具与真实论文一样具有足够非白内容，
        # 空白页门禁仍按生产阈值运行，不为测试改动或跳过 QA。
        p.draw_rect((40, 20, 560, 45), fill=(.2, .4, .6))
        p.insert_textbox((40, 80, 560, 140),
            'remaining water [1] in deep layers and the distribution of roots. The observations show that', fontsize=10)
        img = BytesIO()
        Image.new('RGB', (30, 30), (40, 120, 160)).save(img, format='PNG')
        p.insert_image((60, 180, 200, 320), stream=img.getvalue())
        p.insert_text((250, 240), 'x = 2', fontsize=12)
        p.insert_textbox((40, 400, 560, 440),
            'different layers work together under water stress and therefore require an integrated assessment of the', fontsize=10)
        p = doc.new_page(width=600, height=800)
        # 自有图形页眉使小型稀疏夹具与真实论文一样具有足够非白内容，
        # 空白页门禁仍按生产阈值运行，不为测试改动或跳过 QA。
        p.draw_rect((40, 20, 560, 45), fill=(.2, .4, .6))
        p.insert_textbox((40, 80, 560, 140),
            'whole profile. These observations demonstrate the value of measuring all layers together.', fontsize=10)
        for y in (200, 225, 250):
            p.draw_line((40, y), (300, y))
        for x in (40, 170, 300):
            p.draw_line((x, 200), (x, 250))
        p.insert_text((50, 217), 'Depth', fontsize=10)
        p.insert_text((180, 217), 'Value', fontsize=10)
        p.insert_text((50, 242), '10', fontsize=10)
        p.insert_text((180, 242), '20', fontsize=10)
        doc.save(path)
    return path


class Provider(TranslationProvider):
    calls = 0
    sources = None
    def translate(self, segments, context):
        self.calls += 1
        self.sources = [s.source for s in segments]
        return [Translation(s.id, '研究表明水分{v0}影响植物生长。') for s in segments]


class SourceLayoutTests(unittest.TestCase):
    def setUp(self):
        # 几何/写入单元测试使用自有明确样例；ONNX 实测在真实论文验证中单独执行。
        self.detector = patch('paperlocale.layout_detection.detect_regions', return_value=[])
        self.detector.start()
        self.addCleanup(self.detector.stop)

    def test_cross_page_and_image_gap_is_one_logical_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_fixture(root)
            plan = extract_layout(source)
            save_json(root / 'plan.json', plan)
            _, units = load_plan(source, root / 'plan.json')
            self.assertEqual(len(units), 1)
            self.assertEqual(len(units[0]['blocks']), 4)
            self.assertIn('the remaining water', units[0]['source'])
            self.assertIn('the whole profile', units[0]['source'])
            self.assertNotIn('[1]', units[0]['source'])
            self.assertNotIn('Depth', units[0]['source'])
            self.assertEqual([a['text'] for a in units[0]['anchors']], ['[1]'])

    def test_heading_in_native_body_block_keeps_size_and_weight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'heading.pdf'
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((40, 100), 'Physical mechanisms behind vertically', fontname='hebo', fontsize=12)
                page.insert_text((40, 113), 'compound drought', fontname='hebo', fontsize=12)
                page.insert_text((40, 125), 'the experiment explains the measured response.', fontsize=9)
                document.save(source)
            with fitz.open(source) as document:
                self.assertEqual(len(document[0].get_text('blocks')), 1)
            save_json(root / 'plan.json', extract_layout(source))
            _, units = load_plan(source, root / 'plan.json')
            self.assertEqual(len(units), 2)
            self.assertIn('compound drought', units[0]['source'])
            self.assertTrue(units[1]['source'].startswith('the experiment'))
            font = fitz.Font('china-s')
            heading = fit_unit(units[0], '干旱物理机制', font, None, font)
            body = fit_unit(units[1], '实验说明实测响应。', font, None, font)
            self.assertTrue(all(p['bold'] and p['font_size'] == 12 for p in heading))
            self.assertTrue(all(not p['bold'] and p['font_size'] == 9 for p in body))

    def test_source_plan_cannot_omit_duplicate_or_forge_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_fixture(root)
            for change in ('omit', 'duplicate', 'forge'):
                plan = extract_layout(source)
                if change == 'omit':
                    plan['groups'][0].pop()
                elif change == 'duplicate':
                    plan['groups'][0].append(plan['groups'][0][0])
                else:
                    plan['blocks'][0]['text'] = 'invented text'
                save_json(root / 'plan.json', plan)
                with self.assertRaises(ValueError):
                    load_plan(source, root / 'plan.json')

    def test_anchor_reorder_and_overflow_are_rejected(self):
        unit = {'anchors': [{}, {}], 'slots': [[], [], []]}
        with self.assertRaisesRegex(ValueError, '锚点'):
            fit_unit(unit, '{v1}{v0}', fitz.Font('helv'), None)
        unit = {'anchors': [], 'slots': [[{'page': 1, 'rect': [0, 0, 10, 12], 'size': 10}]]}
        with self.assertRaisesRegex(ValueError, '完整译文'):
            fit_unit(unit, 'impossiblylongword', fitz.Font('helv'), None)

    def test_restored_anchor_cannot_duplicate_closing_parenthesis(self):
        unit = {'source': 'Observed (Fig. {v0}.', 'anchors': [{'text': '3c)'}]}
        self.assertEqual(anchor_errors(unit, '观测（图 {v0}。'), [])
        self.assertTrue(anchor_errors(unit, '观测（图 {v0}）。'))

    def test_link_edge_does_not_protect_body_word_ending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.pdf'
            with fitz.open() as document:
                page = document.new_page()
                text = 'The experiment measures ecosystem functioning'
                page.insert_text((40, 100), text, fontsize=10)
                x = 40 + fitz.get_text_length(text, fontsize=10)
                page.insert_text((x, 97), '1', fontsize=6)
                page.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(x - .02, 90, x + 4, 103),
                                  'uri': 'https://example.org/reference/1'})
                document.save(source)
            plan = extract_layout(source)
            save_json(root / 'plan.json', plan)
            _, units = load_plan(source, root / 'plan.json')
            self.assertIn('functioning {v0}', units[0]['source'])
            self.assertEqual([a['text'] for a in units[0]['anchors']], ['1'])
            self.assertNotIn('writing_rect', units[0]['slots'][0][0])
            self.assertIsNone(CITATION.search('(June 2020 in Fig. 1c,d)'))
            self.assertIsNone(CITATION.search('(grant 2026AFA050)'))
            self.assertIsNotNone(CITATION.search('(Smith et al., 2020)'))

    def test_caption_continuation_in_other_column_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.pdf'
            with fitz.open() as document:
                page = document.new_page(width=600, height=800)
                page.insert_textbox((40, 300, 290, 360),
                                    'Fig. 1 | Soil water observations across multiple regions. '
                                    'The observations include all measured layers and their', fontsize=7)
                page.insert_textbox((310, 300, 560, 360),
                                    'responses across the measured layers.', fontsize=7)
                page.insert_textbox((40, 400, 290, 460),
                                    'A separate body paragraph describes the result.', fontsize=9)
                document.save(source)
            blocks = extract_layout(source)['blocks']
            self.assertEqual([b['kind'] for b in blocks if b['text'].startswith('responses')], ['caption'])
            self.assertEqual([b['kind'] for b in blocks if b['text'].startswith('A separate')], ['body'])

    def test_complete_inline_statistical_expression_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'statistics.pdf'
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((40, 100), 'Strong agreement (r: 0.79-0.99; p < 0.01) was observed (June 2020).', fontsize=9)
                document.save(source)
            save_json(root / 'plan.json', extract_layout(source))
            _, units = load_plan(source, root / 'plan.json')
            self.assertEqual([a['text'] for a in units[0]['anchors']], ['(r: 0.79-0.99; p < 0.01)'])
            self.assertIn('June 2020', units[0]['source'])

    def test_inline_variable_keeps_base_and_subscript_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.pdf'
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((40, 100), 'where ', fontsize=10)
                x = 40 + fitz.get_text_length('where ', fontsize=10)
                page.insert_text((x, 100), 'R', fontname='heit', fontsize=9)
                x += fitz.get_text_length('R', fontname='heit', fontsize=9)
                page.insert_text((x, 102), 'n', fontsize=6)
                x += fitz.get_text_length('n', fontsize=6)
                page.insert_text((x, 100), ' denotes net radiation.', fontsize=10)
                document.save(source)
            plan = extract_layout(source)
            save_json(root / 'plan.json', plan)
            _, units = load_plan(source, root / 'plan.json')
            self.assertEqual([a['text'] for a in units[0]['anchors']], ['Rn'])
            self.assertIn('where {v0} denotes', units[0]['source'])

    def test_standalone_web_address_including_period_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.pdf'
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((40, 100), 'Detailed research methods are available at', fontname='hebo', fontsize=8)
                page.insert_text((40, 110), 'www.nature.com/reprints.', fontsize=8)
                document.save(source)
            plan = extract_layout(source)
            blocks = plan['blocks']
            self.assertEqual([b['kind'] for b in blocks], ['body', 'preserve'])
            self.assertTrue(blocks[1]['text'].endswith('.'))
            self.assertTrue(fitz.Rect(blocks[0]['rect']).intersects(fitz.Rect(blocks[1]['rect'])))
            save_json(Path(directory) / 'plan.json', plan)
            _, units = load_plan(source, Path(directory) / 'plan.json')
            from paperlocale.safe_text import safe_erase_rectangles
            editable = [p for u in units for slot in u['slots'] for p in slot]
            self.assertTrue(safe_erase_rectangles(editable, [blocks[1]], source))
            self.assertTrue(all(p.get('protected_rects') for p in editable))

    def test_clickable_prose_is_not_mistaken_for_reference_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.pdf'
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((40, 100), 'Read this additional information.', fontsize=10)
                page.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(35, 85, 300, 105),
                                  'uri': 'https://example.org/supplement'})
                document.save(source)
            plan = extract_layout(source)
            save_json(root / 'plan.json', plan)
            _, units = load_plan(source, root / 'plan.json')
            self.assertEqual(units[0]['source'], 'Read this additional information.')
            self.assertEqual(units[0]['anchors'], [])

    def test_reference_prefix_and_parentheses_remain_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.pdf'
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((40, 100), 'See (ref. 1) for the complete result.', fontsize=10)
                document.save(source)
            plan = extract_layout(source)
            save_json(root / 'plan.json', plan)
            _, units = load_plan(source, root / 'plan.json')
            self.assertEqual([a['text'] for a in units[0]['anchors']], ['(ref. 1)'])
            self.assertEqual(units[0]['source'], 'See {v0} for the complete result.')

    def test_scientific_unit_base_is_preserved_with_its_power(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.pdf'
            with fitz.open() as document:
                page = document.new_page()
                prefix = 'The area is 20 m'
                page.insert_text((40, 100), prefix, fontsize=10)
                x = 40 + fitz.get_text_length(prefix, fontsize=10)
                page.insert_text((x, 97), '2', fontsize=6)
                page.insert_text((x + 4, 100), ' across this region.', fontsize=10)
                document.save(source)
            plan = extract_layout(source)
            save_json(root / 'plan.json', plan)
            _, units = load_plan(source, root / 'plan.json')
            self.assertEqual([a['text'] for a in units[0]['anchors']], ['20 m2'])

    def test_transparent_image_body_conflict_cannot_silently_omit_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_fixture(root)
            # 模拟真实论文中图片大外框和视觉正文分类矛盾；不能把该正文直接算成图内文字。
            detections = [{'page': 2, 'rect': [70, 200, 190, 280], 'kind': 'plain text'}]
            plan = extract_layout(source, detections)
            save_json(root / 'plan.json', plan)
            with self.assertRaisesRegex(ValueError, '图片外框'):
                load_plan(source, root / 'plan.json', detections)

    def test_transparent_hole_retains_body_and_original_image(self):
        class BodyProvider(TranslationProvider):
            def translate(self, segments, context):
                return [Translation(s.id, '试验测量土壤水分响应。') for s in segments]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.pdf'
            rgba = Image.new('RGBA', (100, 100), (20, 100, 180, 255))
            # 非零 alpha 的四周围住透明孔洞，不能只用整张图或 alpha 外接框。
            rgba.paste((0, 0, 0, 0), (10, 10, 90, 90))
            buffer = BytesIO()
            rgba.save(buffer, format='PNG')
            with fitz.open() as document:
                page = document.new_page(width=600, height=800)
                page.insert_image((40, 80, 560, 600), stream=buffer.getvalue())
                page.insert_text((120, 300), 'An experiment measures soil water responses.', fontsize=10)
                regions = visible_image_regions(page)
                self.assertFalse(any(fitz.Rect(r['rect']).contains(fitz.Point(300, 300)) for r in regions))
                document.save(source)
            plan = extract_layout(source)
            self.assertEqual(len([b for b in plan['blocks'] if b['kind'] == 'body']), 1)
            font = root / 'cjk.ttf'
            font.write_bytes(fitz.Font('china-s').buffer)
            run = root / 'run'
            initialize_run(source_pdf=source, run_dir=run, source_language='en', target_language='zh-CN')
            result = run_preserved(run, provider=BodyProvider(), domain=load_domain_pack('atmospheric-science'),
                                   plan_path=None, font_file=font, dpi=72)
            self.assertEqual(result['status'], 'qa_generated')
            with fitz.open(result['rendered_pdf']) as document:
                self.assertIn('试验测量', document[0].get_text())
                self.assertNotIn('experiment', document[0].get_text())

    def test_chinese_wrap_does_not_start_next_line_with_comma(self):
        with tempfile.TemporaryDirectory() as directory:
            font = fitz.Font('china-s')
            unit = {'anchors': [], 'slots': [[
                {'page': 1, 'rect': [10, 10, 51, 28], 'size': 10, 'baseline': 24},
                {'page': 1, 'rect': [10, 35, 90, 53], 'size': 10, 'baseline': 49}]]}
            placements = fit_unit(unit, '土壤水分，影响植物。', font, None)
            self.assertEqual(''.join(p['target'] for p in placements), '土壤水分，影响植物。')
            self.assertTrue(all(not p['target'].startswith('，') for p in placements))

    def test_short_translation_reaches_original_cross_page_citation(self):
        font = fitz.Font('china-s')
        unit = {'anchors': [{'page': 2, 'rect': [210, 10, 220, 28]}], 'slots': [[
            {'page': 1, 'rect': [10, 10, 210, 28], 'size': 10},
            {'page': 1, 'rect': [10, 40, 210, 58], 'size': 10},
            {'page': 2, 'rect': [10, 10, 210, 28], 'size': 10}], []]}
        text = '土壤水分变化影响植物生长'
        placements = fit_unit(unit, text + '{v0}', font, None)
        self.assertEqual(''.join(p['target'] for p in placements), text)
        self.assertEqual(placements[0]['page'], 1)
        self.assertEqual(placements[-1]['page'], 2)
        last = placements[-1]
        end = last['origin_x'] + font.text_length(last['target'], fontsize=10)
        self.assertLess(210 - end, 1)
        self.assertLessEqual(end, 210)

    def test_corresponding_author_name_is_preserved_without_guessing_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'author.pdf'
            with fitz.open() as document:
                page = document.new_page(width=600, height=800)
                page.insert_text((40, 100), 'Correspondence and requests for materials should be addressed to', fontsize=10)
                page.insert_text((40, 125), 'Example Person.', fontsize=10)
                document.save(source)
            plan = extract_layout(source)
            author = next(b for b in plan['blocks'] if b['text'] == 'Example Person.')
            self.assertEqual(author['kind'], 'preserve')

    def test_short_author_list_and_affiliation_between_title_and_abstract_stay_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'front-matter.pdf'
            with fitz.open() as document:
                page = document.new_page(width=600, height=800)
                page.insert_text((40, 90), 'Soil moisture controls ecosystem response', fontsize=18)
                page.insert_text((40, 125), 'Jane Doe1 and John Smith2*', fontsize=10)
                page.insert_text((40, 150), '1 Department of Ecology, Example University', fontsize=9)
                page.insert_text((40, 190), 'Abstract', fontsize=12)
                page.insert_text((40, 220), 'We measured daily soil moisture and ecosystem response.', fontsize=10)
                document.save(source)
            blocks = extract_layout(source)['blocks']
            self.assertEqual([b['kind'] for b in blocks],
                             ['body', 'preserve', 'preserve', 'body', 'body'])

    def test_auxiliary_sections_stay_original_and_later_methods_resume(self):
        # References 之后的 Methods 仍属于正文；辅助标题和跨页内容均保护。
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'scope.pdf'
            with fitz.open() as document:
                for lines in [
                    ['Introduction', 'Main research prose is translated here.',
                     'Acknowledgements', 'We thank Example Person for assistance.'],
                    ['Funding continued on this page.', 'References',
                     'Methods', 'We measured soil moisture every day.',
                     'Data availability', 'The data are publicly available.'],
                    ['Code availability', 'The source code is deposited online.',
                     'Author contributions', 'Example Person designed the study.',
                     'Competing interests', 'The authors declare no conflicts.'],
                ]:
                    page = document.new_page(width=600, height=800)
                    for index, line in enumerate(lines):
                        page.insert_text((40, 80 + 35 * index), line, fontsize=10)
                document.save(source)
            blocks = extract_layout(source)['blocks']
            body = [b['text'] for b in blocks if b['kind'] == 'body']
            self.assertEqual(body, ['Introduction', 'Main research prose is translated here.',
                                    'Methods', 'We measured soil moisture every day.'])
            self.assertTrue(all(b['kind'] in {'preserve', 'reference'}
                                for b in blocks if b['text'] not in body))

    def test_superscript_check_excludes_body_below_but_detects_changed_anchor(self):
        # 联合外框里的句点属于正文；固定 A 和上标 1 的完整原字框仍逐一核验。
        with fitz.open() as source:
            page = source.new_page(width=100, height=100)
            page.insert_text((40, 70), 'A', fontsize=10)
            page.insert_text((60, 61), '1', fontsize=4)
            page.insert_text((60, 70), '.', fontsize=10)
            chars = [c for block in page.get_text('rawdict')['blocks'] for line in block.get('lines', [])
                     for span in line['spans'] for c in span['chars'] if c['c'] in 'A1']
            part = {'rect': list(fitz.Rect(chars[0]['bbox']) | fitz.Rect(chars[1]['bbox'])),
                    'chars': [{'rect': c['bbox'], 'origin': c['origin']} for c in chars]}
            regions = fixed_text_rectangles(part)
            self.assertEqual(len(regions), 2)
            with fitz.open(stream=source.tobytes(), filetype='pdf') as target:
                target[0].draw_rect((60, 68, 63, 73), color=None, fill=(1, 1, 1))
                def pixels(p, rect):
                    return p.get_pixmap(clip=rect, matrix=fitz.Matrix(4, 4)).samples
                self.assertNotEqual(pixels(page, part['rect']), pixels(target[0], part['rect']))
                self.assertTrue(all(pixels(page, r) == pixels(target[0], r) for r in regions))
                target[0].draw_rect((40, 65, 46, 70), color=None, fill=(1, 1, 1))
                self.assertTrue(any(pixels(page, r) != pixels(target[0], r) for r in regions))

    def test_open_bracket_can_immediately_precede_fixed_variable(self):
        unit = {'anchors': [{'page': 1, 'rect': [55, 10, 65, 28]}],
                'slots': [[{'page': 1, 'rect': [10, 10, 54, 28], 'size': 10}], []]}
        placements = fit_unit(unit, '水分（{v0}', fitz.Font('china-s'), None)
        self.assertEqual(''.join(p['target'] for p in placements), '水分（')

    def test_optional_cjk_ascii_space_does_not_force_identifier_overflow(self):
        font = fitz.Font('china-s')
        width = (font.text_length('土壤 MODEL', fontsize=10) + font.text_length('土壤MODEL', fontsize=10)) / 2
        unit = {'anchors': [], 'slots': [[{'page': 1, 'rect': [0, 0, width, 20], 'size': 10}]]}
        placements = fit_unit(unit, '土壤 MODEL', font, None)
        self.assertEqual(''.join(p['target'] for p in placements), '土壤MODEL')

    def test_range_wrap_keeps_all_digits_and_existing_dash(self):
        font = fitz.Font('china-s')
        width = font.text_length('（1981–', fontsize=10) + .2
        unit = {'anchors': [], 'slots': [[
            {'page': 1, 'rect': [0, 0, width, 20], 'size': 10},
            {'page': 1, 'rect': [0, 30, 100, 50], 'size': 10}]]}
        placements = fit_unit(unit, '（1981–2014）', font, None)
        self.assertEqual(''.join(p['target'] for p in placements), '（1981–2014）')
        self.assertEqual(placements[0]['target'], '（1981–')

    def test_wider_free_region_can_hold_more_text_than_largest_area(self):
        unit = {'anchors': [{'page': 1, 'rect': [13, 0, 20, 12]}],
                'slots': [[], [{'page': 1, 'rect': [0, 10, 115, 21], 'size': 8}]]}
        text = '土壤水分变化影响植被生长'
        placements = fit_unit(unit, '{v0}' + text, fitz.Font('china-s'), None)
        self.assertEqual(''.join(p['target'] for p in placements), text)

    def test_same_line_uses_both_sides_below_a_previous_subscript(self):
        unit = {'anchors': [{'page': 1, 'rect': [53, 0, 60, 13]}],
                'slots': [[], [{'page': 1, 'rect': [0, 10, 112, 21], 'size': 10, 'baseline': 19}]]}
        text = '土壤水分，影响生长'
        placements = fit_unit(unit, '{v0}' + text, fitz.Font('china-s'), None)
        self.assertEqual(''.join(p['target'] for p in placements), text)
        self.assertGreater(len(placements), 1)
        self.assertEqual(len({p['baseline'] for p in placements}), 1)
        self.assertLessEqual(placements[0]['rect'][2], placements[1]['rect'][0])

    def test_overlapping_citation_survives_safe_erasure_and_chinese_placement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, run = root / 'source.pdf', root / 'run'
            with fitz.open() as doc:
                page = doc.new_page()
                text = 'A complete experiment explores soil water '
                page.insert_text((40, 100), text, fontsize=10)
                # 真实出版社字体会产生此类极小字框重叠；即使肉眼近似相邻，
                # 大矩形删除仍可能删掉引用，因此必须在调用模型前阻止。
                x = 40 + fitz.get_text_length(text, fontsize=10) - .2
                page.insert_text((x, 100), '[1]', fontsize=10)
                page.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(40, 90, x + 12, 104),
                                  'uri': 'https://example.org/reference/1'})
                doc.save(source)
            font = root / 'cjk.ttf'
            font.write_bytes(fitz.Font('china-s').buffer)
            initialize_run(source_pdf=source, run_dir=run, source_language='en', target_language='zh-CN')
            class ShortProvider(TranslationProvider):
                def translate(self, segments, context):
                    return [Translation(s.id, '完整研究探索土壤水分{v0}') for s in segments]
            # 本例只检查删除/回填和保护像素；独立 PDF 机器 QA 在三页完整样例中验证。
            with patch('paperlocale.preserved_workflow.qa_run'):
                result = run_preserved(run, provider=ShortProvider(), domain=load_domain_pack('atmospheric-science'),
                                       plan_path=None, font_file=font)
            with fitz.open(result['rendered_pdf']) as document:
                text = document[0].get_text()
                self.assertIn('[1]', text)
                self.assertIn('完整研究', text)
                self.assertNotIn('experiment', text)
                with fitz.open(source) as original:
                    self.assertEqual(document[0].get_links(), original[0].get_links())

    def test_complete_candidate_protects_original_and_resume_reuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, run = make_fixture(root), root / 'run'
            # PyMuPDF 自带合法 CJK 字体，CI 不依赖用户字体缓存或联网下载。
            font_file = root / 'cjk.ttf'
            font_file.write_bytes(fitz.Font('china-s').buffer)
            initialize_run(source_pdf=source, run_dir=run, source_language='en', target_language='zh-CN')
            provider = Provider()
            kwargs = dict(provider=provider, domain=load_domain_pack('atmospheric-science'),
                          plan_path=None, font_file=font_file, min_font_size=7, dpi=72)
            result = run_preserved(run, **kwargs)
            self.assertEqual(result['status'], 'qa_generated')
            self.assertEqual(provider.calls, 1)
            candidate = Path(result['rendered_pdf'])
            before = digest(candidate)
            result = run_preserved(run, **kwargs)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(digest(candidate), before)
            with fitz.open(candidate) as doc:
                self.assertIn('[1]', doc[1].get_text())
                self.assertIn('x = 2', doc[1].get_text())
                self.assertIn('Depth', doc[2].get_text())

    def test_invalid_refinement_has_one_persisted_correction(self):
        class InvalidProvider(TranslationProvider):
            calls = 0
            def translate(self, segments, context):
                self.calls += 1
                assert context.anchor_text[segments[0].id] == {'{v0}': '[1]'}
                text = '水分影响植物生长。' * 2000 + '{v0}' if self.calls == 1 else '水分影响植物。'
                return [Translation(s.id, text) for s in segments]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, run = make_fixture(root), root / 'run'
            font = root / 'cjk.ttf'
            font.write_bytes(fitz.Font('china-s').buffer)
            initialize_run(source_pdf=source, run_dir=run, source_language='en', target_language='zh-CN')
            provider = InvalidProvider()
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, '科学内容校验'):
                    run_preserved(run, provider=provider, domain=load_domain_pack('atmospheric-science'),
                                  plan_path=None, font_file=font)
            self.assertEqual(provider.calls, 3)  # 首译、精炼、明确错误的一次纠正；恢复不再调用。
            self.assertFalse((run / 'render_output' / 'translated.pdf').exists())

    def test_oversized_translation_never_publishes_partial_candidate(self):
        class LongProvider(TranslationProvider):
            calls = 0
            def translate(self, segments, context):
                self.calls += 1
                return [Translation(s.id, '水分影响植物生长。' * 2000 + '{v0}') for s in segments]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, run = make_fixture(root), root / 'run'
            font_file = root / 'cjk.ttf'
            font_file.write_bytes(fitz.Font('china-s').buffer)
            initialize_run(source_pdf=source, run_dir=run, source_language='en', target_language='zh-CN')
            provider = LongProvider()
            for attempt in range(2):
                with self.assertRaisesRegex(ValueError, '完整译文'):
                    run_preserved(run, provider=provider, domain=load_domain_pack('atmospheric-science'),
                                  plan_path=None, font_file=font_file, min_font_size=7)
            self.assertEqual(provider.calls, 2)  # 首译与一次精炼；恢复不能再次调用。
            self.assertFalse((run / 'render_output' / 'translated.pdf').exists())
            self.assertTrue((run / 'translations.jsonl').exists())
            self.assertEqual(load_manifest(run)['status'], 'collected')
