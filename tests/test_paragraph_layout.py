"""0.7.2 用户可见回归：原段落、连续排字、图注和移动引文。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, load_plan, save_json, fit_unit
from paperlocale.paragraph_layout import bind_inline_glyphs, compact_target
from paperlocale.preserved_workflow import run_preserved
from paperlocale.providers import TranslationProvider, Translation
from paperlocale.workflow import initialize_run
from paperlocale.domains import load_domain_pack


class ParagraphTests(unittest.TestCase):
    def test_paragraph_skips_fixed_formula_without_losing_text(self):
        """段落框首行被公式占满时，从下一条可用行完整排字，不覆盖公式。"""
        from paperlocale.paragraph_layout import fit_paragraph
        obstacle = fitz.Rect(0,0,150,16)
        unit = {'id':'blocked','source':'Hello world','anchors':[], 'frames':[{
            'page':1,'rect':[0,0,150,80],'size':10,'bold':False,'leading':12,
            'weight':11,'indent':0,'obstacles':[list(obstacle)]}]}
        placed = fit_paragraph(unit,'Hello world',fitz.Font('china-s'),None,None)
        self.assertEqual(''.join(p['target'] for p in placed),'Hello world')
        self.assertTrue(all(not fitz.Rect(p['rect']).intersects(obstacle) for p in placed))

    def test_wide_figure_edge_does_not_split_caption_with_large_gap(self):
        from paperlocale.paragraph_layout import fit_paragraph
        unit={'id':'caption','source':'A complete caption.','anchors':[], 'frames':[{
            'page':1,'rect':[0,10,200,60],'size':10,'bold':False,'leading':12,
            'weight':20,'indent':0,'obstacles':[[25,0,180,12]]}]}
        placed=fit_paragraph(unit,'A complete caption.',fitz.Font('china-s'),None,None)
        self.assertEqual(''.join(p['target'] for p in placed),'A complete caption.')
        self.assertAlmostEqual(placed[0]['rect'][0],0)
        self.assertTrue(all(p['rect'][1]>12 for p in placed))
        self.assertTrue(all(b['rect'][0]-a['rect'][2]<4 for a,b in zip(placed,placed[1:])))

    def test_sidebar_bullets_keep_continuations_before_main_column(self):
        from paperlocale.paragraph_layout import paragraph_groups
        def block(key,text,x,y,width):
            return {'id':key,'kind':'body','page':1,'page_width':600,
                    'rect':[x,y,x+width,y+10],'text':text,
                    'parts':[{'text':text,'bold':False,'fixed':False,'size':10,
                              'rect':[x,y,x+width,y+10]}]}
        blocks=[block('a','• Changes increased rapidly after the',30,100,120),
                block('main','This main-column paragraph has a distinct reading order.',180,101,390),
                block('year','1990s',30,112,30),
                block('next','• The relative contributions vary',30,124,120),
                block('tail','with regions and seasons',30,136,120)]
        self.assertEqual(paragraph_groups(blocks),[['a','year'],['next','tail'],['main']])

    def test_reference_union_does_not_hide_sidebar_acknowledgements(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'sidebar.pdf'
            with fitz.open() as doc:
                page=doc.new_page(width=600,height=800)
                page.insert_text((180,270),'References',fontsize=10)
                page.insert_text((180,290),'Example, A. (2021). Research. Journal 1, 2.',fontsize=10)
                page.insert_text((30,270),'Acknowledgments',fontsize=8)
                page.insert_text((30,282),'We thank the editors.',fontsize=8)
                doc.save(source)
            refs=[{'page':1,'rect':[29,250,580,700]}]
            with patch('paperlocale.source_layout._reference_geometry',return_value=(None,None,None,refs)):
                plan=extract_layout(source,paragraph=True)
            block=next(b for b in plan['blocks'] if 'We thank' in b['text'])
            self.assertEqual(block['kind'],'body')
            self.assertTrue(all(not fitz.Rect(r['rect']).intersects(fitz.Rect(block['rect']))
                for r in plan['protected_regions'] if r['kind']=='reference'))

    def test_protected_region_does_not_include_adjacent_caption_pixel(self):
        """保护框外的图注像素不触发重放，框内仅1级色差仍必须被发现。"""
        from PIL import Image
        from paperlocale.safe_text import region_pixels
        source = Image.new('RGB',(30,30),'white')
        changed = source.copy()
        rect = [2,2,10,10.2]
        changed.putpixel((6,20),(0,0,0))  # 中心y=10.25，位于框外。
        self.assertEqual(region_pixels(source,rect),region_pixels(changed,rect))
        changed.putpixel((6,19),(254,255,255))
        self.assertNotEqual(region_pixels(source,rect),region_pixels(changed,rect))

    def test_link_without_xref_moves_once_and_preserves_other_link(self):
        """模拟读取为xref=0的链接：跨页后旧区域消失，目标和无关链接保留。"""
        from paperlocale.paragraph_layout import relocate_links, links_equal
        with fitz.open() as seed:
            seed.new_page(); seed.new_page()
            seed[0].insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(10,20,30,30), 'uri':'https://example.org/move'})
            seed[0].insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(60,20,80,30), 'uri':'https://example.org/keep'})
            data = seed.tobytes()
        placements = [{'page':2, 'shift':[100,100], 'inline_anchor':{
            'page':1, 'ink':[10,20,30,30], 'glyphs':[{'rect':[10,20,30,30]}]}}]
        get_links = fitz.Page.get_links
        def without_xref(page):
            return [{**v, 'xref':0} for v in get_links(page)]
        with fitz.open(stream=data,filetype='pdf') as original, fitz.open(stream=data,filetype='pdf') as written:
            with patch.object(fitz.Page, 'get_links', without_xref):
                expected = relocate_links(written,original,placements)
            output = written.tobytes()
        with fitz.open(stream=output,filetype='pdf') as result:
            self.assertTrue(links_equal(expected[1],result[0].get_links()))
            self.assertTrue(links_equal(expected[2],result[1].get_links()))
            self.assertEqual(len(result[0].get_links()),1)
            self.assertEqual(len(result[1].get_links()),1)

    def test_tall_formula_box_does_not_protect_empty_space_beside_it(self):
        """同基线高竖线旁的下一行正文可删，但竖线自身仍完全受保护。"""
        from paperlocale.safe_text import fixed_text_rectangles, safe_erase_rectangles
        protected = {'page': 1, 'rect': [10, 80, 100, 120], 'chars': [
            {'text': '|', 'origin': [10, 100], 'rect': [10, 80, 12, 120]},
            {'text': 'x', 'origin': [90, 100], 'rect': [90, 92, 100, 103]}]}
        edit = {'page': 1, 'rect': [40, 104, 50, 115], 'chars': [
            {'text': 'r', 'origin': [40, 112], 'rect': [40, 104, 50, 115]}]}
        boxes = fixed_text_rectangles(protected)
        self.assertEqual(sum(r.get_area() for r in boxes), 190)
        self.assertTrue(safe_erase_rectangles([edit], [protected]))
        blocked = {**edit, 'rect': [10, 104, 12, 115], 'chars': [
            {'text': 'r', 'origin': [10, 112], 'rect': [10, 104, 12, 115]}]}
        with self.assertRaisesRegex(ValueError, '没有安全删除范围'):
            safe_erase_rectangles([blocked], [protected])

    def test_panel_title_inside_figure_is_not_translated_as_caption(self):
        """相交面板标题保持原图；明确图注及图外无标签图注仍可翻译。"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                for y, text in [(100, 'ERA5 average index'),
                                (200, 'Figure 1. Annual values.'),
                                (300, 'Annual average values.')]:
                    page.insert_text((40, y), text, fontsize=10)
                doc.save(source)
            detections = [dict(page=1, kind='figure_caption', rect=[35,y-12,350,y+3])
                          for y in (100,200,300)]
            detections += [dict(page=1, kind='figure', rect=[35,y-1,400,y+60])
                           for y in (100,200)]
            plan = extract_layout(source, detections, paragraph=True)
            kinds = {b['text']: b['kind'] for b in plan['blocks']}
            self.assertEqual(kinds['ERA5 average index'], 'figure')
            self.assertEqual(kinds['Figure 1. Annual values.'], 'caption')
            self.assertEqual(kinds['Annual average values.'], 'caption')

    def test_blank_native_line_does_not_start_paragraph(self):
        from paperlocale.paragraph_layout import starts_paragraph
        line = {'spans': [{'size': 7, 'chars': [{'c': ' '}]}]}
        self.assertFalse(starts_paragraph([line], [line], line))

    def test_control_space_requires_unique_font_position_and_width(self):
        """重叠首字母不阻止空格恢复；未知、歧义或字体不符不得被删除。"""
        from copy import deepcopy
        from paperlocale.source_layout import normalize_traced_spaces
        raw = [{'lines': [{'spans': [{'font': 'STIX-Regular', 'chars': [
            {'c': '\x07', 'origin': (40, 100), 'bbox': (40, 90, 42, 103)}]}]}]}]
        trace = {'font': 'STIX-Regular', 'dir': (1, 0), 'chars': [
            (32, 1, (40, 100), (40, 95, 42, 102)),
            (65, 30, (40, 100), (40, 95, 46, 102))]}
        valid = deepcopy(raw)
        normalize_traced_spaces(valid, [trace])
        self.assertEqual(valid[0]['lines'][0]['spans'][0]['chars'][0]['c'], ' ')
        for traces in ([], [trace, trace], [{**trace, 'font': 'Other'}],
                       [{**trace, 'chars': [(32, 1, (40, 100), (40, 95, 43, 102))]}]):
            with self.subTest(traces=traces):
                unknown = deepcopy(raw)
                normalize_traced_spaces(unknown, traces)
                self.assertEqual(unknown, raw)

    def test_isolated_letter_list_marker_keeps_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'list.pdf'
            with fitz.open() as doc:
                p = doc.new_page()
                p.insert_text((40,100),'a)',fontsize=10,fontname='tiro')
                p.insert_text((58,100),'All-day percentiles describe precipitation extremes.',fontsize=10,fontname='tiro')
                p.insert_text((58,112),'The continuation remains part of this numbered item.',fontsize=10,fontname='tiro')
                doc.save(source)
            plan = extract_layout(source,paragraph=True)
            blocks = {b['id']:b for b in plan['blocks']}
            self.assertEqual(len(plan['groups']),1)
            self.assertTrue(blocks[plan['groups'][0][0]]['text'].startswith('a) All-day'))

    def test_subscript_product_and_prime_stay_in_original_math_anchor(self):
        from paperlocale.source_layout import _parts
        def span(text,x,y,size,font):
            return {'font':font,'flags':4,'size':size,'origin':[x,y],
                    'chars':[{'c':c,'origin':[x+i*4,y],'bbox':[x+i*4,y-size,x+(i+1)*4,y+2]}
                             for i,c in enumerate(text)]}
        spans=[span('with ',40,100,10,'Times-Roman'),span('g',60,100,10,'Times-Italic'),
               span('′ = ',64,100,10,'Times-Roman'),span('f',80,100,10,'Times-Italic'),
               span('w',84,102,7,'Times-Italic'),span('g',88,100,10,'Times-Italic'),
               span('w',92,102,7,'Times-Italic'),span(' one finds',96,100,10,'Times-Roman')]
        parts=_parts({'lines':[{'spans':spans}]},1,[],paragraph=True)
        self.assertEqual(''.join(p['text'] for p in parts if not p['fixed']),'with  one finds')
        self.assertIn('g′ = fwgw',''.join(p['text'] for p in parts if p['fixed']))

    def test_same_baseline_math_fragments_do_not_start_a_new_paragraph(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'fragment.pdf'
            with fitz.open() as doc:
                p = doc.new_page()
                p.insert_text((40,100),'where g = f',fontsize=10,fontname='tiro')
                p.insert_text((92,102),'d',fontsize=7,fontname='tiit')
                p.insert_text((92,96),' + ',fontsize=10,fontname='tiro')
                p.insert_text((108,100),'g and further prose continues here.',fontsize=10,fontname='tiro')
                p.insert_text((40,112),'The continuation belongs to the same paragraph.',fontsize=10,fontname='tiro')
                doc.save(source)
            plan = extract_layout(source,paragraph=True)
            blocks = {b['id']:b for b in plan['blocks']}
            self.assertEqual(len(plan['groups']),1)
            text=' '.join(blocks[i]['text'] for i in plan['groups'][0])
            self.assertTrue(text.startswith('where'))
            self.assertIn('further prose',text)

    def test_two_line_regular_heading_does_not_absorb_following_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'heading.pdf'
            with fitz.open() as doc:
                p = doc.new_page()
                for x,y,text in [(40,100,'4.2.'),(65,100,'Spatial distribution of precipitation indices'),
                                 (40,112,'with 10 year return period'),
                                 (40,126.5,'Increasing rainfall was observed in this region.'),
                                 (40,138.5,'The observations support the following results.')]:
                    p.insert_text((x,y),text,fontsize=10)
                doc.save(source)
            plan = extract_layout(source,paragraph=True)
            blocks = {b['id']:b for b in plan['blocks']}
            self.assertEqual(len(plan['groups']),2)
            self.assertTrue(blocks[plan['groups'][1][0]]['text'].startswith('Increasing'))

    def test_letter_affiliations_and_long_institution_block_stay_original(self):
        from paperlocale.source_layout import _preserve_front_matter
        def block(text,y,size,parts=None):
            return {'kind':'body','page':1,'text':text,'rect':[40,y,400,y+20],
                    'parts':parts or [{'size':size,'text':text}]}
        institutions = 'a Department of Earth Science, Example University, City, Country; '*5
        blocks = [block('Precipitation extremes',50,18),
                  block('Jane Doea,b John Smithc',90,12,
                        [{'text':'Jane Doe','size':12},{'text':'a,b','size':8},
                         {'text':'John Smith','size':12},{'text':'c','size':8}]),
                  block(institutions,120,8),block('Abstract University observations support this study.',200,10)]
        _preserve_front_matter(blocks,paragraph=True)
        self.assertEqual([b['kind'] for b in blocks],['body','preserve','preserve','body'])

    def test_display_equation_separates_its_surrounding_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'equation.pdf'
            with fitz.open() as doc:
                p = doc.new_page()
                p.insert_text((40,100),'The distribution changes with',fontsize=10)
                p.insert_text((100,125),'x = y + z',fontsize=10)
                # 公式后的短解释框在左边，未必横向碰到居中公式。
                p.insert_text((40,150),'where x.',fontsize=10)
                doc.save(source)
            plan = extract_layout(source,[{'page':1,'rect':[98,112,160,129],'kind':'formula'}],paragraph=True)
            blocks = {b['id']:b for b in plan['blocks']}
            self.assertEqual(len(plan['groups']),2)
            self.assertTrue(blocks[plan['groups'][1][0]]['text'].startswith('where'))

    def test_appendix_after_acknowledgements_is_translated(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'appendix.pdf'
            with fitz.open() as doc:
                p = doc.new_page()
                for i,text in enumerate(['Acknowledgements', 'We thank the contributors.',
                                         'Appendix: Theoretical analysis',
                                         'A1. All-day versus wet-day percentiles',
                                         'The conditional probability follows this equation.',
                                         'Open Access This article is distributed freely.']):
                    p.insert_text((40,80+35*i),text,fontsize=10)
                doc.save(source)
            blocks = extract_layout(source, paragraph=True)['blocks']
            self.assertEqual([b['kind'] for b in blocks],
                             ['body','body','body','body','body','preserve'])

    def test_symbol_list_markers_stay_native_and_items_separate(self):
        # 用错误映射的“&”模拟出版社圆点编码。判断依据是独立符号行
        # 和悬挂几何，程序须保留源字形，不能把符号作为英文正文重排。
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'bullets.pdf'
            with fitz.open() as doc:
                p = doc.new_page()
                for y,label in [(100,'All-day percentiles describe intensity.'),
                                (136,'Frequency indices describe exceedance.')]:
                    p.insert_text((40,y),'&',fontsize=10)
                    p.insert_text((58,y),label,fontsize=10)
                    p.insert_text((58,y+12),'This is the continuation of the definition.',fontsize=10)
                doc.save(source)
            plan = extract_layout(source, paragraph=True)
            blocks = {b['id']:b for b in plan['blocks']}
            self.assertEqual(len(plan['groups']),2)
            self.assertTrue(all(b['kind']=='preserve' for b in blocks.values() if b['text']=='&'))
            self.assertTrue(all('&' not in blocks[i]['text'] for g in plan['groups'] for i in g))

    def test_normal_prose_after_subscript_is_not_frozen_as_superscript(self):
        from paperlocale.source_layout import _parts
        def span(text, x, y, size, flags, font='Times-Roman'):
            chars=[{'c':c,'origin':[x+i*5,y],'bbox':[x+i*5,y-size,x+(i+1)*5,y+2]}
                   for i,c in enumerate(text)]
            return {'font':font,'flags':flags,'size':size,'origin':[x,y],'chars':chars}
        spans=[span('k=1',40,106,7,4), span('denotes the observed series',55,100,10,5),
               span('2',185,96,7,5)]
        parts=_parts({'lines':[{'spans':spans}]},1,[],paragraph=True)
        self.assertTrue(any('denotes' in p['text'] and not p['fixed'] for p in parts))
        self.assertTrue(any(p['text']=='2' and p['fixed'] for p in parts))

    def test_list_item_boundary_and_abbreviation_keep_complete_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                for x,y,text in [(40,100,'a) First definition includes all days, i.e.'),
                                 (58,112,'wet and dry days in the same reference set.'),
                                 (40,124,'b) Second definition uses only wet days.'),
                                 (58,136,'It excludes dry days from the reference set.')]:
                    page.insert_text((x,y),text,fontsize=10)
                doc.save(source)
            plan = extract_layout(source, paragraph=True)
            blocks = {b['id']: b for b in plan['blocks']}
            paragraphs = [' '.join(blocks[i]['text'] for i in g) for g in plan['groups']]
            self.assertEqual(len(paragraphs), 2)
            self.assertIn('wet and dry days', paragraphs[0])
            self.assertTrue(paragraphs[1].startswith('b)'))

    def test_year_at_line_start_is_not_a_list_item(self):
        from paperlocale.paragraph_layout import paragraph_groups
        def block(key,text,y):
            return {'id':key,'kind':'body','page':1,'rect':[40,y,350,y+10],
                    'text':text,'parts':[{'fixed':False,'size':10,'bold':False,
                                         'text':text,'rect':[40,y,350,y+10]}]}
        items=[block('a','The analysis follows Karl and Knight',90),
               block('b','(1998) and their definition of wet days.',102)]
        self.assertEqual(paragraph_groups(items),[['a','b']])

    def test_hanging_list_lines_form_one_frame_with_continuation_indent(self):
        from paperlocale.paragraph_layout import attach_frames
        def block(key, text, rect):
            return {'id': key, 'page': 1, 'rect': rect, 'text': text, 'kind': 'body',
                    'parts': [{'fixed': False, 'size': 10, 'bold': False,
                               'baseline': rect[3]-2, 'rect': rect, 'text': text}]}
        blocks = [block('a', 'a) All-day percentiles and supporting evidence', [40, 90, 350, 101]),
                  block('b', 'continue with the complete explanation.', [58, 103, 350, 139])]
        units = [{}]
        attach_frames(units, {'blocks': blocks, 'groups': [['a', 'b']]})
        self.assertEqual(len(units[0]['frames']), 1)
        self.assertEqual(units[0]['frames'][0]['hanging_indent'], 18)
        units[0].update(id='list', anchors=[])
        placements = fit_unit(units[0], '全日百分位数用于描述降水事件的变化，并结合完整的统计证据解释结果。'*2,
                              fitz.Font('china-s'), None)
        first = min(p['baseline'] for p in placements)
        self.assertTrue(all(p['rect'][0] >= 58 for p in placements if p['baseline'] > first))

    def test_long_author_list_remains_original_before_abstract(self):
        from paperlocale.source_layout import _preserve_front_matter
        def block(text, y, size):
            return {'kind': 'body', 'page': 1, 'text': text, 'rect': [40,y,350,y+20],
                    'parts': [{'size': size}]}
        authors = ' & '.join(['Christoph Schär1', 'Nikolina Ban1', 'Erich M. Fischer1',
                             'Jan Rajczak1', 'Jürg Schmidli2', 'Christoph Frei3',
                             'Filippo Giorgi4', 'Thomas R. Karl5', 'Xuebin Zhang6'])
        blocks = [block('Percentile indices', 50, 16), block(authors, 90, 10),
                  block('Abstract Many climate studies assess trends and projections. '*4, 160, 10)]
        _preserve_front_matter(blocks, paragraph=True)
        self.assertEqual([b['kind'] for b in blocks], ['body', 'preserve', 'body'])

    def test_caption_numbers_do_not_turn_figure_references_into_captions(self):
        from paperlocale.source_layout import CAPTION
        for text in ['Figure 1. Locations of stations.', 'Fig. 2 Impact of definition.',
                     'Fig. 1 | Soil water observations.',
                     'Table III. Test values.', 'Table IV. Test values.']:
            self.assertIsNotNone(CAPTION.match(text), text)
        for text in ['Figure 5 displays the spatial patterns.',
                     'Figure 4(e) shows the largest value.',
                     'Figure 1a and b underline the key point.']:
            self.assertIsNone(CAPTION.match(text), text)

    def test_list_number_misdetected_as_formula_does_not_block_body_deletion(self):
        from paperlocale.safe_text import safe_erase_rectangles
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((40,100),'(1) The correlation coefficient is calculated below.',fontsize=10)
                page.insert_text((100,160),'x = 2',fontsize=10)
                page.insert_text((300,160),'(1)',fontsize=10)
                detections=[{'page':1,'kind':'formula_caption','rect':list(r)}
                            for r in page.search_for('(1)')]
                doc.save(source)
            plan=extract_layout(source,detections,paragraph=True)
            self.assertEqual(len([p for p in plan['protected_regions'] if p['kind']=='formula']),1)
            save_json(Path(tmp)/'plan.json',plan)
            _,units=load_plan(source,Path(tmp)/'plan.json',detections,paragraph=True)
            editable=[p for u in units for s in u['slots'] for p in s]+[p for u in units for p in u['anchors']]
            protected=plan['protected_regions']+[b for b in plan['blocks'] if b['kind'] not in ('body','caption')]
            self.assertTrue(safe_erase_rectangles(editable,protected,source))

    def test_native_block_with_two_indented_paragraphs_keeps_two_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                for x,y,text in [(40,100,'A first paragraph has a complete opening sentence and'),
                                 (40,112,'continues on this second line.'),
                                 (60,124,'A second paragraph starts at the original indent and'),
                                 (40,136,'continues without a new paragraph.')]:
                    page.insert_text((x,y),text,fontsize=10)
                doc.save(source)
            plan = extract_layout(source, paragraph=True)
            self.assertEqual(len(plan['groups']),2)
            self.assertEqual([len(g) for g in plan['groups']],[1,1])

    def test_cjk_spaces_removed_but_english_words_and_units_retained(self):
        self.assertEqual(compact_target('  土 壤\n水分 model name 3 mm 改善 。 '),
                         '土壤水分model name 3 mm改善。')

    def test_frame_boundary_prefers_clause_and_keeps_parenthesized_units(self):
        from paperlocale.paragraph_layout import frame_boundary
        tokens=list('研究识别了主要因素。我们发现不同响应。')
        split=frame_boundary(tokens,11,[])
        self.assertEqual(''.join(tokens[:split]),'研究识别了主要因素。')
        tokens=['增','强','（','month','{v0}','）','；','结','果']
        self.assertEqual(frame_boundary(tokens,4,[{'text':'−1'}]),7)

    def test_frame_boundary_does_not_overfill_narrow_sidebar_line(self):
        """句读远离首行预算时按容量切分，让后续三行容纳完整译文。"""
        from paperlocale.paragraph_layout import frame_boundary, fit_paragraph
        target='未来变暖预计使DPATs转向高温干旱主导路径，加剧生态系统损失与暴露风险'
        tokens=list(target.replace('DPATs', 'D'))
        self.assertLessEqual(frame_boundary(tokens, 13, []), 16)
        unit={'id':'sidebar','source':'Future warming increases the risk', 'anchors':[], 'frames':[
            {'page':1,'rect':[41.44,225.34,142.96,232.31],'size':6.97,'bold':False,
             'leading':8.37,'indent':0,'weight':36},
            {'page':1,'rect':[41.44,234.30,150.24,259.19],'size':6.97,'bold':False,
             'leading':8.37,'indent':0,'weight':89}]}
        placed=fit_paragraph(unit,target,fitz.Font('china-s'),None,None)
        self.assertEqual(''.join(p['target'] for p in placed),target)

    def test_caption_translated_and_inline_citation_flows_without_holes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source.pdf'; run=root/'run'
            with fitz.open() as doc:
                p=doc.new_page(width=400,height=400)
                p.draw_rect((40,160,360,240),fill=(.1,.3,.6))
                p.insert_text((40,90),'The first paragraph explains water and vegetation [1].',fontsize=10)
                p.insert_link({'kind':fitz.LINK_URI,'from':p.search_for('[1]')[0], 'uri':'https://example.org/ref/1'})
                p.insert_text((40,102),'The remaining sentence explains the same result.',fontsize=10)
                p.insert_text((40,260),'Fig. 1 | Water and vegetation response.',fontsize=9)
                doc.save(source)
            font=root/'font.ttf';font.write_bytes(fitz.Font('china-s').buffer)
            initialize_run(source_pdf=source,run_dir=run,source_language='en',target_language='zh-CN')
            class Provider(TranslationProvider):
                def translate(self,segments,context):
                    return [Translation(s.id,'Fig. 1 | 水分与植被响应。' if 'Fig.' in s.source else
                                        '水分影响植被{v0}。') for s in segments]
            with patch('paperlocale.layout_detection.detect_regions',return_value=[]), patch('paperlocale.preserved_workflow.qa_run'):
                result=run_preserved(run,provider=Provider(),domain=load_domain_pack('ecology'),
                                     plan_path=None,font_file=font,paragraph=True)
            with fitz.open(result['rendered_pdf']) as doc:
                text=doc[0].get_text()
                self.assertNotIn('Water and vegetation response',text)
                self.assertIn('水分与植被响应',text.replace('\n',''))
                self.assertIn('[1]',text.replace('\n',''))
                # 引文原来在句尾约 x=270；译文只需几十点宽，应随文字左移。
                citation=doc[0].search_for('[1]')[0]
                self.assertLess(citation.x0,150)
                link=doc[0].get_links()[0]
                self.assertEqual(link['uri'],'https://example.org/ref/1')
                self.assertLess(link['from'].x0,150)
                self.assertTrue(link['from'].intersects(citation))

    def test_cross_page_frames_keep_one_translation_and_no_blank_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source.pdf'
            with fitz.open() as doc:
                doc.new_page().insert_text((40,700),'This complete source sentence continues across the boundary with',fontsize=10)
                doc.new_page().insert_text((40,90),'the remaining result and its evidence.',fontsize=10)
                doc.save(source)
            plan=extract_layout(source,paragraph=True);save_json(root/'plan.json',plan)
            _,units=load_plan(source,root/'plan.json',paragraph=True)
            self.assertEqual(len(units),1)
            placements=fit_unit(units[0],'这个完整段落跨页后仍然保持原有语义连续。',fitz.Font('china-s'),None)
            self.assertEqual({p['page'] for p in placements},{1,2})
            self.assertEqual(''.join(p['target'] for p in placements),'这个完整段落跨页后仍然保持原有语义连续。')

    def test_heading_with_regular_digits_does_not_merge_with_body(self):
        from paperlocale.paragraph_layout import paragraph_groups
        def block(key,text,parts,y):
            return {'id':key,'kind':'body','page':1,'rect':[40,y,290,y+10],
                    'text':text,'parts':parts}
        def part(text,bold,x,size=10):
            return {'text':text,'bold':bold,'fixed':False,'size':size,'rect':[x,90,x+100,100]}
        heading=block('h','Patterns during 1982–2019', [part('Patterns during ',True,40),part('1982–2019',False,180)],90)
        body=block('b','We measured the patterns of soil moisture.',[part('We measured the patterns of soil moisture.',False,40)],102)
        self.assertEqual(paragraph_groups([heading,body]),[['h'],['b']])

    def test_inline_verification_rejects_changed_glyph(self):
        from paperlocale.paragraph_layout import verify_inline
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'wrong.pdf'
            with fitz.open() as doc:
                doc.new_page().insert_text((40,100),'2',fontsize=10)
                doc.save(source)
            item={'page':1,'shift':[0,0],'inline_anchor':{'page':1,'text':'1',
                  'glyphs':[{'origin':[40,100],'size':10,'color':[0], 'text':'1'}]}}
            with self.assertRaisesRegex(ValueError,'原字形重放校验失败'):
                verify_inline(source,[item])
