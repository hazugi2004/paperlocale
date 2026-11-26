"""只在原生文字和绘制记录共同证实的独立竖排图注中使用临时水平坐标。"""
from __future__ import annotations

import math
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageChops, ImageDraw


# 图注标题可以由每字之间带空格的字母组成。要求有完整的 Figure/Fig.
# 标记与编号，且图注和图的形状、相邻关系另外由检测框和原生文字证实。
_FIGURE_TITLE = re.compile(r'^\s*(?:F\s*I\s*G(?:\s*U\s*R\s*E)?)\.?\s+[A-Z]?\d+\b', re.I)


@dataclass
class CaptionPage:
    page: int
    angle: int
    original_rect: fitz.Rect
    to_upright: fitz.Matrix
    detection: dict

    @property
    def upright_rect(self):
        return self.original_rect * self.to_upright

    def to_original(self, coordinates):
        return list(fitz.Rect(coordinates) * ~self.to_upright)

    def record(self):
        return {'page': self.page, 'angle': self.angle, 'original_rect': list(self.original_rect),
                'to_upright': list(self.to_upright), 'detection': self.detection}


@dataclass
class CaptionSpace:
    path: Path
    pages: dict[int, CaptionPage]

    def records(self):
        return [page.record() for page in self.pages.values()]

    def upright_detections(self, detections):
        result = []
        for region in detections or ():
            page = self.pages.get(region['page'])
            if page is None:
                result.append(region)
            elif region == page.detection:
                # 原模型可能将竖排图注标为表尾。原生标题及绘制证据通过后，
                # 将同一图注带交给水平图注逻辑，归并全部续行。
                result.append({**region, 'kind': 'figure_caption', 'rect': list(page.upright_rect)})
            else:
                result.append({**region, 'rect': list(fitz.Rect(region['rect']) * page.to_upright)})
        return result

    def original_region(self, region):
        page = self.pages.get(region['page'])
        if page is None:
            return region
        return {**region, **{key: page.to_original(region[key]) for key in ('rect', 'writing_rect')
                             if key in region}}

    def original_item(self, item):
        # UI 和人工修订的定位、修订身份始终使用实际交付 PDF 的坐标。
        if 'regions' not in item:
            return item
        result = {**item, 'regions': [self.original_region(r) for r in item['regions']]}
        if 'anchor_region' in item:
            result['anchor_region'] = self.original_region(item['anchor_region'])
        if 'origin' in item and result['regions']:
            page = self.pages.get(result['regions'][0]['page'])
            if page:
                # 旧修复台仍按工作版面计划精确匹配字框；只给内部字符匹配保留原工作值。
                result['layout_character_position'] = {'origin': item['origin'], 'rect': item['regions'][0]['rect']}
                result['origin'] = list(fitz.Point(item['origin']) * ~page.to_upright)
        return result

    @contextmanager
    def display_coordinates(self):
        from .diagnostics import LocatedError
        try:
            yield
        except LocatedError as error:
            if not getattr(error, 'original_page_coordinates', False):
                error.items = [self.original_item(item) for item in error.items]
            raise

    def check_plan_scope(self, blocks, groups):
        from .diagnostics import LocatedError
        for number, page in self.pages.items():
            editable = [b for b in blocks if b['page'] == number and b['kind'] in {'body', 'caption'}]
            ids = {b['id'] for b in editable}
            caption_groups = [set(group) for group in groups if ids.intersection(group)]
            if not ids or caption_groups != [ids] or any(b['kind'] != 'caption' for b in editable) or any(
                    not page.upright_rect.contains(fitz.Rect(part['rect']))
                    for block in editable for part in block['parts']):
                raise LocatedError(f'第{number}页竖排文字未能合并为一处已证实图注', [{
                    'pages': [number], 'regions': [{'page': number, 'rect': list(page.upright_rect)}],
                }], 'extraction')


def _chars(line):
    return [char for span in line['spans'] for char in span['chars']]


def _origin_key(origin):
    return tuple(round(value, 3) for value in origin)


