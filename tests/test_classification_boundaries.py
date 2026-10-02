"""Publisher sidebars and table headings cannot hide scientific reading content."""
from collections import Counter
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, load_plan, save_json, units_from_plan


class ClassificationBoundaryTests(unittest.TestCase):
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
