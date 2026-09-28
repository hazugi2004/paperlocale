"""自造字体覆盖空轮廓、未知乱码、fi 单字形以及重复空白的证据边界。"""
from io import BytesIO
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
import pymupdf as fitz
from fontTools.cffLib import CFFFontSet
from test_type1_geometry import program
from paperlocale.source_layout import normalize_traced_spaces
from paperlocale.source_symbols import restore_verified_symbols
from paperlocale.safe_text import safe_erase_rectangles, prepare_source_layer


def fixture(path, *, empty=False, ligature=False, zero_ink=False):
    data=program('cff',[.001,0,0,.001,0,0])
    cff=CFFFontSet();cff.decompile(BytesIO(data),None);top=cff[0]
    name='fi' if ligature else 'P'
    if ligature:
        top.CharStrings.charStrings['fi']=top.CharStrings.charStrings.pop('P');top.charset[1]='fi'
    if empty:
        glyph=top.CharStrings[name];glyph.program=[500,'endchar'];glyph.bytecode=None
    if zero_ink:
        from fontTools.pens.t2CharStringPen import T2CharStringPen
        pen=T2CharStringPen(0,None)
        pen.moveTo((-250,600));pen.lineTo((-50,600));pen.lineTo((-50,650));pen.lineTo((-250,650));pen.closePath()
        top.CharStrings[name]=pen.getCharString(private=top.Private)
    buffer=BytesIO();cff.compile(buffer,SimpleNamespace(recalcBBoxes=False))
    with fitz.open() as doc:
        page=doc.new_page()
        def obj(value,stream=None):
            ref=doc.get_new_xref();doc.update_object(ref,value)
            if stream is not None:doc.update_stream(ref,stream)
            return ref
        fontfile=obj('<< /Subtype /Type1C >>',buffer.getvalue())
        descriptor=obj(f'<< /Type /FontDescriptor /FontName /FixtureCFF /Flags 4 /FontBBox [0 0 500 700] '
                       f'/Ascent 700 /Descent 0 /CapHeight 700 /StemV 80 /ItalicAngle 0 /FontFile3 {fontfile} 0 R >>')
        unicode='FB01' if ligature else 'E001'
        cmap=obj('<<>>',f'begincmap /CMapType 2 def 1 begincodespacerange <00> <ff> endcodespacerange 1 beginbfchar <50> <{unicode}> endbfchar endcmap'.encode())
        ref=obj(f'<< /Type /Font /Subtype /Type1 /BaseFont /FixtureCFF /FontDescriptor {descriptor} 0 R '
                f'/FirstChar 80 /LastChar 80 /Widths [500] /Encoding << /Differences [80 /{name}] >> /ToUnicode {cmap} 0 R >>')
        doc.xref_set_key(page.xref,'Resources',f'<< /Font << /F {ref} 0 R >> >>')
        commands=b'BT /F 20 Tf 1 0 0 1 100 742 Tm <50> Tj ET'
        if ligature:
            second_cmap=obj('<<>>',b'begincmap /CMapType 2 def 1 begincodespacerange <00> <ff> endcodespacerange 1 beginbfchar <50> <00660069> endbfchar endcmap')
            second=obj(doc.xref_object(ref));doc.xref_set_key(second,'ToUnicode',f'{second_cmap} 0 R')
            doc.xref_set_key(page.xref,'Resources/Font/G',f'{second} 0 R')
            commands=b'BT /F 20 Tf 1 0 0 1 50 742 Tm <50> Tj ET BT /G 20 Tf 1 0 0 1 100 742 Tm <50> Tj ET'
        content=obj('<<>>',commands)
        doc.xref_set_key(page.xref,'Contents',f'{content} 0 R');doc.save(path)