def _caption_on_page(page, region, figures):
    band = fitz.Rect(region['rect'])
    if band.is_empty or not page.rect.contains(band):
        return None
    selection = fitz.Rect(band.x0 - 1, band.y0 - 1, band.x1 + 1, band.y1 + 1)
    lines = [line for block in page.get_text('rawdict',
             flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']
             for line in block.get('lines', []) if band.intersects(fitz.Rect(line['bbox']))]
    if len(lines) < 2:
        return None
    direction = tuple(lines[0]['dir'])
    if direction not in {(0, -1), (0, 1)} or any(
            tuple(line['dir']) != direction or not selection.contains(fitz.Rect(line['bbox']))
            for line in lines):
        return None
    angle = 90 if direction == (0, -1) else 270
    width, height = page.rect.width, page.rect.height
    rotation = (fitz.Matrix(0, 1, -1, 0, height, 0) if angle == 90
                else fitz.Matrix(0, -1, 1, 0, 0, width))
    for line in lines:
        band |= fitz.Rect(line['bbox'])
    writing = band * rotation
    # 粗体标题与同一行的正文可能有不同上边界。按基线排序才能保留首行顺序。
    baseline = lambda line: (fitz.Point(line['spans'][0]['origin']) * rotation).y
    if len({round(baseline(line), 1) for line in lines}) < 2:
        return None
    lines.sort(key=lambda line: (round(baseline(line), 1),
                                 (fitz.Rect(line['bbox']) * rotation).x0))
    if not _FIGURE_TITLE.match(''.join(c['c'] for c in _chars(lines[0]))):
        return None
    if not any(0 <= writing.y0 - figure.y1 <= writing.height
               and min(figure.x1, writing.x1) - max(figure.x0, writing.x0) >= .5 * writing.width
               for figure in (fitz.Rect(item['rect']) * rotation for item in figures)):
        return None

    # 检测框只用于定位。绘制日志中的每个相交对象都必须是图注字符，
    # 并且其全部墨迹落在带内；同页的图、白底、矢量或其他文字一律不得借框放行。
    origins = Counter(_origin_key(c['origin']) for line in lines for c in _chars(line)
                      if not c['c'].isspace())
    traces = {}
    for span in page.get_texttrace():
        for code, _, origin, _ in span['chars']:
            if not chr(code).isspace():
                traces.setdefault(span['seqno'], []).append(_origin_key(origin))
    painted = Counter()
    for index, (kind, bounds) in enumerate(page.get_bboxlog()):
        rect = fitz.Rect(bounds)
        if not band.intersects(rect):
            continue
        if kind not in {'fill-text', 'stroke-text'} or not band.contains(rect) or index not in traces:
            return None
        painted.update(traces[index])
    if not painted or painted - origins or origins - painted:
        return None
    return CaptionPage(page.number + 1, angle, band, rotation, region)


def prepare_caption_space(source, detections, scratch):
    """筛选零旋转、紧邻图形、无注释链接的页，用 PDF 原始流创建水平工作副本。"""
    kinds = {'figure_caption', 'table_footnote'}
    localization = {}
    for region in detections or ():
        if region['kind'] in kinds:
            localization.setdefault(region['page'], []).append(region)
    if not localization:
        return None
    pages = {}
    with fitz.open(source) as document:
        for number, regions in localization.items():
            page = document[number - 1]
            if page.rotation or page.cropbox != page.mediabox or page.cropbox_position != fitz.Point(0, 0):
                continue
            if document.xref_get_key(page.xref, 'Annots')[0] != 'null':
                continue
            figures = [r for r in detections if r['page'] == number and r['kind'] == 'figure']
            candidates = [found for r in regions if (found := _caption_on_page(page, r, figures))]
            if len(candidates) == 1:
                pages[number] = candidates[0]
        if not pages:
            return None
        # 现有链接定位以实际页框为准：有其他页面直接指向该页时，继续原来的待确认路径。
        for page in document:
            for link in page.get_links():
                if link['kind'] == fitz.LINK_GOTO and link.get('page', -1) >= 0:
                    pages.pop(link['page'] + 1, None)
        if not pages:
            return None
        pages = dict(sorted(pages.items()))
        for number, candidate in pages.items():
            _turn(document[number - 1], candidate.angle)
        path = scratch / 'upright.pdf'
        document.save(path, garbage=0, deflate=True)

    # 向模型送出正文前，证明正反变换与序列化没有改变原图、图注或其他墨迹。
    with fitz.open(source) as original, fitz.open(path) as working:
        for number, candidate in pages.items():
            _turn(working[number - 1], -candidate.angle % 360)
        serialized = working.tobytes(garbage=0, deflate=True)
        with fitz.open(stream=serialized, filetype='pdf') as roundtrip:
            for number in pages:
                _check_original_page(original[number - 1], roundtrip[number - 1], None, scale=2)
    return CaptionSpace(path, pages)


def _turn(page, angle):
    page.set_rotation(angle)
    page.remove_rotation()


def _check_original_page(original, written, allowed, *, scale):
    from .diagnostics import LocatedError
    if (original.rect, original.mediabox, original.cropbox, original.rotation) != (
            written.rect, written.mediabox, written.cropbox, written.rotation):
        raise ValueError(f'第{original.number + 1}页竖排图注还原后页面几何发生变化')
    matrix = fitz.Matrix(scale, scale)
    a, b = original.get_pixmap(matrix=matrix, alpha=False), written.get_pixmap(matrix=matrix, alpha=False)
    diff = ImageChops.difference(Image.frombytes('RGB', (a.width, a.height), a.samples),
                                 Image.frombytes('RGB', (b.width, b.height), b.samples))
    if allowed is not None:
        r = allowed * scale
        # 核验严格限制在有独立源绘制证据的图注带，图注带外不提供正文边缘像素容差。
        ImageDraw.Draw(diff).rectangle((math.ceil(r.x0), math.ceil(r.y0),
                                        math.floor(r.x1) - 1, math.floor(r.y1) - 1), fill=(0, 0, 0))
    if delta := diff.getbbox():
        number = original.number + 1
        error = LocatedError(f'第{number}页竖排图注还原后原页保护像素发生变化', [{
            'pages': [number], 'regions': [{'page': number, 'rect': [value / scale for value in delta]}],
        }], 'preservation')
        error.original_page_coordinates = True
        raise error


def _caption_trace(page, band, matrix):
    """提取工作图注或还原图注的真实绘制字符，用同一原页坐标逐字复核。"""
    items = []
    for span in page.get_texttrace():
        direction = fitz.Point(span['dir']) * matrix - fitz.Point(0, 0) * matrix
        for code, gid, point, _ in span['chars']:
            if chr(code).isspace() or not band.contains(fitz.Point(point)):
                continue
            items.append(((code, gid, span['font'], span['type'], span['color'], tuple(direction)),
                          tuple(fitz.Point(point) * matrix), span['size']))
    return items


def restore_caption_pages(candidate, space):
    """将译后页面恢复到原方向并记录可回读的原页字形位置。"""
    expected = {}
    with fitz.open(candidate) as written:
        for number, page in space.pages.items():
            old = written[number - 1]
            expected[number] = _caption_trace(old, page.upright_rect, ~page.to_upright)
            _turn(old, -page.angle % 360)
        data = written.tobytes(garbage=0, deflate=True)
    candidate.write_bytes(data)
    return expected


def verify_caption_pages(source, candidate, space, expected, edited_pages):
    evidence = []
    with fitz.open(source) as original, fitz.open(candidate) as written:
        for number, page in space.pages.items():
            allowed = page.original_rect if number in edited_pages else None
            a, b = original[number - 1], written[number - 1]
            actual = _caption_trace(b, page.original_rect, fitz.Matrix(1, 1))
            if len(actual) != len(expected[number]) or any(
                    before[0] != after[0] or abs(before[2] - after[2]) > .01
                    or any(abs(x - y) > .01 for x, y in zip(before[1], after[1]))
                    for before, after in zip(expected[number], actual)):
                raise ValueError(f'第{number}页竖排图注还原后字形、方向或原点发生变化')
            _check_original_page(a, b, allowed, scale=4)
            evidence.append({**page.record(), 'dpi': 288, 'outside_pixels_equal': True,
                             'glyph_positions_verified': len(actual)})
    return evidence
