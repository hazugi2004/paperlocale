"""真实高密度图片输入与原生删除差分，不复制论文或上游字体。"""
from io import BytesIO
import unittest

from PIL import Image
import pymupdf as fitz

from paperlocale.text_filter import (apply_text_redactions, _filter_dense_page,
                                    _text_intersects_redactions)


def dense_document(count=65536):
    """一个原图对象在同一页真实绘制 count 次；无 mock 替代阈值证据。"""
    document = fitz.open()
    page = document.new_page(width=240, height=180)
    image = BytesIO()
    # 半透明支持集也必须保持，不能为了越过命名上限先把图像重编码。
    Image.new('RGBA', (2, 2), (20, 90, 180, 130)).save(image, format='PNG')
    page.insert_image((20, 20, 40, 40), stream=image.getvalue())
    drawing = document.xref_stream(page.get_contents()[0])
    document.update_stream(page.get_contents()[0], drawing * count)
    page.draw_rect((150, 20, 210, 40), color=(1, 0, 0), width=1)
    page.insert_text((50, 100), 'ABCD', fontsize=12)
    return document


def redact_b(page):
    page.add_redact_annot(page.search_for('B')[0], fill=False, cross_out=False)


def crop_pixels(page, rectangle):
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rectangle, alpha=False)
    return pix.width, pix.height, pix.samples


