"""0.7.5 的标题、悬挂段落、符号语义与量值边界回归；不分发论文。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, units_from_plan
from paperlocale.quantities import find_quantities


class WangRegressions(unittest.TestCase):
    def test_italic_heading_is_translatable_as_one_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'heading.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((50,100),'c. Grades of compound droughts and heat waves by',fontname='tiit',fontsize=10)
                page.insert_text((62,112),'intensity',fontname='tiit',fontsize=10)
                page.insert_text((50,140),'The following paragraph is separate.',fontsize=10)
                doc.save(path)
            plan = extract_layout(path, paragraph=True)
            units = units_from_plan(plan, paragraph=True)
            title = next(u for u in units if u['source'].startswith('c.'))
            self.assertEqual(title['source'],'c. Grades of compound droughts and heat waves by intensity')
            self.assertEqual(title['anchors'], [])
            self.assertEqual(len(title['frames']),1)
            self.assertTrue(any('following paragraph' in u['source'] for u in units if u is not title))

    def test_hanging_list_has_one_continuous_frame_per_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'list.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                for y,text,x in [(100,'2) The magnitude of compound events was',50),
                                 (112,'analyzed over the basin. The numbers of',62),
                                 (124,'severe events decreased. Another sentence',62),
                                 (136,'continues to the final short line.',62),
                                 (148,'3) Since 2009, there have been more events.',50),
                                 (160,'The conclusion continues on this line.',62)]:
                    page.insert_text((x,y),text,fontsize=10)
                doc.save(path)
            units=units_from_plan(extract_layout(path,paragraph=True),paragraph=True)
            self.assertEqual(len(units),2)
            self.assertEqual([len(u['frames']) for u in units],[1,1])
            self.assertIn('final short line.',units[0]['source'])

    def test_trend_quantity_binds_sign_power_and_count_unit(self):
        quantity=find_quantities('decreased at −2.4 × 10−3 events yr−1')[0]
        self.assertEqual(quantity.values, ('-2.4x10-3',))
        self.assertEqual(quantity.unit,(('event',1),('yr',-1)))
        self.assertEqual(find_quantities('2.77 events decade−1')[0].unit,(('decade',-1),('event',1)))
        self.assertEqual(find_quantities('−10−3 events yr−1')[0].values,('-10-3',))

    def test_symbol_outline_changes_semantics_only_with_exact_match(self):
        from paperlocale.source_symbols import restore_source_symbols
        from fontTools.pens.recordingPen import RecordingPen
        import hashlib
        class Shape:
            def draw(self,pen):
                pen.moveTo((0,200));pen.lineTo((400,200));pen.lineTo((400,250));pen.closePath()
        class Top:
            CharStrings={'two':Shape()}
        pen=RecordingPen();Shape().draw(pen)
        key=hashlib.sha256(repr(pen.value).encode()).hexdigest()
        def raw():
            return [{'lines':[{'spans':[{'font':'misnamed','chars':[{
                'c':'2','origin':[20,30],'bbox':[20,20,25,30]}]}]}]}]
        glyphs=[{'xref':1,'name':'two'}]
        with patch('paperlocale.source_symbols._source_anchor_glyphs',return_value=glyphs):
            unknown=raw();restore_source_symbols(None,unknown,{1:Top()})
            self.assertEqual(unknown[0]['lines'][0]['spans'][0]['chars'][0]['c'],'2')
            with patch('paperlocale.source_symbols.REVIEWED_OUTLINES',{key:'−'}):
                known=raw();restore_source_symbols(None,known,{1:Top()})
                char=known[0]['lines'][0]['spans'][0]['chars'][0]
                self.assertEqual((char['c'],char['native_text']),('−','2'))
                self.assertEqual(char['bbox'],[20,20,25,30])

    def test_repaired_relations_do_not_swallow_prose(self):
        from paperlocale.source_layout import _parts
        with fitz.open() as doc:
            page=doc.new_page();page.insert_text((50,100),'a period of $3 days and SPEI , 20.8',fontsize=10)
            block=page.get_text('rawdict')['blocks'][0]
            for line in block['lines']:
                for span in line['spans']:
                    for c in span['chars']:
                        if c['c'] in '$,2':
                            c['native_text']=c['c'];c['c']={'$':'≥',',':'<','2':'−'}[c['c']]
            parts=_parts(block,1,[],paragraph=True)
            fixed=''.join(p['text'] for p in parts if p['fixed'])
            self.assertIn('SPEI < −0.8',fixed)
            self.assertNotIn('of',fixed)
            self.assertTrue(any('of' in p['text'] for p in parts if not p['fixed']))

    def test_replayed_symbol_keeps_native_ink_and_correct_copy_text(self):
        """自造错误 ToUnicode 字体：轮廓匹配后重放同一字形，复制得到负号。"""
        from io import BytesIO
        import hashlib
        import re
        from fontTools.fontBuilder import FontBuilder
        from fontTools.pens.t2CharStringPen import T2CharStringPen
        from fontTools.pens.recordingPen import RecordingPen
        from paperlocale.source_symbols import restore_source_symbols
        from paperlocale.source_layout import _parts
        from paperlocale.paragraph_layout import bind_inline_glyphs, write_inline
        builder=FontBuilder(1000,isTTF=False)
        builder.setupGlyphOrder(['.notdef','two']);builder.setupCharacterMap({50:'two'})
        strings={}
        for name in ['.notdef','two']:
            pen=T2CharStringPen(500,None)
            if name=='two':
                pen.moveTo((50,200));pen.lineTo((450,200));pen.lineTo((450,250));pen.lineTo((50,250));pen.closePath()
            strings[name]=pen.getCharString()
        builder.setupCFF('WrongUnicode',{'FullName':'WrongUnicode','FamilyName':'WrongUnicode','Weight':'Regular'},strings,{})
        builder.setupHorizontalMetrics({n:(500,0) for n in strings})
        builder.setupHorizontalHeader(ascent=800,descent=-200)
        builder.setupNameTable({'familyName':'WrongUnicode','styleName':'Regular'})
        builder.setupOS2(sTypoAscender=800,sTypoDescender=-200,usWinAscent=800,usWinDescent=200)
        builder.setupPost()
        data=BytesIO();builder.font['CFF '].cff.compile(data,builder.font)
        # 与真实嵌入程序相同，先编译再读回，避免整数/浮点表示影响摘要。
        from fontTools.cffLib import CFFFontSet
        cff=CFFFontSet();cff.decompile(BytesIO(data.getvalue()),None)
        pen=RecordingPen();cff[0].CharStrings['two'].draw(pen)
        signature=hashlib.sha256(repr(pen.value).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'symbol.pdf'
            with fitz.open() as doc:
                page=doc.new_page();ref=page.insert_font(fontname='WrongUnicode',fontbuffer=data.getvalue())
                page.insert_text((100,100),'2',fontname='WrongUnicode',fontsize=12)
                child=int(re.search(r'\d+',doc.xref_get_key(ref,'DescendantFonts')[1]).group())
                descriptor=int(doc.xref_get_key(child,'FontDescriptor')[1].split()[0])
                stream=int(doc.xref_get_key(descriptor,'FontFile3')[1].split()[0])
                doc.xref_set_key(stream,'Subtype','/Type1C')
                doc.update_object(ref,f'<< /Type /Font /Subtype /Type1 /BaseFont /WrongUnicode /FirstChar 50 /LastChar 50 /Widths [500] /Encoding /WinAnsiEncoding /FontDescriptor {descriptor} 0 R >>')
                content=page.get_contents()[0]
                doc.update_stream(content,doc.xref_stream(content).replace(b'<0001>',b'<32>'))
                doc.save(path)
            with fitz.open(path) as doc, patch('paperlocale.source_symbols.REVIEWED_OUTLINES',{signature:'−'}):
                self.assertEqual(doc[0].get_text().strip(),'2')
                raw=doc[0].get_text('rawdict')['blocks'];restore_source_symbols(doc[0],raw,{})
                anchor=_parts(raw[0],1,[],paragraph=True)[0]
                unit={'anchors':[anchor],'slots':[]};bind_inline_glyphs(path,[unit])
                self.assertEqual(anchor['glyphs'][0]['text'],'−')
                before=doc[0].get_pixmap(clip=fitz.Rect(95,85,110,105)).samples
                page=doc.new_page();write_inline(page,{'inline_anchor':anchor,'shift':[0,0]}, {})
                after=page.get_pixmap(clip=fitz.Rect(95,85,110,105)).samples
                self.assertEqual(before,after)
                self.assertEqual(page.get_text().strip(),'−')
