"""分式横线两侧的真实 PDF 字形必须整体保护，不能跨译文框拆开。"""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import (
    _preserve_vector_fractions, extract_layout, load_plan, save_json, units_from_plan,
)
from paperlocale.safe_text import prepare_source_layer, safe_erase_rectangles


def fraction_pdf(path, *, denominator=True, rule=True, subscript_y=198.8, continuation_dx=2):
    with fitz.open() as doc:
        page=doc.new_page(width=600,height=800)
        for y, text in ((166,'We calculate the probability of compound events.'),
                        (178,'Further we calculate the likelihood factor.'),
                        (194,'The expression is LMF')):
            page.insert_text((307,y),text,fontname='tiro',fontsize=10)
        x=307+fitz.get_text_length('The expression is LMF',fontname='tiro',fontsize=10)
        page.insert_text((x,194),' = ',fontname='tiro',fontsize=10)
        left=x+fitz.get_text_length(' = ',fontname='tiro',fontsize=10)
        def put(x,y,text,font,size):
            page.insert_text((x,y),text,fontname=font,fontsize=size)
            return x+fitz.get_text_length(text,fontname=font,fontsize=size)
        right=put(left,189.5,'P','tiit',7.57)
        right=put(right,190.767,'d-and-h','tiro',5.9776)
        if rule:
            page.draw_line((left,191.51),(right,191.51),width=.458)
        if denominator:
            x=put(left,197.54,'P','tiit',7.57)
            x=put(x,subscript_y,'d','tiro',5.9776)
            x=put(x,197.54,'×','tiro',7.57)
            x=put(x,197.54,'P','tiit',7.57)
            put(x,subscript_y,'h','tiro',5.9776)
        if continuation_dx is not None:
            page.insert_text((right+continuation_dx,194),'. Under independence,',fontname='tiro',fontsize=10)
        page.insert_text((307,206),'the factor equals one and can increase.',fontname='tiro',fontsize=10)
        doc.save(path)


