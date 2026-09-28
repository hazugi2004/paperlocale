"""公式全局框、正文重音的分类回归；使用自造页面而非论文私有文件。"""
from pathlib import Path
import tempfile
import unittest
import pymupdf as fitz
from paperlocale.source_layout import extract_layout, _parts


class MathLineGeometryTests(unittest.TestCase):
    def test_formula_baseline_does_not_absorb_adjacent_prose(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((50,200),'SSTCI = 2',fontsize=12)
                page.insert_text((50,230),'The index describes compound events.',fontsize=12)
                doc.save(path)
            # 视觉框涵盖真实公式及基线，但不足原字体全局框面积的一半。
            plan=extract_layout(path,[{'page':1,'kind':'isolate_formula','rect':[49,197,140,201]}],paragraph=True)
            formula=next(b for b in plan['blocks'] if b['text']=='SSTCI = 2')
            prose=next(b for b in plan['blocks'] if b['text'].startswith('The index'))
            self.assertEqual(formula['kind'],'formula')
            self.assertEqual(prose['kind'],'body')

    def test_word_accent_is_text_but_variable_accent_stays_fixed(self):
        def part(text):
            spans=[]
            for i,c in enumerate(text):
                mark=c=='\u0303'
                spans.append({'font':'Fixture','flags':1 if mark else 0,'size':7 if mark else 10,
                    'origin':(50+i*5,97 if mark else 100),
                    'chars':[{'c':c,'origin':(50+i*5,97 if mark else 100),
                              'bbox':(50+i*5,89,50+i*5+(0 if mark else 5),102)}]})
            return _parts({'lines':[{'spans':spans}]},1,[],paragraph=True)
        word=part('Nin\u0303o')
        self.assertEqual(len(word),1)
        self.assertFalse(word[0]['fixed'])
        self.assertEqual(word[0]['text'],'Nin\u0303o')
        variable=part('x\u0303')
        self.assertTrue(next(p for p in variable if '\u0303' in p['text'])['fixed'])

    def test_single_metadata_bitmap_is_distinct_from_scanned_body(self):
        from io import BytesIO
        from PIL import Image,ImageDraw
        with tempfile.TemporaryDirectory() as folder:
            for height in [12,50]:
                path=Path(folder)/f'source-{height}.pdf'
                bitmap=Image.new('RGB',(180,height*2),'white');ImageDraw.Draw(bitmap).text((0,0),'Published online: 2026',fill='black')
                buffer=BytesIO();bitmap.save(buffer,format='PNG')
                with fitz.open() as doc:
                    p=doc.new_page();p.insert_text((40,200),'Accepted: 16 January 2026',fontsize=8)
                    p.insert_image(fitz.Rect(40,210,160,210+height),stream=buffer.getvalue())
                    p.insert_text((220,210),'This is the article abstract.',fontsize=11)
                    doc.save(path)
                plan=extract_layout(path,[{'page':1,'kind':'plain text','rect':[40,210,160,210+height]}],paragraph=True)
                conflict=any('图片外框覆盖' in issue for issue in plan['issues'])
                self.assertEqual(conflict,height>12)
