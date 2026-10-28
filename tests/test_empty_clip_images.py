"""只接纳被原生证明为空的裁切调用差，真实图片/几何/附属资源差仍拒绝。"""

from hashlib import sha256
import copy
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz
from pypdf import PdfReader

from paperlocale.qa import (_empty_clip_image_evidence, _image_counts,
                            inspect_pdf_pair)
from paperlocale.source_layout import verify_unchanged


def _build_image_page(path, *, hidden=True, visible=True, x=20, color=(200, 30, 40),
                      mask=None, clip=None, inline=None, opacity=None, clip_path=None):
    """用自有2×2像素与明确内容流构造状态，隐藏调用使用严格零面积 clip。"""
    with fitz.open() as document:
        page = document.new_page(width=200, height=200)
        page.insert_text((15, 105), 'Native image integrity check', fontsize=10)
        text = page.read_contents()
        ppm = b'P6\n2 2\n255\n' + bytes(color) * 4
        image_args = {'stream': ppm}
        if mask is not None:
            image_args['mask'] = b'P5\n2 2\n255\n' + bytes([mask]) * 4
        page.insert_image(fitz.Rect(20, 20, 60, 60), **image_args)
        name = page.get_images(full=True)[0][7].encode('ascii')
        if opacity is not None:
            resources = int(document.xref_get_key(page.xref, 'Resources')[1].split()[0])
            document.xref_set_key(resources, 'ExtGState',
                f'<< /FixtureAlpha << /ca {opacity} /CA {opacity} >> >>')
        content = bytearray(text)
        if visible:
            content += b'q\n'
            if opacity is not None:
                content += b'/FixtureAlpha gs\n'
            if clip is not None:
                content += ('%s %s %s %s re W n\n' % tuple(clip)).encode()
            if clip_path is not None:
                content += clip_path.encode() + b' W n\n'
            content += f'40 0 0 40 {x} 140 cm /'.encode() + name + b' Do Q\n'
        if hidden:
            content += b'q 0 0 0 0 re W n 40 0 0 40 80 140 cm /' + name + b' Do Q\n'
        if inline is not None:
            content += b'q\n'
            if inline == 'empty':
                content += b'0 0 0 0 re W n\n'
            content += b'BI /W 1 /H 1 /BPC 8 /CS /RGB ID \xff\x00\x00 EI Q\n'
        xref = document.get_new_xref()
        document.update_object(xref, '<<>>')
        document.update_stream(xref, bytes(content))
        page.set_contents(xref)
        document.save(path)


def _evidence(source, target):
    a, b = PdfReader(source), PdfReader(target)
    return _empty_clip_image_evidence(source, target, 0, a.pages[0], b.pages[0],
                                     _image_counts(source)[0], _image_counts(target)[0])


def _joint_report(source, target):
    # 使用真正的逐像素检查产生成功页记录。其他元数据对应保留流程在
    # 全部保护区检查成功后写出的报告结构；这不是由图片回调虚构视觉相等。
    report = verify_unchanged(source, target, [])
    report.update(source_sha256=sha256(source.read_bytes()).hexdigest(),
                  translated_sha256=sha256(target.read_bytes()).hexdigest(),
                  protected_region_count=_image_counts(source)[0], fixed_ink_checks=[])
    return report


def _build_overlapping_images(path, *, reverse=False, hidden=False):
    """两张自有不透明图片同框叠加；换序会改变可见颜色而不改变调用集合。"""
    with fitz.open() as document:
        page = document.new_page(width=200, height=200)
        page.insert_text((15, 105), 'Native image integrity check', fontsize=10)
        text = page.read_contents()
        for color in ((220, 20, 40), (20, 40, 220)):
            page.insert_image(fitz.Rect(20, 20, 60, 60),
                stream=b'P6\n2 2\n255\n' + bytes(color) * 4)
        names = [item[7] for item in page.get_images(full=True)]
        ordered = list(reversed(names)) if reverse else names
        content = text + b''.join(
            f'q 40 0 0 40 20 140 cm /{name} Do Q\n'.encode() for name in ordered)
        if hidden:
            content += (f'q 0 0 0 0 re W n 40 0 0 40 80 140 cm '
                        f'/{names[0]} Do Q\n').encode()
        xref = document.get_new_xref()
        document.update_object(xref, '<<>>')
        document.update_stream(xref, content)
        page.set_contents(xref)
        document.save(path)