class VectorFractionStructureTests(unittest.TestCase):
    def test_merged_label_and_cross_unit_fraction_remain_at_source_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'fraction.pdf'
            fraction_pdf(source)
            old=extract_layout(source,paragraph=True,fraction_structure=False)
            self.assertEqual(old['vector_fraction_revision'],2)
            old_units=units_from_plan(old,paragraph=True)
            numerator=next(u for u in old_units if 'The expression is' in u['source'])
            denominator=next(u for u in old_units if 'Under independence' in u['source'])
            self.assertNotEqual(numerator['id'],denominator['id'])
            self.assertEqual(numerator['anchors'][-1]['text'],'LMF = Pd-and-h')
            self.assertEqual(denominator['anchors'][0]['text'],'Pd×Ph')
            self.assertFalse(any(b['kind']=='formula' for b in old['blocks']))
            plan=extract_layout(source,paragraph=True)
            self.assertEqual(plan['vector_fraction_revision'],3)
            formulas=[b for b in plan['blocks'] if b['kind']=='formula']
            self.assertEqual(len(formulas),1)
            self.assertEqual(formulas[0]['text'],'LMF = Pd-and-h Pd×Ph')
            def characters(blocks):
                return Counter((c['text'],tuple(c['origin']),tuple(c['rect']))
                               for b in blocks for p in b['parts'] for c in p['chars'])
            self.assertEqual(characters(old['blocks']),characters(plan['blocks']))
            units=units_from_plan(plan,paragraph=True)
            self.assertTrue(all(not u['anchors'] for u in units))
            self.assertTrue(any('The expression is' in u['source'] for u in units))
            self.assertTrue(any('Under independence' in u['source'] for u in units))
            editable=[p for u in units for slot in u['slots'] for p in slot]
            erase=safe_erase_rectangles(editable,formulas,source)
            data=prepare_source_layer(source,editable,formulas,erase)
            with fitz.open(source) as original, fitz.open(stream=data,filetype='pdf') as cleared:
                expected={(c['text'],tuple(c['origin'])) for p in formulas[0]['parts'] for c in p['chars']
                          if c['text'].strip()}
                def glyphs(page):
                    return [(s['font'],s['size'],s['color'],c) for s in page.get_texttrace() for c in s['chars']
                            if any(chr(c[0])==text and all(abs(a-b)<.0001 for a,b in zip(c[2],origin))
                                   for text,origin in expected)]
                before,after=glyphs(original[0]),glyphs(cleared[0])
                self.assertEqual(len(before),len(expected))
                self.assertEqual(len(after),len(expected))
                for a,b in zip(before,after):
                    self.assertEqual(a[:3],b[:3])
                    self.assertEqual(a[3][:2],b[3][:2])
                    # MuPDF serializes origins to decimal PDF operands on redaction.
                    for x,y in zip(a[3][2]+a[3][3],b[3][2]+b[3][3]):
                        self.assertAlmostEqual(x,y,delta=.0001)
                # The neighboring prose can overlap font bounding boxes; compare the
                # native glyphs above, and the actual fraction rule pixels separately.
                clip=fitz.Rect(original[0].get_drawings()[0]['rect'])+(-.5,-.5,.5,.5)
                rule=original[0].get_drawings()[0]['rect']
                self.assertLessEqual(formulas[0]['rect'][0],rule.x0)
                self.assertGreaterEqual(formulas[0]['rect'][2],rule.x1)
                self.assertEqual(original[0].get_pixmap(matrix=fitz.Matrix(3,3),clip=clip).samples,
                                 cleared[0].get_pixmap(matrix=fitz.Matrix(3,3),clip=clip).samples)
                def drawings(page):
                    return [{k:v for k,v in d.items() if k!='seqno'} for d in page.get_drawings()]
                self.assertEqual(drawings(original[0]),drawings(cleared[0]))
            # Both recorded algorithms remain reproducible; loading never upgrades a plan.
            for name, value in (('old',old),('new',plan)):
                saved=root/(name+'.json'); save_json(saved,value)
                loaded,_=load_plan(source,saved,paragraph=True)
                self.assertEqual(loaded,value)

    def test_slice_does_not_protect_prose_merged_into_wide_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'fraction.pdf'; fraction_pdf(source)
            plan=extract_layout(source,paragraph=True,fraction_structure=False)
            blocks=deepcopy(plan['blocks'])
            upper=next(b for b in blocks if 'LMF = Pd-and-h' in b['text'])
            prefix,anchor=upper['parts'][-2:]
            anchor['chars']=prefix['chars']+anchor['chars']
            anchor['text']=prefix['text']+anchor['text']
            anchor['rect']=list(fitz.Rect(prefix['rect'])|fitz.Rect(anchor['rect']))
            upper['parts'].remove(prefix)
            _preserve_vector_fractions(source,blocks,include_label=True,glyph_structure=True)
            formula=next(b for b in blocks if b['kind']=='formula')
            self.assertEqual(formula['text'],'LMF = Pd-and-h Pd×Ph')
            self.assertIn('The expression is',''.join(p['text'] for p in upper['parts']))

    def test_denominator_run_keeps_its_lowered_trailing_subscript(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'lower-subscript.pdf'
            fraction_pdf(source,subscript_y=199.5)
            plan=extract_layout(source,paragraph=True)
            formula=next(b for b in plan['blocks'] if b['kind']=='formula')
            self.assertEqual(formula['text'],'LMF = Pd-and-h Pd×Ph')
            self.assertEqual(formula['parts'][-1]['chars'][-1]['text'],'h')
            self.assertAlmostEqual(formula['parts'][-1]['chars'][-1]['origin'][1],199.5,places=3)
            self.assertTrue(all(not u['anchors'] for u in units_from_plan(plan,paragraph=True)))

    def test_fraction_already_merged_into_one_anchor_stays_fixed(self):
        for continuation in (None,30):
            with self.subTest(continuation=continuation), tempfile.TemporaryDirectory() as directory:
                source=Path(directory)/'merged-fraction.pdf'
                fraction_pdf(source,continuation_dx=continuation)
                old=extract_layout(source,paragraph=True,fraction_structure=False)
                self.assertTrue(any('Pd-and-h\nPd×Ph' in a['text']
                                    for u in units_from_plan(old,paragraph=True) for a in u['anchors']))
                plan=extract_layout(source,paragraph=True)
                formulas=[b for b in plan['blocks'] if b['kind']=='formula']
                self.assertEqual(len(formulas),1)
                self.assertEqual(formulas[0]['text'].replace(' ',''),'LMF=Pd-and-hPd×Ph')
                self.assertTrue(all(not u['anchors'] for u in units_from_plan(plan,paragraph=True)))

    def test_underline_or_missing_rule_does_not_become_fraction(self):
        for denominator,rule in ((False,True),(True,False)):
            with self.subTest(denominator=denominator,rule=rule), tempfile.TemporaryDirectory() as directory:
                source=Path(directory)/'ordinary.pdf'
                fraction_pdf(source,denominator=denominator,rule=rule)
                plan=extract_layout(source,paragraph=True)
                self.assertFalse(any(b['kind']=='formula' for b in plan['blocks']))

    def test_underline_between_prose_lines_keeps_both_variable_anchors(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'underlined-prose.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                prefix='We measured '
                x=50+fitz.get_text_length(prefix,fontname='tiro',fontsize=11)
                width=fitz.get_text_length('P',fontname='tiit',fontsize=11)
                for y in (180,192):
                    page.insert_text((50,y),prefix,fontname='tiro',fontsize=11)
                    page.insert_text((x,y),'P',fontname='tiit',fontsize=11)
                    page.insert_text((x+width,y),' at the station.',fontname='tiro',fontsize=11)
                page.draw_line((x-.5,182),(x+width+.5,182),width=.5)
                doc.save(source)
            old=extract_layout(source,paragraph=True,fraction_structure=False)
            self.assertEqual([b['text'] for b in old['blocks'] if b['kind']=='formula'],['P P'])
            plan=extract_layout(source,paragraph=True)
            self.assertFalse(any(b['kind']=='formula' for b in plan['blocks']))
            units=units_from_plan(plan,paragraph=True)
            self.assertEqual([a['text'] for u in units for a in u['anchors']],['P','P'])
            self.assertEqual(sum(u['source'].count('{v') for u in units),2)
            def characters(value):
                return Counter((c['text'],tuple(c['origin']),tuple(c['rect']))
                               for b in value['blocks'] for p in b['parts'] for c in p['chars'])
            self.assertEqual(characters(old),characters(plan))
            for name,value in [('old',old),('new',plan)]:
                path=Path(directory)/(name+'.json');save_json(path,value)
                loaded,_=load_plan(source,path,paragraph=True)
                self.assertEqual(loaded,value)


if __name__=='__main__':
    unittest.main()
