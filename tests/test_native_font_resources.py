"""验证源字体资源重放及旋转页坐标，不依赖论文字体或联网。"""
from pathlib import Path
import tempfile
import unittest
import pymupdf as fitz
from paperlocale.font_geometry import _source_anchor_glyphs, open_source_pdf
from paperlocale.paragraph_layout import write_inline, verify_inline
from test_type1_geometry import program


class NativeFontResourceTests(unittest.TestCase):
    def replay(self, embedded):
        with tempfile.TemporaryDirectory() as folder, fitz.open() as doc:
            page = doc.new_page()
            if embedded:
                # insert_font 将自造 CFF 置于 Type0 / CIDFontType0 容器。
                page.insert_font(fontname='Fixture', fontbuffer=program('cff', [.001,0,0,.001,0,0]))
                page.insert_text((100,100), 'P', fontname='Fixture', fontsize=20)
            else:
                ref=doc.get_new_xref()
                doc.update_object(ref, '<< /Type /Font /Subtype /TrueType /BaseFont /TimesNewRomanPSMT '
                                  '/Encoding /WinAnsiEncoding >>')
                doc.xref_set_key(page.xref,'Resources',f'<< /Font << /F {ref} 0 R >> >>')
                stream=doc.get_new_xref();doc.update_object(stream,'<<>>')
                doc.update_stream(stream,b'BT /F 20 Tf 1 0 0 1 100 742 Tm (a,b) Tj ET')
                doc.xref_set_key(page.xref,'Contents',f'{stream} 0 R')
            # 重开后实际渲染，保证证明的是落盘字体而非编辑缓存。
            with fitz.open(stream=doc.tobytes(), filetype='pdf') as source:
                page=source[0]
                before=page.get_pixmap(matrix=fitz.Matrix(4,4)).samples
                chars=[{'text':c['c'],'rect':c['bbox'],'origin':c['origin']}
                       for b in page.get_text('rawdict')['blocks'] for l in b['lines']
                       for s in l['spans'] for c in s['chars']]
                glyphs=_source_anchor_glyphs(page,chars,{})
                self.assertTrue(glyphs)
                self.assertTrue(all('source_code' in g for g in glyphs))
                original_fonts={r[0]:source.xref_object(r[0]) for r in page.get_fonts()}
                for ref in page.get_contents():source.update_stream(ref,b'')
                item={'page':1,'shift':[0,0],'inline_anchor':{'page':1,'text':'fixture','glyphs':glyphs}}
                write_inline(page,item,{})
                target=Path(folder)/'replay.pdf';source.save(target);verify_inline(target,[item])
                with fitz.open(target) as written:
                    self.assertEqual(before,written[0].get_pixmap(matrix=fitz.Matrix(4,4)).samples)
                    self.assertTrue(all(written.xref_object(ref)==value for ref,value in original_fonts.items()))

    def test_nonembedded_resource_uses_same_renderer_font(self):
        self.replay(False)

    def test_cid_cff_reuses_original_encoding(self):
        self.replay(True)

    def test_rotation_keeps_pixels_and_links(self):
        with tempfile.TemporaryDirectory() as folder, fitz.open() as doc:
            page=doc.new_page(width=300,height=400)
            page.insert_text((50,100),'Caption')
            page.insert_link({'kind':fitz.LINK_URI,'from':fitz.Rect(50,80,100,105),'uri':'https://example.org'})
            page.set_rotation(90)
            path=Path(folder)/'rotated.pdf';doc.save(path)
            with fitz.open(path) as original,open_source_pdf(path) as normalized:
                a,b=original[0],normalized[0]
                self.assertEqual(a.get_pixmap().samples,b.get_pixmap().samples)
                self.assertEqual(b.rotation,0)
                self.assertEqual(a.get_links()[0]['from'],b.get_links()[0]['from'])
