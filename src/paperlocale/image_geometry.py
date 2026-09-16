"""依据实际图片绘制和 alpha 支持集计算可见占用，不改写原 PDF 对象。"""
from io import BytesIO
import re

import pymupdf as fitz
from PIL import Image, ImageChops


def visible_image_regions(page, detections=()):
    """返回页坐标矩形；零 alpha 区域不占用正文空间。

    get_image_info 也会报告参与透明度/遮罩计算的图片。这些图片的大外框
    未必是页面实际绘制范围，因此先与 MuPDF 的 fill-image 绘制记录匹配。
    提取时使用无限裁剪，避免完全包含规则漏掉跨 CropBox 的图像。
    alpha 的任何非零值都视为可见，不用阈值抹掉半透明细线。
    """
    painted = [fitz.Rect(box) for kind, box in page.get_bboxlog() if kind == 'fill-image']
    figures = [fitz.Rect(r['rect']) for r in detections
               if r['page'] == page.number + 1 and r['kind'] in {'figure', 'table'}]
    regions = []
    for block in page.get_text('dict', clip=fitz.INFINITE_RECT())['blocks']:
        if block['type'] != 1:
            continue
        outer = fitz.Rect(block['bbox'])
        if not any(max(abs(a - b) for a, b in zip(outer, box)) < .01 for box in painted):
            continue
        with Image.open(BytesIO(block['image'])) as image:
            alpha = image.convert('RGBA').getchannel('A')
        if block.get('mask'):
            with Image.open(BytesIO(block['mask'])) as mask:
                mask = mask.convert('L')
                if mask.size != alpha.size:
                    raise ValueError('图片与透明度蒙版尺寸不一致，不能推测其对齐方式')
                alpha = ImageChops.multiply(alpha, mask)
        bounds = alpha.getbbox()
        if bounds is None:
            continue
        matrix = fitz.Matrix(1 / alpha.width, 1 / alpha.height) * fitz.Matrix(block['transform'])
        visible = fitz.Rect(bounds) * matrix
        # 若可见支持集整体落在已识别图表内，外围透明孔洞不影响正文归属，
        # 可用一个保守外框；其他情形精确合并逐行非零区间，保留透明孔洞。
        if alpha.getextrema()[0] > 0 or any(box.contains(visible) for box in figures):
            rectangles = [bounds]
        else:
            data, active, rectangles = alpha.tobytes(), {}, []
            for y in range(alpha.height + 1):
                runs = set() if y == alpha.height else {
                    (m.start(), m.end()) for m in re.finditer(b'[^\x00]+', data[y * alpha.width:(y + 1) * alpha.width])}
                for interval in active.keys() - runs:
                    rectangles.append((interval[0], active[interval], interval[1], y))
                active = {interval: active.get(interval, y) for interval in runs}
        for rectangle in rectangles:
            mapped = (fitz.Rect(rectangle) * matrix) & page.rect
            if not mapped.is_empty:
                regions.append({'page': page.number + 1, 'rect': list(mapped), 'kind': 'figure'})
    # 同一图片可能多次用于绘制；分类只需要可见范围，不重复放大其权重。
    return list({tuple(r['rect']): r for r in regions}.values())
