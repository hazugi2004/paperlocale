"""Real PDF rewrite regressions: precision, native resources and parser boundaries."""
from pathlib import Path
from io import BytesIO
import shutil
import subprocess
import tempfile
import unittest
import pymupdf as fitz
from pypdf import PdfReader
from pypdf.generic import DictionaryObject, FloatObject, NameObject
from paperlocale.source_watermarks import set_page_stream
from paperlocale.vector_precision import (restore_rectangles, restore_vector_precision,
                                         _same_resource, _SourceFloat)


class VectorPrecisionTests(unittest.TestCase):
    def test_rectangles_keep_ctm_clip_paint_and_comments(self):
        for paint in [b'f', b'f*', b'S', b'B', b'B*', b'W n', b'W* n']:
            with self.subTest(paint=paint):
                original = b'q .75 .25 -.25 .75 10.12345 20.54321 cm % source\n374.584 337.497 .31287 .40698 re '+paint+b' Q'
                changed = b'q .75 .25 -.25 .75 10.12345 20.54321 cm 374.584 337.497 .3128662 .40698243 re '+paint+b' Q'
                fixed, count = restore_rectangles(original, changed)
                self.assertEqual(count, 1)
                self.assertIn(b'.31287 .40698 re', fixed)
                self.assertTrue(fixed.endswith(b' '+paint+b' Q'))
                self.assertEqual(restore_rectangles(original, fixed), (fixed, 0))

    def test_strings_comments_and_inline_image_bytes_are_not_operators(self):
        fake = b'374.584 337.497 .3128662 .40698243 re'
        source = b'374.584 337.497 .31287 .40698 re f'
        prefix = b'% '+fake+b'\nBT ('+fake+b') Tj ET <'+fake.hex().encode()+b'> /Tag BDC EMC '
        # A raw RGB inline image containing PDF-looking bytes is parsed as image data.
        pixels = fake.ljust(60, b'x')
        image = b'BI /W 20 /H 1 /BPC 8 /CS /RGB ID '+pixels+b' EI\n'
        target = prefix+image+fake+b' f'
        fixed, count = restore_rectangles(source, target)
        self.assertEqual(count, 1)
        self.assertTrue(fixed.startswith(prefix+image))
        self.assertIn(pixels, fixed)

    def test_ctm_distinguishes_rounding_collisions_and_ambiguity_fails(self):
        one = b'374.584 337.497 .31287 .40698 re'
        two = b'374.584 337.497 .3128701 .40698 re'
        rounded = b'374.584 337.497 .3128662 .40698243 re'
        source = b'q 1 0 0 1 10 0 cm '+one+b' f Q q 1 0 0 1 20 0 cm '+two+b' S Q'
        target = b'q 1 0 0 1 10 0 cm '+rounded+b' f Q q 1 0 0 1 20 0 cm '+rounded+b' S Q'
        result, count = restore_rectangles(source, target)
        self.assertEqual(count, 2)
        self.assertIn(one, result);self.assertIn(two, result)
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            restore_rectangles(one+b' f '+two+b' f', rounded+b' f')

    def test_unchanged_precision_and_unrelated_rectangle_stay_unchanged(self):
        for data in [b'-12.123456789 0.000000123 2.125 -3.5 re B', b'0 0 20 20 re W n']:
            self.assertEqual(restore_rectangles(data, data), (data, 0))
        self.assertEqual(restore_rectangles(b'1 2 3 4 re f',b'8 9 10 11 re S'),(b'8 9 10 11 re S',0))

    def test_resource_comparison_rejects_semantic_changes(self):
        a=DictionaryObject({NameObject('/Coords'):FloatObject('501.881958')})
        b=DictionaryObject({NameObject('/Coords'):FloatObject('501.88197')})
        self.assertTrue(_same_resource(a,b))
        b[NameObject('/Coords')]=FloatObject('502')
        self.assertFalse(_same_resource(a,b))
        for value in ['.0000000000123456789','501.881958','-.123456789012345']:
            output=BytesIO();_SourceFloat(value).write_to_stream(output)
            self.assertNotIn(b'e',output.getvalue())
            self.assertEqual(float(output.getvalue()),float(value))

    def test_actual_redaction_preserves_shading_and_rectangles(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.pdf';target=root/'target.pdf'
            with fitz.open() as doc:
                p=doc.new_page(width=600,height=500);p.insert_font(fontname='helv')
                fn=doc.get_new_xref();doc.update_object(fn,'<< /FunctionType 2 /Domain [0 1] /C0 [1 .968627451 .701960784] /C1 [.984313725 .968627451 .788235294] /N 1 >>')
                shade=doc.get_new_xref();doc.update_object(shade,f'<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [355.123456 100.345678 443.234567 200.456789] /Function {fn} 0 R >>')
                owner=int(doc.xref_get_key(p.xref,'Resources')[1].split()[0]);doc.xref_set_key(owner,'Shading',f'<< /Original {shade} 0 R >>')
                set_page_stream(p,b'q 350 100 100 120 re W n /Original sh Q q .3 .2 -.2 .3 50 70 cm 374.584 337.497 .31287 .40698 re B Q BT /helv 12 Tf 1 0 0 1 30 450 Tm (Erase this text) Tj ET')
                doc.save(source)
            # Source created by MuPDF has rounded dictionary numbers already;
            # append a high-precision source shading dictionary independently.
            from pypdf import PdfWriter
            from pypdf.generic import ArrayObject
            w=PdfWriter(source,incremental=True)
            sh=w.pages[0]['/Resources']['/Shading']['/Original']
            sh[NameObject('/Coords')]=ArrayObject([w._add_object(_SourceFloat(x)) for x in ['355.123456','100.345678','443.234567','200.456789']])
            buff=BytesIO();w.write(buff);source.write_bytes(buff.getvalue())
            with fitz.open(source) as doc:
                p=doc[0];p.add_redact_annot(fitz.Rect(20,30,150,60),fill=False);p.apply_redactions(images=0,graphics=0);p.insert_text((30,50),'Replacement');doc.save(target,garbage=0)
            evidence=restore_vector_precision(source,target)
            self.assertEqual(evidence['rectangles_restored'],1)
            self.assertEqual(evidence['shading_resource_sets_restored'],1)
            a=PdfReader(source);b=PdfReader(target)
            self.assertEqual([float(x.get_object()) for x in a.pages[0]['/Resources']['/Shading']['/Original']['/Coords']], [float(x.get_object()) for x in b.pages[0]['/Resources']['/Shading']['/Original']['/Coords']])
            with fitz.open(target) as doc:
                self.assertIn('Replacement',doc[0].get_text());self.assertNotIn('Erase',doc[0].get_text())
            # Real independent renderer when installed; the structural assertions
            # above always run, including on platforms without Poppler.
            executable=shutil.which('pdftoppm')
            if executable:
                from PIL import Image
                for name,path in [('a',source),('b',target)]:
                    subprocess.run([executable,'-r','144','-png','-singlefile',str(path),str(root/name)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                box=(0,200,1200,1000)
                self.assertEqual(Image.open(root/'a.png').crop(box).tobytes(),Image.open(root/'b.png').crop(box).tobytes())

    def test_nested_form_geometry_and_shading_remain_native(self):
        from pypdf import PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, NumberObject
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.pdf';target=root/'target.pdf'
            w=PdfWriter();page=w.add_blank_page(width=600,height=500)
            function=DictionaryObject({NameObject('/FunctionType'):NumberObject(2),NameObject('/Domain'):ArrayObject([NumberObject(0),NumberObject(1)]),NameObject('/C0'):ArrayObject([NumberObject(0)]),NameObject('/C1'):ArrayObject([NumberObject(1)]),NameObject('/N'):NumberObject(1)})
            shade=DictionaryObject({NameObject('/ShadingType'):NumberObject(2),NameObject('/ColorSpace'):NameObject('/DeviceGray'),NameObject('/Coords'):ArrayObject([_SourceFloat(v) for v in ['.123456789','0','100.987654321','0']]),NameObject('/Function'):function})
            form=DecodedStreamObject();form.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Form'),NameObject('/BBox'):ArrayObject([NumberObject(0),NumberObject(0),NumberObject(600),NumberObject(500)]),NameObject('/Matrix'):ArrayObject([_SourceFloat(v) for v in ['.987654321','0','0','.987654321','10.123456789','20.123456789']]),NameObject('/Resources'):DictionaryObject({NameObject('/Shading'):DictionaryObject({NameObject('/Gradient'):shade})})})
            form.set_data(b'q 0 0 200 100 re W n /Gradient sh Q 374.584 337.497 .31287 .40698 re B')
            ref=w._add_object(form);page[NameObject('/Resources')]=DictionaryObject({NameObject('/XObject'):DictionaryObject({NameObject('/F'):ref})})
            content=DecodedStreamObject();content.set_data(b'q /F Do Q q 1 0 0 1 200 0 cm /F Do Q');page[NameObject('/Contents')]=w._add_object(content)
            with source.open('wb') as stream:w.write(stream)
            with fitz.open(source) as d:
                # Clean the Form as redaction does, and serialize resource floats.
                p=d[0];p.add_redact_annot(fitz.Rect(0,450,10,460),fill=False);p.apply_redactions(images=0,graphics=0);d.save(target,garbage=0)
            evidence=restore_vector_precision(source,target)
            a=PdfReader(source).pages[0]['/Resources']['/XObject']['/F'];forms=PdfReader(target).pages[0]['/Resources']['/XObject'];b=next(iter(forms.values())).get_object()
            self.assertEqual(a['/Matrix'],b['/Matrix'])
            self.assertEqual(a['/Resources']['/Shading']['/Gradient']['/Coords'],b['/Resources']['/Shading']['/Gradient']['/Coords'])
            self.assertEqual(evidence['form_geometry_restored'],2)
            self.assertIn(b'.31287 .40698 re',b.get_data())
            self.assertEqual(len(forms),2)
            for other in forms.values():
                self.assertEqual(a['/Matrix'],other.get_object()['/Matrix'])

    def test_precision_failure_cannot_publish_or_mark_success(self):
        from unittest.mock import patch
        from test_source_layout import make_fixture, Provider
        from paperlocale.workflow import initialize_run,load_manifest
        from paperlocale.preserved_workflow import run_preserved
        from paperlocale.domains import load_domain_pack
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=make_fixture(root);run=root/'run';font=root/'cjk.ttf';font.write_bytes(fitz.Font('china-s').buffer)
            initialize_run(source_pdf=source,run_dir=run,source_language='en',target_language='zh-CN')
            # This generated layout fixture tests publication failure, not ONNX.
            with patch('paperlocale.layout_detection.detect_regions', return_value=[]), \
                 patch('paperlocale.vector_precision.restore_vector_precision',side_effect=ValueError('Ambiguous original rectangle precision')):
                with self.assertRaisesRegex(ValueError,'Ambiguous'):
                    run_preserved(run,provider=Provider(),domain=load_domain_pack('atmospheric-science'),plan_path=None,font_file=font,min_font_size=7,dpi=72)
            self.assertNotIn(load_manifest(run)['status'],{'rendered','qa_generated','accepted'})
            self.assertFalse((run/'render_output/translated.pdf').exists())
            self.assertFalse((run/'.preserved.tmp.pdf').exists())
            self.assertFalse((run/'preservation_report.json').exists())

    def _rewrite_forms(self, root, specs, content):
        from pypdf import PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, NumberObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=600, height=500)
        forms = DictionaryObject()
        for name, width, translation in specs:
            form = DecodedStreamObject()
            form.update({NameObject('/Type'): NameObject('/XObject'),
                         NameObject('/Subtype'): NameObject('/Form'),
                         NameObject('/BBox'): ArrayObject([NumberObject(v) for v in (0, 0, 600, 500)]),
                         NameObject('/Resources'): DictionaryObject()})
            if translation is not None:
                form[NameObject('/Matrix')] = ArrayObject([_SourceFloat(v) for v in ('1', '0', '0', '1', translation, '0')])
            form.set_data(width if isinstance(width, bytes)
                          else f'374.584 337.497 {width} .40698 re B'.encode())
            forms[NameObject('/'+name)] = writer._add_object(form)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): forms})
        stream = DecodedStreamObject(); stream.set_data(content)
        page[NameObject('/Contents')] = writer._add_object(stream)
        source, target = root/'source.pdf', root/'target.pdf'
        with source.open('wb') as output:
            writer.write(output)
        with fitz.open(source) as doc:
            page = doc[0]
            page.add_redact_annot(fitz.Rect(0, 450, 10, 460), fill=False)
            page.apply_redactions(images=0, graphics=0)
            doc.save(target, garbage=0)
        evidence = restore_vector_precision(source, target)
        return PdfReader(source), PdfReader(target), evidence

    def test_renamed_form_cannot_steal_an_existing_source_name(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b, evidence = self._rewrite_forms(Path(directory),
                [('A', '.31287', '10.123456789'), ('Fm1', '.3128701', '10.1234569')],
                b'q /A Do Q q 1 0 0 1 0 -100 cm /Fm1 Do Q')
            original = a.pages[0]['/Resources']['/XObject']
            result = b.pages[0]['/Resources']['/XObject']
            self.assertEqual(result['/Fm1']['/Matrix'], original['/A']['/Matrix'])
            self.assertEqual(result['/Fm2']['/Matrix'], original['/Fm1']['/Matrix'])
            self.assertIn(b'.31287 .40698 re', result['/Fm1'].get_data())
            self.assertIn(b'.3128701 .40698 re', result['/Fm2'].get_data())
            self.assertEqual(evidence['rectangles_restored'], 2)

    def test_real_mupdf_composed_matrix_matches_each_float32_step(self):
        with tempfile.TemporaryDirectory() as directory:
            _, b, evidence = self._rewrite_forms(Path(directory), [('F', '.31287', None)],
                b'q .987654321 .123456789 -.234567891 .876543219 10.123456789 20.345678912 cm '
                b'.789123456 .345678912 -.234567891 .891234567 20.123456789 10.345678912 cm /F Do Q')
            form = next(iter(b.pages[0]['/Resources']['/XObject'].values())).get_object()
            self.assertIn(b'.31287 .40698 re', form.get_data())
            self.assertEqual(evidence['rectangles_restored'], 1)

    def test_same_ctm_empty_form_and_page_rule_need_no_identity_or_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, result, evidence = self._rewrite_forms(root,
                [('Fm0', b'', None), ('Fm1', b'0 0 0 1 K .5 w 4 M 0 j 0 J '
                 b'q 1 0 0 1 45.354 41.3812 cm 0 0 m 504.567 0 l S Q', None)],
                b'q /Fm0 Do Q q /Fm1 Do Q')
            forms = result.pages[0]['/Resources']['/XObject']
            self.assertEqual(forms['/Fm1'].get_data(), b'q Q')
            self.assertIn(b'504.567 0 l S', forms['/Fm2'].get_data())
            self.assertEqual(evidence['ambiguous_forms_unchanged'], 2)
            self.assertEqual(evidence['rectangles_restored'], 0)
            before = (root/'target.pdf').read_bytes()
            self.assertEqual(restore_vector_precision(root/'source.pdf', root/'target.pdf'), evidence)
            self.assertEqual((root/'target.pdf').read_bytes(), before)

    def test_same_ctm_ambiguity_still_rejects_any_candidate_needing_precision(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'Ambiguous original Form calls'):
                self._rewrite_forms(Path(directory),
                    [('Fm0', b'', None), ('Fm1', '.31287', None)],
                    b'q /Fm0 Do Q q /Fm1 Do Q')
