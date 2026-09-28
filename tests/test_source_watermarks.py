"""出版社标记的水印与正文分层，水印像素及源指令必须完整保留。"""
from pathlib import Path
import tempfile
import unittest
import pymupdf as fitz
from paperlocale.source_watermarks import background_layers, set_page_stream, restore_background, verify_backgrounds
from paperlocale.source_layout import extract_layout


def fixture(path, declared=True):
    with fitz.open() as doc:
        page=doc.new_page(width=400,height=400)
        font=page.insert_font(fontname='helv')
        doc.xref_set_key(int(doc.xref_get_key(page.xref,'Resources')[1].split()[0]),'Properties',
                         '<< /WM << /Type /Pagination /Subtype /Watermark >> >>' if declared else '<<>>')
        marker=b'/Artifact /WM BDC' if declared else b'/Artifact BMC'
        data=(b'q '+marker+b' q 0.8 g BT /helv 40 Tf 0.7071 0.7071 -0.7071 0.7071 50 50 Tm '
              b'(Accepted Manuscript) Tj ET Q EMC Q '
              b'BT /helv 12 Tf 1 0 0 1 50 250 Tm (This paragraph measures soil moisture.) Tj ET')
        set_page_stream(page,data);doc.save(path)


class SourceWatermarkTests(unittest.TestCase):
    def test_original_background_pixels_survive_body_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.pdf';fixture(source)
            plan=extract_layout(source,paragraph=True)
            self.assertEqual(plan['watermarks'][0]['text'],'Accepted Manuscript')
            self.assertTrue(any('soil moisture' in b['text'] for b in plan['blocks']))
            self.assertFalse(any('Accepted Manuscript' in b['text'] for b in plan['blocks']))
            with fitz.open(source) as original,fitz.open(source) as written:
                layer=background_layers(original)[1];page=written[0]
                set_page_stream(page,layer['foreground']);page=written.reload_page(page)
                # 真实删除正文后添加另一段文字，不用“未改页面”冒充编辑验证。
                page.add_redact_annot(fitz.Rect(40,130,350,170),fill=False)
                page.apply_redactions(images=0,graphics=0)
                restore_background(page,original[0],layer)
                page=written.reload_page(page);page.insert_text((50,150),'Replacement body.')
                target=Path(folder)/'result.pdf';written.save(target)
                with fitz.open(target) as result:
                    self.assertTrue(verify_backgrounds(original,result)[0]['pixels_equal'])
                    ref=int(result.xref_get_key(result[0].xref,'PLWatermark')[1].split()[0])
                    result.update_stream(ref,layer['background'].replace(b'0.8 g',b'0.5 g'))
                    with self.assertRaisesRegex(ValueError,'水印指令'):
                        verify_backgrounds(original,result)

    def test_unmarked_diagonal_text_is_not_discarded(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.pdf';fixture(source,False)
            plan=extract_layout(source,paragraph=True)
            self.assertEqual(plan['watermarks'],[])
            self.assertTrue(any(b['kind']=='review' and 'Accepted' in b['text'] for b in plan['blocks']))
