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
