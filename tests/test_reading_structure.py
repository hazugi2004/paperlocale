"""Source-backed failures: pipe-separated authors/headings and post-reference captions."""
from pathlib import Path
import tempfile
import unittest
import pymupdf as fitz
from paperlocale.source_layout import extract_layout, load_plan, save_json

class ReadingStructureTests(unittest.TestCase):
    def test_pipe_authors_stay_original_and_headings_stay_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                for y,text,font,size in [
                    (60,'Compound climate extremes','hebo',18),
                    (90,'Jane Smith | John Wang | Daniel Tawia Hagan','helv',10),
                    (130,'Abstract','hebo',10),
                    (150,'This study evaluates compound drought events.','helv',10),
                    (200,'2 | Data and Methods','hebo',10),
                    (222,'2.1 | Data','hebo',10),
                    (245,'We use daily measurements from climate stations.','helv',10),
                ]:page.insert_text((40,y),text,fontname=font,fontsize=size)
                doc.save(source)
            plan=extract_layout(source,paragraph=True)
            blocks={b['id']:b for b in plan['blocks']}
            author=next(b for b in blocks.values() if 'Jane Smith' in b['text'])
            self.assertEqual(author['kind'],'preserve')
            groups=[' '.join(blocks[k]['text'] for k in group) for group in plan['groups']]
            self.assertIn('2 | Data and Methods',groups)
            self.assertIn('2.1 | Data',groups)
            self.assertIn('We use daily measurements from climate stations.',groups)

    def test_supporting_information_after_references_remains_translatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf';path=Path(tmp)/'plan.json'
            with fitz.open() as doc:
                page=doc.new_page()
                for y,text,font in [
                    (100,'The main body describes the experiment.','helv'),
                    (200,'References','hebo'),
                    (225,'Smith, J. 2020. Compound climate extremes. Journal 1, 1-20.','helv'),
                    (270,'Supporting Information','hebo'),
                    (295,'Figure S1: Seasonal counts of compound climate events.','helv'),
                ]:page.insert_text((40,y),text,fontname=font,fontsize=10)
                doc.save(source)
            plan=extract_layout(source,paragraph=True)
            supporting=next(b for b in plan['blocks'] if b['text']=='Supporting Information')
            self.assertEqual(supporting['kind'],'body')
            caption=next(b for b in plan['blocks'] if b['text'].startswith('Figure S1'))
            self.assertIn(caption['kind'],('body','caption'))
            save_json(path,plan);load_plan(source,path,paragraph=True)
            old=extract_layout(source,paragraph=True,reading_structure=False)
            self.assertNotIn('reading_structure_revision',old)
            save_json(path,old);load_plan(source,path,paragraph=True)

    def test_stacked_main_and_subheading_remain_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((40,100),'MATERIALS AND METHODS',fontname='hebo',fontsize=9)
                page.insert_text((40,111),'Data',fontname='hebo',fontsize=9.5)
                page.insert_text((40,125),'We use daily observations from weather stations.',fontsize=9.5)
                doc.save(source)
            plan=extract_layout(source,paragraph=True)
            blocks={b['id']:b for b in plan['blocks']}
            groups=[' '.join(blocks[k]['text'] for k in g) for g in plan['groups']]
            self.assertIn('MATERIALS AND METHODS',groups)
            self.assertIn('Data',groups)

    def test_acknowledgments_misdetected_as_abandon_are_translatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((40,100),'REFERENCES AND NOTES',fontname='hebo',fontsize=10)
                page.insert_text((40,120),'1. Smith, J. 2020. Climate extremes. Journal 1, 1-20.',fontsize=10)
                page.insert_text((40,150),'Acknowledgments:',fontname='hebo',fontsize=10)
                page.insert_text((135,150),'We thank the observing network.',fontsize=10)
                doc.save(source)
            plan=extract_layout(source,[{'page':1,'kind':'abandon','rect':[39,138,360,156]}],paragraph=True)
            ack=next(b for b in plan['blocks'] if b['text'].startswith('Acknowledgments'))
            self.assertEqual(ack['kind'],'body')
            self.assertTrue(any(ack['id'] in g for g in plan['groups']))

    def test_science_advtt_styles_are_explicit(self):
        from paperlocale.journals import publisher_style_flags
        self.assertEqual(publisher_style_flags('science','AdvTT837b8832.BI',4,science_styles=True)&18,18)
        self.assertEqual(publisher_style_flags('science','AdvTT837b8832.BI',4),4)
        self.assertEqual(publisher_style_flags('generic','Unknown.BI',4,science_styles=True),4)

    def test_diagnostic_paths_are_not_mistaken_for_keys(self):
        from paperlocale.recovery import _message
        message=_message(ValueError('/work/task-2/input.pdf missing; key=sk-private-secret'))
        self.assertIn('/work/task-2/input.pdf',message)
        self.assertNotIn('sk-private-secret',message)

    def test_early_pdf_footer_does_not_expand_reference_box_over_supporting_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((400,810),'1 of 1',fontsize=10)
                page.insert_text((40,100),'References',fontname='hebo',fontsize=10)
                page.insert_text((40,125),'Smith, J. 2020. Climate extremes. Journal 1, 1-20.',fontsize=10)
                page.insert_text((40,200),'Supporting Information',fontname='hebo',fontsize=10)
                page.insert_text((40,225),'Figure S1: Counts of compound extremes.',fontsize=10)
                doc.save(source)
            plan=extract_layout(source,paragraph=True)
            self.assertEqual(next(b for b in plan['blocks'] if b['text']=='Supporting Information')['kind'],'body')

    def test_unflagged_roman_subscripts_keep_their_complete_base_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                for x,word in [(80,'0001'),(170,'PEI')]:
                    page.insert_text((x,100),word,fontname='Times-Roman',fontsize=10)
                    w=fitz.get_text_length(word,fontname='Times-Roman',fontsize=10)
                    page.insert_text((x+w,101.5),'2' if word=='0001' else '30',fontname='Times-Roman',fontsize=7.5)
                page.insert_text((220,100),'describes the event.',fontname='Times-Roman',fontsize=10)
                doc.save(source)
            plan=extract_layout(source,paragraph=True)
            parts=[p for b in plan['blocks'] for p in b['parts']]
            self.assertTrue(any(p['fixed'] and '00012' in p['text'] for p in parts))
            self.assertTrue(any(p['fixed'] and 'PEI30' in p['text'] for p in parts))
            self.assertTrue(any(not p['fixed'] and 'describes' in p['text'] for p in parts))
            old=extract_layout(source,paragraph=True,script_geometry=False)
            self.assertFalse(any(p['fixed'] and '00012' in p['text'] for b in old['blocks'] for p in b['parts']))

    def test_appendix_captions_are_separate_and_abandoned_headings_translated(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((40,100),'Appendix B: Supplementary table',fontname='hebo',fontsize=10)
                page.insert_text((40,150),'Figure A2. Probability estimates and their difference.',fontsize=10)
                page.insert_text((40,250),'Figure A3. Distribution of the observations.',fontsize=10)
                page.insert_text((40,350),'Competing interests.',fontname='hebo',fontsize=10)
                page.insert_text((145,350),'The authors declare no competing interests.',fontsize=10)
                doc.save(source)
            plan=extract_layout(source,[{'page':1,'kind':'abandon','rect':[39,87,350,105]},
                {'page':1,'kind':'abandon','rect':[39,337,450,355]}],paragraph=True)
            blocks={b['id']:b for b in plan['blocks']}
            groups=[' '.join(blocks[k]['text'] for k in g) for g in plan['groups']]
            self.assertIn('Figure A2. Probability estimates and their difference.',groups)
            self.assertIn('Figure A3. Distribution of the observations.',groups)
            self.assertEqual(next(b for b in blocks.values() if b['text'].startswith('Appendix B'))['kind'],'body')
            self.assertEqual(next(b for b in blocks.values() if b['text'].startswith('Competing interests'))['kind'],'body')
            from paperlocale.source_layout import APPENDIX_CAPTION
            self.assertIsNone(APPENDIX_CAPTION.match('Figure A3 shows the observations.'))

    def test_plain_font_equations_keep_original_glyphs_across_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((40,100),'Risk = Hazard *',fontsize=10)
                page.insert_text((40,112),'Vulnerability * Exposure. This is ordinary prose.',fontsize=10)
                page.insert_text((40,150),'The expectation is (1 - 0.9)*(1 - 0.9)*113 = 1.13.',fontsize=10)
                doc.save(source)
            plan=extract_layout(source,paragraph=True)
            parts=[p for b in plan['blocks'] for p in b['parts']]
            fixed=' '.join(p['text'] for p in parts if p['fixed'])
            self.assertIn('Risk = Hazard *',fixed)
            self.assertIn('Vulnerability * Exposure',fixed)
            self.assertIn('(1 - 0.9)*(1 - 0.9)*113 = 1.13',fixed)
            self.assertTrue(any(not p['fixed'] and 'ordinary prose' in p['text'] for p in parts))

    def test_bold_heading_and_bold_italic_subheading_do_not_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((40,100),'Statistical analysis',fontname='hebo',fontsize=10)
                page.insert_text((40,112),'Interannual correlations and model-data comparison',fontname='hebi',fontsize=10)
                page.insert_text((40,126),'We computed correlations between the variables.',fontsize=10)
                doc.save(source)
            plan=extract_layout(source,paragraph=True);b={v['id']:v for v in plan['blocks']}
            groups=[' '.join(b[i]['text'] for i in g) for g in plan['groups']]
            self.assertIn('Statistical analysis',groups)
            self.assertIn('Interannual correlations and model-data comparison',groups)