class TextFilterTests(unittest.TestCase):
    def test_65536_real_image_instances_preserve_resources_pixels_links_and_adjacent_text(self):
        with dense_document() as document:
            page = document[0]
            self.assertEqual(sum(kind == 'fill-image' for kind, _ in page.get_bboxlog()), 65536)
            images = page.get_images(full=True)
            self.assertEqual(len(images), 1)
            original_image = document.xref_stream_raw(images[0][0])
            resources = document.xref_get_key(page.xref, 'Resources')
            fonts = page.get_fonts(full=True)
            drawing_pixels = crop_pixels(page, (10, 10, 220, 45))
            adjacent_pixels = [crop_pixels(page, page.search_for(c)[0]) for c in 'AD']
            page.insert_link({'kind': fitz.LINK_URI, 'from': page.search_for('B')[0],
                              'uri': 'https://example.org/scientific-source'})
            note = page.add_text_annot((215, 130), 'original note')
            note_reference = note.xref
            page = document.reload_page(page)
            links = page.get_links()
            redact_b(page)
            self.assertTrue(apply_text_redactions(page))
            page = document.reload_page(page)
            self.assertEqual(''.join(page.get_text().split()), 'ACD')
            self.assertEqual(page.get_images(full=True), images)
            self.assertEqual(page.get_fonts(full=True), fonts)
            self.assertEqual(document.xref_get_key(page.xref, 'Resources'), resources)
            self.assertEqual(document.xref_stream_raw(images[0][0]), original_image)
            self.assertEqual(crop_pixels(page, (10, 10, 220, 45)), drawing_pixels)
            self.assertEqual([crop_pixels(page, page.search_for(c)[0]) for c in 'AD'], adjacent_pixels)
            self.assertEqual(page.get_links(), links)
            self.assertEqual([a.xref for a in page.annots()], [note_reference])
            # 再次序列化并回读，不能只对 MuPDF 的编辑中间态声称成功。
            with fitz.open(stream=document.tobytes(garbage=0), filetype='pdf') as saved:
                self.assertEqual(''.join(saved[0].get_text().split()), 'ACD')
                self.assertEqual(saved[0].get_images(full=True), images)

    def test_65792_images_across_form_domains_keep_native_instancing(self):
        # 每个 Form 仅 257 次绘图，整页共 256*257=65792 次。原生会为
        # 各实例创建独立 new_res，因此整页总数不能误作单域资源名耗尽。
        # 此处真实绘制/解析/删除，不模拟 count 或把 Form 静默展开后绕过。
        with fitz.open() as document, fitz.open() as form_document:
            source = form_document.new_page(width=20, height=20)
            image = BytesIO()
            Image.new('RGB', (2, 2), (20, 90, 180)).save(image, format='PNG')
            source.insert_image((2, 2, 8, 8), stream=image.getvalue())
            contents = source.get_contents()[0]
            drawing = form_document.xref_stream(contents)
            form_document.update_stream(contents, drawing * 257)
            page = document.new_page(width=240, height=240)
            for i in range(256):
                x, y = 10 + i % 16 * 12, 10 + i // 16 * 12
                page.show_pdf_page(fitz.Rect(x, y, x + 10, y + 10), form_document, 0)
            page.insert_text((50, 225), 'ABCD', fontsize=12)
            count = sum(kind == 'fill-image' for kind, _ in page.get_bboxlog())
            self.assertEqual(count, 65792)
            self.assertTrue(page.get_xobjects())
            before = crop_pixels(page, (0, 0, 220, 205))
            redact_b(page)
            self.assertTrue(apply_text_redactions(page))
            page = document.reload_page(page)
            self.assertEqual(''.join(page.get_text().split()), 'ACD')
            self.assertEqual(sum(kind == 'fill-image' for kind, _ in page.get_bboxlog()), count)
            self.assertEqual(crop_pixels(page, (0, 0, 220, 205)), before)
            self.assertTrue(page.get_xobjects())

    def test_small_pages_keep_original_form_instancing(self):
        with fitz.open() as document, fitz.open() as form_document:
            source = form_document.new_page(width=100, height=30)
            source.insert_text((5, 20), 'ABCD')
            page = document.new_page(width=240, height=180)
            page.show_pdf_page(fitz.Rect(20, 20, 120, 50), form_document, 0)
            page.show_pdf_page(fitz.Rect(20, 100, 120, 130), form_document, 0)
            page.add_redact_annot(page.search_for('B')[0], fill=False, cross_out=False)
            self.assertTrue(apply_text_redactions(page))
            self.assertEqual(''.join(page.get_text().split()), 'ACDABCD')

    def test_geometry_matches_native_with_crop_rotation_transform_clip_and_opacity(self):
        # 两份同源自造 PDF 分别走原接口和新 text_filter；逐字与全页像素
        # 比较验证 TRM/CTM、页面裁剪、透明度和图形状态由原 sanitize 保持。
        for transformed, cropped, rotated in ((False, False, 0), (True, False, 0),
                                               (True, True, 90), (False, True, 270)):
            with self.subTest(transformed=transformed, cropped=cropped, rotated=rotated):
                with fitz.open() as document:
                    page = document.new_page(width=260, height=200)
                    page.insert_text((50, 90), 'ABCD', fontsize=16, fill_opacity=.55,
                                     morph=(fitz.Point(50, 90), fitz.Matrix(1.1, .2, .1, .9, 0, 0)) if transformed else None)
                    page.draw_rect((140, 30, 190, 60), fill=(.1, .4, .7), fill_opacity=.35)
                    # 非零裁剪原点，同时用 q/Q 和矩形剪裁包住真实文字流。
                    contents = page.get_contents()[0]
                    document.update_stream(contents, b'q -20 -20 280 230 re W n\n' + document.xref_stream(contents) + b'\nQ')
                    if cropped:
                        page.set_cropbox((10, 15, 250, 190))
                    page.set_rotation(rotated)
                    buffer = document.tobytes()
                with fitz.open(stream=buffer, filetype='pdf') as native, fitz.open(stream=buffer, filetype='pdf') as filtered:
                    native_page, filtered_page = native[0], filtered[0]
                    redact_b(native_page); redact_b(filtered_page)
                    native_page.apply_redactions(images=0, graphics=0, text=0)
                    _filter_dense_page(filtered_page, list(filtered_page.annots(types=(fitz.PDF_ANNOT_REDACT,))))
                    self.assertEqual(native_page.get_text(), filtered_page.get_text())
                    self.assertEqual(crop_pixels(native_page, native_page.rect),
                                     crop_pixels(filtered_page, filtered_page.rect))

    def test_inherited_resources_and_implicit_default_color_space_survive(self):
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((50, 90), 'ABCD')
            original_fonts = page.get_fonts(full=True)
            kind, resources = document.xref_get_key(page.xref, 'Resources')
            self.assertEqual(kind, 'xref')
            resource_reference = int(resources.split()[0])
            document.xref_set_key(resource_reference, 'ColorSpace', '<< /DefaultRGB /DeviceRGB >>')
            # 构造合法的 PageTree 继承资源；本页 Resources 必须恢复为缺省，
            # 不能用 null 覆盖继承，也不能遗漏未由显式 cs 运算引用的 DefaultRGB。
            parent = int(document.xref_get_key(page.xref, 'Parent')[1].split()[0])
            document.xref_set_key(parent, 'Resources', resources)
            fitz.mupdf.pdf_dict_del(page._pdf_page().obj(), fitz.PDF_NAME('Resources'))
            page = document.reload_page(page)
            redact_b(page)
            _filter_dense_page(page, list(page.annots(types=(fitz.PDF_ANNOT_REDACT,))))
            self.assertEqual(document.xref_get_key(page.xref, 'Resources')[0], 'null')
            self.assertEqual(document.xref_get_key(parent, 'Resources')[1], resources)
            self.assertEqual(document.xref_get_key(resource_reference, 'ColorSpace/DefaultRGB'),
                             ('name', '/DeviceRGB'))
            page = document.reload_page(page)
            self.assertEqual(page.get_fonts(full=True), original_fonts)
            self.assertEqual(''.join(page.get_text().split()), 'ACD')

    def test_text_clip_render_modes_and_quadpoints_match_native(self):
        for render_mode in (4, 7):
            with self.subTest(render_mode=render_mode):
                with fitz.open() as document:
                    page = document.new_page(width=240, height=180)
                    page.insert_text((50, 90), 'ABCD', fontsize=26, render_mode=render_mode)
                    contents = page.get_contents()[0]
                    # 使用文字轮廓剪裁后面的半透明矢量；q/Q 阻止剪裁泄漏。
                    document.update_stream(contents, b'q\n' + document.xref_stream(contents) +
                        b'\n0.2 0.5 0.8 rg 0 0 240 180 re f\nQ')
                    buffer = document.tobytes()
                with fitz.open(stream=buffer, filetype='pdf') as native, fitz.open(stream=buffer, filetype='pdf') as filtered:
                    native_page, filtered_page = native[0], filtered[0]
                    for page in (native_page, filtered_page):
                        rectangle = page.search_for('B')[0]
                        annotation = page.add_redact_annot(rectangle, fill=False, cross_out=False)
                        # 原实现以 QuadPoints 的外接框测试，不以四边形内部测试。
                        quad = fitz.Quad(rectangle.tl, rectangle.tr, rectangle.bl, rectangle.br) * ~page.transformation_matrix
                        values = [v for point in quad for v in point]
                        page.parent.xref_set_key(annotation.xref, 'QuadPoints',
                            '[' + ' '.join(map(str, values)) + ']')
                    native_page.apply_redactions(images=0, graphics=0, text=0)
                    _filter_dense_page(filtered_page, list(filtered_page.annots(types=(fitz.PDF_ANNOT_REDACT,))))
                    self.assertEqual(native_page.get_text(), filtered_page.get_text())
                    self.assertEqual(crop_pixels(native_page, native_page.rect),
                                     crop_pixels(filtered_page, filtered_page.rect))

    def test_zero_width_and_touching_intersection_follow_c_valid_rectangle(self):
        identity = fitz.mupdf.FzMatrix(1, 0, 0, 1, 0, 0)
        def intersects(coordinates, rectangles):
            bounds = fitz.mupdf.FzRect(*coordinates)
            return _text_intersects_redactions(identity.internal(), identity.internal(),
                                               bounds.internal(), rectangles)
        rectangle = fitz.mupdf.FzRect(10, 0, 20, 20)
        self.assertEqual(intersects((10, 1, 10, 9), [rectangle]), 1)
        self.assertEqual(intersects((-10, 1, -10, 9), [rectangle]), 0)
        # [0,10] 收缩到 [1,9]，恰好贴住 9 的边缘仍 valid；普通
        # Rect.intersects 会把这个零面积交集错判为不删除。
        self.assertEqual(intersects((0, 0, 10, 10), [fitz.mupdf.FzRect(9, 0, 10, 10)]), 1)
        self.assertEqual(intersects((0, 0, 10, 10), [fitz.mupdf.FzRect(9.01, 0, 10, 10)]), 0)

    def test_replacement_or_invalid_geometry_fails_explicitly(self):
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((50, 90), 'ABCD')
            page.add_redact_annot(page.search_for('B')[0], text='replacement', fill=False)
            before = page.get_text()
            with self.assertRaisesRegex(ValueError, '替换文字'):
                _filter_dense_page(page, list(page.annots()))
            self.assertEqual(page.get_text(), before)
        identity = fitz.mupdf.FzMatrix(1, 0, 0, 1, 0, 0)
        invalid = fitz.mupdf.FzRect(float('nan'), 0, 1, 1)
        with self.assertRaisesRegex(ValueError, '非有限'):
            _text_intersects_redactions(identity.internal(), identity.internal(), invalid.internal(), [])


if __name__ == '__main__':
    unittest.main()
