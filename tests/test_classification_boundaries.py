"""Publisher sidebars and table headings cannot hide scientific reading content."""
from collections import Counter
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import _definition_rows, extract_layout, load_plan, save_json, units_from_plan


class ClassificationBoundaryTests(unittest.TestCase):
    def test_detached_soft_hyphen_in_detected_caption_preserves_only_the_glyph_box(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'caption.pdf'
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((40, 100), 'Ordinary body text remains translatable.')
                page.insert_text((40, 320), ' ' * 30 + '-')
                page.insert_text((40, 345), 'Figure 1. The study region.')
                # 模拟出版社字体的 ToUnicode：字形为连字符，语义是可选断行。
                cmap = document.get_new_xref()
                document.update_object(cmap, '<<>>')
                document.update_stream(cmap, b'begincmap /CMapType 2 def '
                    b'1 begincodespacerange <00> <FF> endcodespacerange '
                    b'1 beginbfchar <2D> <00AD> endbfchar endcmap')
                document.xref_set_key(page.get_fonts()[0][0], 'ToUnicode', f'{cmap} 0 R')
                document.save(source)

            detections = [{'page': 1, 'kind': 'figure_caption', 'rect': [35, 305, 320, 355]}]
            old = extract_layout(source, detections, paragraph=True, spacing_revision=1)
            current = extract_layout(source, detections, paragraph=True)
            previous_marker = next(b for b in old['blocks'] if b['text'] == '\u00ad')
            marker = next(b for b in current['blocks'] if b['text'] == '\u00ad')
            self.assertEqual(previous_marker['kind'], 'caption')
            self.assertEqual(marker['kind'], 'preserve')
            self.assertEqual(marker['parts'], previous_marker['parts'])
            self.assertEqual(marker['rect'], marker['parts'][0]['chars'][-1]['rect'])
            self.assertLess(marker['rect'][2] - marker['rect'][0], 5)
            path = root / 'plan.json'
            save_json(path, current)
            loaded, units = load_plan(source, path, detections, paragraph=True)
            self.assertEqual(loaded, current)
            self.assertEqual([unit['source'] for unit in units],
                             ['Ordinary body text remains translatable.', 'Figure 1. The study region.'])

    def test_detached_formula_with_underscore_keeps_its_original_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'formula.pdf'
            with fitz.open() as document:
                page = document.new_page()
                # 斜体变量形成固定锚点，下划线模拟正文字体提取的独立痕迹。
                page.insert_text((40, 100), 'Tv', fontname='tiit', fontsize=10)
                page.insert_text((51, 100), '_', fontname='tiro', fontsize=10)
                page.insert_text((40, 170), 'Tv', fontname='tiit', fontsize=10)
                page.insert_text((51, 170), '_ where temperature changes.', fontname='tiro', fontsize=10)
                page.insert_text((40, 240), 'The underscore _ belongs to the prose.')
                document.save(source)

            old = extract_layout(source, paragraph=True, spacing_revision=2)
            current = extract_layout(source, paragraph=True)
            previous_formula, formula = old['blocks'][0], current['blocks'][0]
            self.assertEqual(previous_formula['kind'], 'body')
            self.assertEqual(formula['kind'], 'formula')
            self.assertEqual(formula['parts'], previous_formula['parts'])
            self.assertEqual(formula['rect'], previous_formula['rect'])
            self.assertEqual(current['spacing_revision'], 3)
            self.assertEqual(current['blocks'][1:], old['blocks'][1:])
            translations = [unit['source'] for unit in units_from_plan(current, paragraph=True)]
            self.assertEqual(translations, ['{v0}_ where temperature changes.',
                                            'The underscore _ belongs to the prose.'])
            path = root / 'plan.json'
            save_json(path, old)
            self.assertEqual(load_plan(source, path, paragraph=True)[0], old)

    def test_math_definition_grid_requires_repeated_aligned_pairs(self):
        def line(text, x, y, font):
            return {'bbox': [x, y, x + len(text) * 5, y + 10],
                    'spans': [{'font': font, 'size': 10, 'chars': [{'c': c} for c in text]}]}

        rows = [(line(symbol, 40, y, 'Symbol'), line(definition, 100, y, 'Times-Roman'))
                for symbol, definition, y in [('X', 'Constituent', 100), ('t', 'Time', 112),
                                               ('p', 'Pressure', 136)]]
        continued = line('in this equation', 100, 124, 'Times-Roman')
        lines = [*rows[0], *rows[1], continued, *rows[2]]
        native = {'number': 7, 'lines': lines}
        pieces = _definition_rows(native)
        self.assertEqual([entry for piece in pieces for entry in piece['lines']], lines)
        self.assertEqual([p.get('_definition_symbol', False) for p in pieces],
                         [True, False, True, False, True, False])
        self.assertEqual(pieces[3]['lines'], [rows[1][1], continued])
        self.assertEqual(pieces[3]['_definition_right'], continued['bbox'][2])
        unrelated = {**native, 'lines': [*lines[:-1], line('Pressure', 150, 136, 'Times-Roman')]}
        self.assertEqual(_definition_rows(unrelated), [unrelated])

    def test_frontiers_sidebar_does_not_preserve_unlabelled_abstract_or_keywords(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'frontiers.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=600,height=800)
                for x,y,text,font,size in (
                    (220,100,'Compound climate extremes','hebo',18),
                    (40,130,'OPEN ACCESS','hebo',9),
                    (40,155,'EDITED BY','hebo',8),
                    (40,175,'Jane Editor','helv',8),
                    (220,160,'Alice Smith and Bob Jones','helv',10),
                    (220,185,'Department of Climate, Example University','helv',9),
                    (220,400,'KEYWORDS','hebo',9),
                    (220,420,'drought, heatwave, compound extremes','helv',10),
                    (40,760,'Frontiers in Earth Science','helv',8),
                ):
                    page.insert_text((x,y),text,fontname=font,fontsize=size)
                page.insert_textbox(fitz.Rect(220,220,555,380),
                    'Droughts and heatwaves form compound extremes. This study reviews '
                    'climate hazards across several regions and compares the observed '
                    'drivers, event frequencies, and impacts using published evidence. '
                    'We explain the main scientific findings and their implications.',fontsize=11)
                page = doc.new_page(width=600,height=800)
                page.insert_text((40,100),'Introduction',fontname='hebo',fontsize=12)
                page.insert_text((40,125),'We analyse the regional observations.',fontsize=10)
                doc.save(source)
            old = extract_layout(source,paragraph=True,reading_structure=4)
            current = extract_layout(source,paragraph=True)
            self.assertEqual(current['reading_structure_revision'],5)
            for prefix in ('Droughts and heatwaves','KEYWORDS','drought, heatwave'):
                self.assertEqual(next(b['kind'] for b in old['blocks'] if b['text'].startswith(prefix)),'preserve')
                self.assertEqual(next(b['kind'] for b in current['blocks'] if b['text'].startswith(prefix)),'body')
            for prefix in ('OPEN ACCESS','EDITED BY','Jane Editor','Alice Smith','Department of'):
                self.assertEqual(next(b['kind'] for b in current['blocks'] if b['text'].startswith(prefix)),'preserve')
            unknown=extract_layout(source,paragraph=True,journal_adapt=False)
            self.assertEqual(next(b['kind'] for b in unknown['blocks']
                                  if b['text'].startswith('Droughts and heatwaves')),'preserve')
            def characters(plan):
                return Counter((c['text'],tuple(c['origin']),tuple(c['rect']))
                    for b in plan['blocks'] for p in b['parts'] for c in p['chars'])
            self.assertEqual(characters(old),characters(current))
            for name,plan in (('old',old),('new',current)):
                path=root/(name+'.json'); save_json(path,plan)
                self.assertEqual(load_plan(source,path,paragraph=True)[0],plan)

    def test_table_references_label_cannot_start_bibliography_or_hide_later_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'table-references.pdf'
            with fitz.open() as doc:
                page=doc.new_page(width=600,height=800)
                page.insert_text((40,70),'Table 3. Climate event methods.',fontsize=10)
                for y in (100,140,180): page.draw_line((40,y),(400,y))
                for x in (40,200,400): page.draw_line((x,100),(x,180))
                page.insert_text((50,120),'References',fontname='hebo',fontsize=10)
                page.insert_text((210,120),'Parameters',fontname='hebo',fontsize=10)
                page.insert_text((50,160),'Smith (2020)',fontsize=10)
                page.insert_text((210,160),'Event frequency',fontsize=10)
                page=doc.new_page(width=600,height=800)
                page.insert_text((40,100),'The most analysed parameter is the frequency of compound events.',fontsize=10)
                page.insert_text((40,125),'We compare the observed durations and regional climate drivers.',fontsize=10)
                page=doc.new_page(width=600,height=800)
                page.insert_text((40,100),'References',fontname='hebo',fontsize=12)
                page.insert_text((40,130),'Smith, A. (2020). Climate event observations. Journal 1, 1-20.',fontsize=10)
                page=doc.new_page(width=600,height=800)
                page.insert_text((40,100),'Jones, B. (2021). Regional climate drivers. Journal 2, 21-40.',fontsize=10)
                doc.save(source)
            detections=[{'page':1,'kind':'table','rect':[40,100,400,180]}]
            old=extract_layout(source,detections,paragraph=True,reading_structure=4)
            current=extract_layout(source,detections,paragraph=True)
            self.assertTrue(any(b['page']==1 and b['kind']=='reference' for b in old['blocks']))
            self.assertTrue(all(b['kind']=='body' for b in current['blocks'] if b['page']==2))
            self.assertTrue(all(b['kind']=='table' for b in current['blocks'] if b['page']==1 and b['text'].startswith('References')))
            self.assertTrue(all(b['kind']=='reference' for b in current['blocks'] if b['page']>=3))
            self.assertFalse(any('Jones' in u['source'] for u in units_from_plan(current,paragraph=True)))
            native=extract_layout(source,paragraph=True)
            self.assertTrue(all(b['kind']=='reference' for b in native['blocks'] if b['page']>=3))
            for name,plan in (('old',old),('new',current)):
                path=root/(name+'.json'); save_json(path,plan)
                self.assertEqual(load_plan(source,path,detections,paragraph=True)[0],plan)

    def test_right_column_declarations_above_references_do_not_end_bibliography(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'mixed-ending.pdf'
            with fitz.open() as doc:
                page=doc.new_page(width=600,height=800)
                page.insert_text((40,400),'References',fontname='hebo',fontsize=12)
                for y,text in ((430,'Smith, A. (2020). Climate observations.'),
                               (444,'Journal of compound extremes 1, 1-20.'),
                               (458,'https://doi.org/10.1000/climate-observations')):
                    page.insert_text((40,y),text,fontsize=9)
                page.insert_text((320,100),'Author contributions',fontname='hebo',fontsize=12)
                for y,text in ((125,'The authors designed the complete study.'),
                               (139,'They collected all regional observations.'),
                               (153,'They reviewed the manuscript and methods.')):
                    page.insert_text((320,y),text,fontsize=9)
                page.insert_text((320,300),'Supplementary material',fontname='hebo',fontsize=12)
                page.insert_text((320,325),'Additional observations are available online.',fontsize=9)
                for y,text in ((430,'Wang, B. (2021). Compound climate extremes.'),
                               (444,'Journal of climate variations 2, 21-40.'),
                               (458,'https://doi.org/10.1000/compound-extremes')):
                    page.insert_text((320,y),text,fontsize=9)
                page=doc.new_page(width=600,height=800)
                page.insert_text((40,100),'Zhang, C. (2022). Regional observations. Journal 3, 41-60.',fontsize=9)
                doc.save(source)
            plan=extract_layout(source,paragraph=True)
            for prefix in ('Author contributions','The authors','Supplementary material','Additional observations'):
                self.assertEqual(next(b['kind'] for b in plan['blocks'] if b['text'].startswith(prefix)),'body')
            for prefix in ('Smith,','Wang,','Zhang,'):
                self.assertEqual(next(b['kind'] for b in plan['blocks'] if b['text'].startswith(prefix)),'reference')

    def test_rotated_table_heading_uses_canonical_detection_coordinates(self):
        from paperlocale.references import _reference_geometry
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'rotated-table.pdf'
            with fitz.open() as doc:
                page=doc.new_page(width=600,height=800)
                page.insert_text((50,120),'References',fontname='hebo',fontsize=10)
                page.set_rotation(90)
                box=list(fitz.Rect(40,100,200,140)*page.rotation_matrix)
                page=doc.new_page(width=600,height=800)
                page.insert_text((40,100),'References',fontname='hebo',fontsize=12)
                page.insert_text((40,130),'Smith, A. (2020). Climate observations. Journal 1, 1-20.',fontsize=10)
                doc.save(source)
            old=_reference_geometry(source)
            self.assertEqual(old[0],[1,2])
            self.assertEqual(old[3],[])
            current=_reference_geometry(source,visual_exclusions=[{'page':1,'kind':'table','rect':box}])
            self.assertEqual(current[0],[2])
            self.assertTrue(current[3])
            self.assertTrue(all(r['page']==2 for r in current[3]))
