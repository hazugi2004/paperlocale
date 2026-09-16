"""用实际字形轮廓测量墨迹，不把字体全局升降部当作每个汉字的高度。"""
from functools import lru_cache
from io import BytesIO

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen


@lru_cache(maxsize=4)
def _tables(font):
    # 同一排版阶段反复使用完整字体和最终子集；按字体对象隔离，不能只按字体名缓存。
    table = TTFont(BytesIO(font.buffer))
    return table.getGlyphSet(), table.getBestCmap(), table['head'].unitsPerEm


@lru_cache(maxsize=4096)
def glyph_ink(font, character):
    glyphs, cmap, units = _tables(font)
    name = cmap.get(ord(character))
    if name is None:
        raise ValueError(f'中文字体缺少字形：{character!r}')
    pen = BoundsPen(glyphs)
    glyphs[name].draw(pen)
    return tuple(x / units for x in pen.bounds) if pen.bounds is not None else None


def line_ink(font, text, size):
    """返回相对基线的 (最左, 最下, 最右, 最上)，坐标沿字体的 y 轴向上。"""
    boxes, advance = [], 0
    for character in text:
        box = glyph_ink(font, character)
        if box:
            boxes.append((advance + box[0] * size, box[1] * size,
                          advance + box[2] * size, box[3] * size))
        advance += font.text_length(character, fontsize=size)
    if not boxes:
        return (0, 0, 0, 0)
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)