class EmptyClipImagesTests(unittest.TestCase):
    def test_zero_area_clip_removal_has_strict_evidence_and_pair_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root/'source.pdf', root/'target.pdf'
            _build_image_page(source)
            _build_image_page(target, hidden=False)
            source_sha = sha256(source.read_bytes()).hexdigest()
            self.assertEqual((_image_counts(source), _image_counts(target)), ([2], [1]))
            evidence = _evidence(source, target)
            self.assertEqual(evidence['source_nonempty_calls'], 1)
            self.assertEqual(evidence['translated_nonempty_calls'], 1)
            self.assertEqual(evidence['source_empty_clip_calls'], 1)
            self.assertEqual(evidence['translated_empty_clip_calls'], 0)
            self.assertTrue(evidence['used_image_resources_equal'])
            self.assertTrue(evidence['nonempty_call_order_equal'])
            # 真正走 inspect_pdf_pair 的图片差分支与渲染输出；不 mock 完整性门禁。
            report = inspect_pdf_pair(source_pdf=source, translated_pdf=target,
                                      output_dir=root/'qa', dpi=72,
                                      preservation_report=_joint_report(source, target))
            self.assertEqual(report['errors'], [])
            self.assertEqual(report['warnings'], [])
            self.assertEqual(report['pages'][0]['image_preservation_evidence'], evidence | {
                'bound_preservation_pixels_equal': True, 'preservation_dpi': 144})
            self.assertEqual(sha256(source.read_bytes()).hexdigest(), source_sha)
            self.assertFalse(report['visual_accepted'])

    def test_overlapping_visible_images_cannot_reorder_when_empty_call_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root/'source.pdf', root/'target.pdf'
            _build_overlapping_images(source, hidden=True)
            _build_overlapping_images(target, reverse=True)
            self.assertEqual((_image_counts(source), _image_counts(target)), ([3], [2]))
            # 以实际渲染证明反例改变了图像，不能让Counter相等替代调用顺序。
            with fitz.open(source) as a, fitz.open(target) as b:
                self.assertNotEqual(a[0].get_pixmap().pixel(40, 40),
                                    b[0].get_pixmap().pixel(40, 40))
            self.assertIsNone(_evidence(source, target))
            report = inspect_pdf_pair(source_pdf=source, translated_pdf=target,
                                      output_dir=root/'qa', dpi=72)
            self.assertTrue(any('图片对象减少' in error for error in report['errors']))
            self.assertNotIn('image_preservation_evidence', report['pages'][0])
            # 同顺序仅删除真正空clip调用仍合法，不能把修复变成一律拒绝。
            _build_overlapping_images(target)
            self.assertTrue(_evidence(source, target)['nonempty_call_order_equal'])

    def test_missing_stale_incomplete_or_failed_preservation_report_cannot_allow_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root/'source.pdf', root/'target.pdf'
            _build_image_page(source)
            _build_image_page(target, hidden=False)
            good = _joint_report(source, target)
            bad_reports = [None]
            for change in ({'source_sha256': 'stale'}, {'translated_sha256': 'stale'},
                           {'pages': []}, {'pages': [{'page': 1, 'outside_pixels_equal': False}]},
                           {'protected_region_count': -1}, {'fixed_ink_checks': None},
                           {'fixed_ink_checks': [{'page': 1, 'fixed_ink_pixels': -1,
                                                 'body_overlap_pixels': 0}]}):
                bad_reports.append(good | change)
            for index, preservation in enumerate(bad_reports):
                with self.subTest(case=index):
                    report = inspect_pdf_pair(source_pdf=source, translated_pdf=target,
                        output_dir=root/str(index), dpi=72, preservation_report=preservation)
                    self.assertTrue(any('图片对象减少' in error for error in report['errors']))
                    self.assertNotIn('image_preservation_evidence', report['pages'][0])
            # overlap记录的是已核验工作量，可以非零，不能误当失败数量。
            overlap = copy.deepcopy(good)
            overlap['fixed_ink_checks'] = [{'page': 1, 'fixed_ink_pixels': 5,
                                           'body_overlap_pixels': 2}]
            report = inspect_pdf_pair(source_pdf=source, translated_pdf=target,
                output_dir=root/'overlap', dpi=72, preservation_report=overlap)
            self.assertEqual(report['errors'], [])

    def test_same_call_signature_cannot_replace_opacity_or_full_clip_pixel_checks(self):
        changes = ({'opacity': .1},
                   {'clip_path': '20 160 m 40 180 l 60 160 l 40 140 l h'})
        for changed in changes:
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, target = root/'source.pdf', root/'target.pdf'
                _build_image_page(source, opacity=1, clip=(20, 140, 40, 40))
                _build_image_page(target, hidden=False,
                    **({'opacity': 1, 'clip': (20, 140, 40, 40)} | changed))
                # 原生回调只给clip包围框，局部证据会相等；共同像素门禁
                # 必须拒绝真实透明度或裁切路径变化，不能单凭局部证据放行。
                self.assertIsNotNone(_evidence(source, target))
                with self.assertRaisesRegex(ValueError, '像素改变'):
                    verify_unchanged(source, target, [])
                report = inspect_pdf_pair(source_pdf=source, translated_pdf=target,
                                          output_dir=root/'qa', dpi=72)
                self.assertTrue(any('图片对象减少' in error for error in report['errors']))
                self.assertNotIn('image_preservation_evidence', report['pages'][0])

    def test_deleted_nonempty_image_still_fails_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root/'source.pdf', root/'target.pdf'
            _build_image_page(source)
            _build_image_page(target, hidden=False, visible=False)
            self.assertIsNone(_evidence(source, target))
            report = inspect_pdf_pair(source_pdf=source, translated_pdf=target,
                                      output_dir=root/'qa', dpi=72)
            self.assertTrue(any('图片对象减少' in error for error in report['errors']))
            self.assertNotIn('image_preservation_evidence', report['pages'][0])

    def test_equal_nonempty_counts_do_not_hide_position_clip_image_or_mask_change(self):
        for changed in ({'x': 21}, {'clip': (10, 130, 70, 60)},
                        {'color': (30, 200, 40)}, {'mask': 60}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, target = root/'source.pdf', root/'target.pdf'
                # 两侧各有一次非空调用、同资源名，不能靠次数一致放过内容变化。
                _build_image_page(source, mask=180)
                _build_image_page(target, hidden=False, **({'mask': 180} | changed))
                self.assertEqual((_image_counts(source), _image_counts(target)), ([2], [1]))
                self.assertIsNone(_evidence(source, target))

    def test_form_and_even_empty_clipped_inline_images_are_outside_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'target.pdf'
            _build_image_page(target, hidden=False)
            with fitz.open(target) as form, fitz.open() as document:
                page = document.new_page(width=200, height=200)
                page.show_pdf_page(page.rect, form, 0)
                document.save(root/'form.pdf')
            with self.assertRaisesRegex(ValueError, 'Form'):
                _evidence(root/'form.pdf', target)
            for mode in ('visible', 'empty'):
                with self.subTest(inline=mode):
                    source = root/(mode+'.pdf')
                    _build_image_page(source, inline=mode)
                    with self.assertRaisesRegex(ValueError, '内联'):
                        _evidence(source, target)
                    # 双向拒绝，避免仅检查 source 而漏掉目标新增的内联结构。
                    with self.assertRaisesRegex(ValueError, '内联'):
                        _evidence(target, source)


if __name__ == '__main__':
    unittest.main()
