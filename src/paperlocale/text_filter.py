"""为图片实例极多的无 Form 页面清除正文，保留原资源绑定。

MuPDF 1.25.2 的 redaction 为每次图片 Do 创建 Im1…Im65535，图片即使
不变也会耗尽名字。本模块只处理已证实的这个边界；普通页仍调用原接口。
这是 PaperLocale 的文字清除，不是通用隐私 redaction：链接及其他注释
保持原样，不能声称清除了它们可能携带的文字/信息。最终完整性门禁不变。
"""
from ctypes import c_float
import math

import pymupdf as fitz


_RESOURCE_NAME_LIMIT = 65535


def _redaction_rectangles(annotations):
    """读取 PDF 原坐标，避免 Page 坐标再转换产生边缘舍入差异。

    与 pdf-clean.c 的 pdf_redact_text_filter 一致，QuadPoints 使用每个
    四边形的外接矩形；空宽字框也可相交。填色/替换文字不属于本函数用途。
    """
    mupdf = fitz.mupdf
    rectangles = []
    for annotation in annotations:
        values = annotation._get_redact_values()
        if values.get('fill') or values.get('text'):
            raise ValueError('高密度图片页只支持无填色、无替换文字的正文清除')
        obj = mupdf.pdf_annot_obj(annotation.this)
        points = mupdf.pdf_dict_get(obj, fitz.PDF_NAME('QuadPoints'))
        count = mupdf.pdf_array_len(points)
        if count:
            if count % 8:
                raise ValueError('正文清除注释的 QuadPoints 不完整')
            boxes = [mupdf.fz_rect_from_quad(mupdf.pdf_to_quad(points, i))
                     for i in range(0, count, 8)]
        else:
            boxes = [mupdf.pdf_dict_get_rect(obj, fitz.PDF_NAME('Rect'))]
        for box in boxes:
            if not all(math.isfinite(v) for v in (box.x0, box.y0, box.x1, box.y1)) or not mupdf.fz_is_valid_rect(box):
                raise ValueError('正文清除注释包含非法坐标')
        rectangles.extend(boxes)
    return rectangles


def _text_intersects_redactions(trm, ctm, bbox, rectangles):
    """逐运算复现 MuPDF 1.25.2 pdf-clean.c:502 的字符删除谓词。

    变换、相交、有效性均调用原 MuPDF，不能用 Rect.intersects 替代：
    后者拒绝零宽交集，会遗漏组合重音等有位置但零进宽的字形。
    C 原实现每一步都是 float，显式 c_float 保留其舍入顺序；Python
    double 先算完再赋回 float 可能在紧贴固定字符的边界选择另一个字符。
    """
    mupdf = fitz.mupdf
    if not all(math.isfinite(v) for v in (bbox.x0, bbox.y0, bbox.x1, bbox.y1,
                trm.a, trm.b, trm.c, trm.d, trm.e, trm.f,
                ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)):
        raise ValueError('正文清除遇到非有限字形坐标或变换')
    matrix = mupdf.fz_concat(mupdf.FzMatrix(trm), mupdf.FzMatrix(ctm))
    box = mupdf.fz_transform_rect(mupdf.FzRect(bbox), matrix)
    if not all(math.isfinite(v) for v in (box.x0, box.y0, box.x1, box.y1)):
        raise ValueError('正文清除遇到非有限字形坐标')
    dx = c_float(c_float(box.x1 - box.x0).value / 10).value
    dy = c_float(c_float(box.y1 - box.y0).value / 10).value
    box.x0 = c_float(box.x0 + dx).value
    box.x1 = c_float(box.x1 - dx).value
    box.y0 = c_float(box.y0 + dy).value
    box.y1 = c_float(box.y1 - dy).value
    return int(any(mupdf.fz_is_valid_rect(mupdf.fz_intersect_rect(box, rectangle))
                   for rectangle in rectangles))


def _verify_resource_bindings(original, filtered):
    """过滤器只能引用原来同名同对象的资源；不接受新图片/字体或改绑名字。

    sanitize 会清理未显式引用的资源，包括隐式 Default* 色彩空间；核对
    活跃绑定后恢复完整原 Resources，而不是让清理影响图表颜色或源字体。
    """
    mupdf = fitz.mupdf
    for i in range(mupdf.pdf_dict_len(filtered)):
        category = mupdf.pdf_dict_get_key(filtered, i)
        actual = mupdf.pdf_dict_get_val(filtered, i)
        expected = mupdf.pdf_dict_get(original, category)
        if mupdf.pdf_is_dict(actual):
            for j in range(mupdf.pdf_dict_len(actual)):
                name = mupdf.pdf_dict_get_key(actual, j)
                if mupdf.pdf_objcmp(mupdf.pdf_dict_get_val(actual, j),
                                    mupdf.pdf_dict_get(expected, name)):
                    raise ValueError('正文清除改变了原 PDF 的资源名字或对象绑定')
        elif mupdf.pdf_objcmp(actual, expected):
            raise ValueError('正文清除改变了原 PDF 的资源绑定')


def _filter_dense_page(page, annotations):
    """执行无 Form 页的原生 sanitize，只过滤文字，不递归图案/软蒙版。"""
    mupdf = fitz.mupdf
    rectangles = _redaction_rectangles(annotations)

    class TextFilter(mupdf.PdfSanitizeFilterOptions2):
        def __init__(self):
            super().__init__()
            self.use_virtual_text_filter()

        def text_filter(self, context, characters, length, trm, ctm, bbox):
            return _text_intersects_redactions(trm, ctm, bbox, rectangles)

    native = page._pdf_page()
    resources = mupdf.pdf_page_resources(native)
    # 单独保留页本身的 Resources 条目，缺省时恢复继承关系；不可写入 null
    # 遮住父 PageTree 的 Resources。recurse=0 确保不修改共享资源内部对象。
    resource_key = fitz.PDF_NAME('Resources')
    local_resources = mupdf.pdf_dict_get(native.obj(), resource_key)
    options = fitz._make_PdfFilterOptions(instance_forms=0, recurse=0, ascii=1,
                                          sanitize=1, sopts=TextFilter())
    mupdf.pdf_filter_page_contents(native.doc(), native, options)
    _verify_resource_bindings(resources, mupdf.pdf_page_resources(native))
    if local_resources.m_internal:
        mupdf.pdf_dict_put(native.obj(), resource_key, local_resources)
    else:
        mupdf.pdf_dict_del(native.obj(), resource_key)
    # 仅消费本次输入的 Redact 注释；链接/其他注释保持原对象与坐标。
    # 若原文携带真正用于隐私脱敏的 Redact，应使用通用原生接口而非此函数。
    for annotation in annotations:
        page.delete_annot(annotation)
    return True


def apply_text_redactions(page):
    """清除正文并返回是否处理过注释；失败向上传播，不发布部分结果。

    只有 fill-image 实例数达到 MuPDF 名字上限且无 Form 才启用新路径。
    Form 可能复用在不同坐标，关闭实例化会误删或漏删其中一处，故含
    Form 的页继续沿原 apply_redactions。每个 Form 有独立资源命名域，
    整页图片总数超限并不代表各个域超限；不能据此拒绝原生支持的页面。
    """
    annotations = list(page.annots(types=(fitz.PDF_ANNOT_REDACT,)) or ())
    if not annotations:
        return False
    count = sum(kind == 'fill-image' for kind, _ in page.get_bboxlog())
    if count < _RESOURCE_NAME_LIMIT or page.get_xobjects():
        return page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                     graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                     text=fitz.PDF_REDACT_TEXT_REMOVE)
    return _filter_dense_page(page, annotations)