class CorpusEncodingTests(unittest.TestCase):
    def test_empty_outline_is_space_but_unknown_ink_is_not_guessed(self):
        for empty in [False,True]:
            with self.subTest(empty=empty),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'source.pdf';fixture(path,empty=empty)
                with fitz.open(path) as doc:
                    raw=doc[0].get_text('rawdict')['blocks'];restore_verified_symbols(doc[0],raw,{})
                    char=raw[0]['lines'][0]['spans'][0]['chars'][0]
                    self.assertEqual(char['c'],' ' if empty else '\ue001')
                    if empty:self.assertEqual(char['source_blank'],'\ue001')

    def test_trace_ligature_deletion_requires_its_leading_character(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source.pdf';fixture(path,ligature=True)
            with fitz.open(path) as doc:
                page=doc[0]
                chars=[{'text':c['c'],'rect':c['bbox'],'origin':c['origin']}
                       for b in page.get_text('rawdict')['blocks'] for l in b['lines']
                       for s in l['spans'] for c in s['chars'] if c['origin'][0]>=100]
                self.assertEqual(''.join(c['text'] for c in chars),'fi')
                self.assertEqual(page.get_texttrace()[0]['chars'][0][0],0xFB01)
            part={'page':1,'rect':[100,80,110,105],'text':'fi','chars':chars}
            regions=safe_erase_rectangles([part],[],path)
            result=prepare_source_layer(path,[part],[],regions)
            with fitz.open(stream=result,filetype='pdf') as erased:
                self.assertEqual(erased[0].get_text().strip(),'ﬁ')
            with self.assertRaisesRegex(ValueError,'没有安全删除范围'):
                safe_erase_rectangles([{**part,'chars':chars[1:]}],[],path)

    def test_repeated_spaces_keep_existing_coordinate_tolerance(self):
        def raw():return [{'lines':[{'spans':[{'font':'Fixture','chars':[
            {'c':'\u200b','origin':(10,20),'bbox':(10,10,12,22)}]}]}]}]
        traces=[{'font':'Fixture','dir':(1,0),'chars':[(32,1,(10,20),(10,11,12,21)),
                     (32,1,(10.0001,20),(10.0001,11,12.0001,21))]}]
        value=raw();normalize_traced_spaces(value,traces,repeated_spaces=True)
        self.assertEqual(value[0]['lines'][0]['spans'][0]['chars'][0]['c'],' ')
        old=raw();normalize_traced_spaces(old,traces)
        self.assertEqual(old[0]['lines'][0]['spans'][0]['chars'][0]['c'],'\u200b')

    def test_zero_advance_ink_is_actually_erased(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source.pdf';fixture(path,zero_ink=True)
            with fitz.open(path) as doc:
                ref=doc[0].get_fonts()[0][0]
                cmap=int(doc.xref_get_key(ref,'ToUnicode')[1].split()[0])
                doc.update_stream(cmap,doc.xref_stream(cmap).replace(b'E001',b'0303'))
                doc.xref_set_key(ref,'Widths','[0]')
                doc[0].insert_text((90,100),'n',fontsize=20,overlay=False)
                data=doc.tobytes()
            path.write_bytes(data)
            with fitz.open(path) as doc:
                chars=[c for b in doc[0].get_text('rawdict')['blocks'] for l in b['lines'] for sp in l['spans'] for c in sp['chars']]
                self.assertTrue(any(v[0]==0x303 for t in doc[0].get_texttrace() for v in t['chars']))
                # 不同 MuPDF 版本可省略孤立零宽 rawdict 字符；输入计划
                # 显式沿用本 fixture 的零推进字框，像素验收不能省略。
                chars.append({'c':'\u0303','bbox':(100,86,100,104),'origin':(100,100)})
                part={'page':1,'text':'n\u0303','rect':[90,75,112,108],
                      'chars':[{'text':v['c'],'rect':v['bbox'],'origin':v['origin']} for v in chars]}
            regions=safe_erase_rectangles([part],[],path)
            result=prepare_source_layer(path,[part],[],regions)
            with fitz.open(stream=result,filetype='pdf') as doc:
                self.assertFalse(doc[0].get_text().strip())
                self.assertTrue(all(v==255 for v in doc[0].get_pixmap().samples))
