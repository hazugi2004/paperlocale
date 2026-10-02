"""Native object numeric recovery must never touch moved or translated text."""
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import pymupdf as fitz
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

from paperlocale.source_watermarks import set_page_stream
from paperlocale.vector_precision import (_SourceFloat, _restore_protected_text,
                                          restore_vector_precision)


class NativePrecisionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.source = b'BT /F1 10 Tf 1 0 0 1 10 20 Tm [(A) -7092.7 (B)] TJ ET'
        self.target = self.source.replace(b'-7092.7 ', b'-7092.7009 ')
        self.fonts = {'/F1': DictionaryObject({NameObject('/BaseFont'): NameObject('/Helvetica'),
                                             NameObject('/Subtype'): NameObject('/Type1')})}
        style = ('Helvetica', 10., (1., 0.), 0, (0., 0., 0.), 1., 0)
        self.proofs = [([((ord(c), n, (x, 20.), (x, 10., x+5, 22.)), style)
                         for n, (c, x) in enumerate([('A', 10.), ('B', 86.)])],
                        (1., 0., 0., 1., 0., 0.))]

        self.proofs = [(glyphs, glyphs, matrix) for glyphs, matrix in self.proofs]

    def repair(self, source=None, target=None, fonts=None, proofs=None):
        return _restore_protected_text(self.source if source is None else source,
            self.target if target is None else target, self.fonts,
            self.fonts if fonts is None else fonts, self.proofs if proofs is None else proofs)

    def test_only_numeric_array_is_recovered_and_idempotent(self):
        fixed, count = self.repair()
        self.assertEqual(count, 1)
        self.assertIn(b'[(A) -7092.7 (B)] TJ', fixed)
        self.assertEqual(self.repair(target=fixed), (fixed, 0))

    def test_body_moved_citation_and_same_text_at_other_position_are_untouched(self):
        self.assertEqual(self.repair(proofs=[]), (self.target, 0))
        moved = self.target.replace(b'10 20 Tm', b'10 30 Tm')
        self.assertEqual(self.repair(target=moved), (moved, 0))
        # Both streams agree at another location, but evidence is from a table
        # elsewhere containing the same string. It must not authorize body edits.
        source = self.source.replace(b'10 20 Tm', b'10 30 Tm')
        self.assertEqual(self.repair(source=source, target=moved), (moved, 0))

    def test_internal_glyph_motion_cannot_borrow_an_unchanged_start(self):
        from copy import deepcopy
        proofs = deepcopy(self.proofs)
        before, after, matrix = proofs[0]
        after = list(after)
        char, style = after[1]
        after[1] = ((char[0], char[1], (char[2][0] + .00003, char[2][1]), char[3]), style)
        self.assertEqual(self.repair(proofs=[(before, after, matrix)]), (self.target, 0))

    def test_font_and_nonnumeric_structure_changes_are_untouched(self):
        other = {'/F1': DictionaryObject({NameObject('/BaseFont'): NameObject('/Times-Roman'),
                                         NameObject('/Subtype'): NameObject('/Type1')})}
        self.assertEqual(self.repair(fonts=other), (self.target, 0))
        changed = {'/F1': DictionaryObject(self.fonts['/F1'])}
        changed['/F1'][NameObject('/FirstChar')] = NumberObject(31)
        self.assertEqual(self.repair(fonts=changed), (self.target, 0))
        for replacement in [b'[(B) -7092.7009 (A)] TJ', b'[(AB) -7092.7009] TJ',
                            b'[(A) -7092.7009 (C)] TJ']:
            target = self.target.replace(b'[(A) -7092.7009 (B)] TJ', replacement)
            self.assertEqual(self.repair(target=target), (target, 0))

    def test_ambiguous_original_and_dependent_showing_are_not_guessed(self):
        collision = self.source + b'\n' + self.source.replace(b'-7092.7 ', b'-7092.7001 ')
        with self.assertRaisesRegex(ValueError, 'Ambiguous original protected text'):
            self.repair(source=collision)
        target = self.target.replace(b' ET', b' (dependent) Tj ET')
        self.assertEqual(self.repair(target=target), (target, 0))

    def test_quote_spacing_and_line_movement_are_part_of_identity(self):
        from paperlocale.vector_precision import _text_arrays
        source = b'BT /F1 10 Tf 0 0 () " 1 0 0 1 10 20 Tm [(A) 0 (B)] TJ ET'
        target = b'BT /F1 10 Tf 0 1 () " 1 0 0 1 10 20 Tm [(A) 100 (B)] TJ ET'
        self.assertEqual(self.repair(source=source, target=target), (target, 0))
        quoted = b"BT /F1 10 Tf 5 TL 1 0 0 1 10 30 Tm () ' 0 0 Td [(A) -7092.7 (B)] TJ ET"
        direct = b'BT /F1 10 Tf 5 TL 1 0 0 1 10 25 Tm [(A) -7092.7 (B)] TJ ET'
        self.assertEqual(list(_text_arrays(quoted))[0][0], list(_text_arrays(direct))[0][0])

    def test_extgstate_font_widths_cannot_borrow_the_previous_tf_identity(self):
        from pypdf.generic import DecodedStreamObject
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with fitz.open() as base:
                page = base.new_page(width=200, height=200)
                page.insert_font(fontname='helv')
                raw = base.tobytes()
            for label, width, adjustment in [('source', 667, 0), ('target', 767, 100)]:
                writer = PdfWriter(BytesIO(raw)); page = writer.pages[0]
                font = DictionaryObject(page['/Resources']['/Font']['/helv'])
                font[NameObject('/FirstChar')] = NumberObject(65)
                font[NameObject('/LastChar')] = NumberObject(66)
                font[NameObject('/Widths')] = ArrayObject([NumberObject(width), NumberObject(667)])
                state = DictionaryObject({NameObject('/Font'): ArrayObject([writer._add_object(font), NumberObject(10)])})
                page['/Resources'][NameObject('/ExtGState')] = DictionaryObject({NameObject('/GS0'): writer._add_object(state)})
                stream = DecodedStreamObject()
                stream.set_data(f'BT /helv 10 Tf /GS0 gs 1 0 0 1 20 100 Tm [(A) {adjustment} (B)] TJ ET'.encode())
                page[NameObject('/Contents')] = writer._add_object(stream)
                with (root/f'{label}.pdf').open('wb') as output: writer.write(output)
            with fitz.open(root/'source.pdf') as a, fitz.open(root/'target.pdf') as b:
                self.assertEqual(a[0].get_texttrace(), b[0].get_texttrace())
            before = (root/'target.pdf').read_bytes()
            result = restore_vector_precision(root/'source.pdf', root/'target.pdf',
                protected_regions=[{'page': 1, 'kind': 'table', 'rect': [0, 0, 200, 200]}])
            self.assertEqual(result['protected_text_arrays_restored'], 0)
            self.assertEqual((root/'target.pdf').read_bytes(), before)

    def test_actual_pdf_full_glyph_proof_and_region_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, value in [('source', '7092.7'), ('target', '7092.7009')]:
                with fitz.open() as doc:
                    page = doc.new_page(); page.insert_font(fontname='helv')
                    other_x = '160' if label == 'source' else '160.00003'
                    set_page_stream(page, (f'BT /helv 1 Tf 6.9738 0 0 6.9738 158.1164 402.12078857421875 Tm [(A) -{value} (B)] TJ ET '
                                          f'BT /helv 7 Tf 1 0 0 1 {other_x} 420 Tm (Other row) Tj ET').encode())
                    doc.save(root/f'{label}.pdf')
            original = (root/'target.pdf').read_bytes()
            region = {'page': 1, 'kind': 'table', 'rect': [150, 410, 220, 450]}
            for kind in ('formula', 'text'):
                evidence = restore_vector_precision(root/'source.pdf', root/'target.pdf',
                    protected_regions=[dict(region, kind=kind)])
                self.assertEqual(evidence['protected_text_arrays_restored'], 0)
                self.assertEqual((root/'target.pdf').read_bytes(), original)
            evidence = restore_vector_precision(root/'source.pdf', root/'target.pdf', protected_regions=[region])
            self.assertEqual(evidence['protected_text_arrays_restored'], 1)
            self.assertIn(b'-7092.7 ', PdfReader(root/'target.pdf').pages[0].get_contents().get_data())
            with fitz.open(root/'source.pdf') as a, fitz.open(root/'target.pdf') as b:
                self.assertEqual(a[0].get_texttrace()[0], b[0].get_texttrace()[0])
                self.assertNotEqual(a[0].get_texttrace()[1]['chars'][0][2], b[0].get_texttrace()[1]['chars'][0][2])

    def test_page_boxes_exact_recovery_keeps_native_qa_and_poppler(self):
        from paperlocale.qa import _canonical_page_geometry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source, target = root/'source.pdf', root/'target.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=595.276, height=841.89)
                page.draw_line((129.5, 108.5), (200.333, 200.777), width=.3)
                page.insert_text((260.12494, 439.769226), ',')
                doc.save(source)
            writer = PdfWriter(source, incremental=True)
            box = ArrayObject([_SourceFloat(v) for v in ('0', '0', '595.276001', '841.890015')])
            writer.pages[0][NameObject('/MediaBox')] = box
            writer.pages[0][NameObject('/CropBox')] = box.clone(writer)
            buf = BytesIO(); writer.write(buf); source.write_bytes(buf.getvalue())
            with fitz.open(source) as doc:
                doc.save(target)
            self.assertNotEqual(PdfReader(source).pages[0].mediabox, PdfReader(target).pages[0].mediabox)
            evidence = restore_vector_precision(source, target)
            self.assertEqual(evidence['page_geometry_restored'], 2)
            a, b = PdfReader(source), PdfReader(target)
            self.assertEqual(a.pages[0].mediabox, b.pages[0].mediabox)
            self.assertEqual(a.pages[0].cropbox, b.pages[0].cropbox)
            row = _canonical_page_geometry(source, target)[0]
            self.assertEqual(row['source_media_box'], row['translated_media_box'])
            self.assertEqual(row['source_crop_box'], row['translated_crop_box'])
            executable = shutil.which('pdftoppm')
            if executable:
                from PIL import Image
                for name, pdf in [('a', source), ('b', target)]:
                    subprocess.run([executable, '-cropbox', '-r', '144', '-singlefile', '-png', str(pdf), str(root/name)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.assertEqual(Image.open(root/'a.png').tobytes(), Image.open(root/'b.png').tobytes())

    def test_rotation_normalization_and_real_page_resize_are_not_undone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source, target = root/'source.pdf', root/'target.pdf'
            for rotated, resized in [(True, False), (False, True)]:
                for name, rotation, width in [('source', 90 if rotated else 0, '595.276001'),
                                              ('target', 0, '596.276' if resized else '595.276')]:
                    writer = PdfWriter(); page = writer.add_blank_page(width=595.276, height=841.89)
                    page[NameObject('/MediaBox')] = ArrayObject([_SourceFloat(v) for v in ('0', '0', width, '841.890015' if name == 'source' else '841.89')])
                    page[NameObject('/Rotate')] = NumberObject(rotation)
                    with (root/f'{name}.pdf').open('wb') as stream: writer.write(stream)
                original = target.read_bytes()
                self.assertEqual(restore_vector_precision(source, target)['page_geometry_restored'], 0)
                self.assertEqual(target.read_bytes(), original)
