"""用实际字形轮廓测量墨迹，不把字体全局升降部当作每个汉字的高度。"""
from functools import lru_cache
from io import BytesIO

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen


def open_source_for_editing(source):
    """为文字删除创建临时工作文档，不改变输入 PDF 或最终字体轮廓。

    部分出版社 CFF 子集沿用完整字体的巨大 FontBBox，MuPDF 会因此把
    下一行正文和数学符号当成重叠字符。只对高度超过 2 em 的 CFF 外框
    重新计算子集中全部字形的真实包围框；字形程序、编码与宽度不变。
    返回工作文档和原始字体流；调用者必须在保存产物前恢复字体流。
    不支持的字体保持原状，后续安全删除检查继续拒绝未知重叠。
    """
    import pymupdf as fitz
    from fontTools.cffLib import CFFFontSet
    from types import SimpleNamespace

    document = fitz.open(source)
    originals = {}
    try:
        fonts = {font[0]: font for page in document for font in page.get_fonts(full=True)}
        for xref, font in fonts.items():
            if font[1] != 'cff':
                continue
            descriptor = document.xref_get_key(xref, 'FontDescriptor')
            if descriptor[0] != 'xref':
                continue
            stream_ref = document.xref_get_key(int(descriptor[1].split()[0]), 'FontFile3')
            if stream_ref[0] != 'xref':
                continue
            stream_xref = int(stream_ref[1].split()[0])
            if stream_xref in originals:
                continue
            original = document.xref_stream(stream_xref)
            cff = CFFFontSet()
            cff.decompile(BytesIO(original), None)
            top = cff[0]
            matrix = top.FontMatrix
            if matrix != [.001, 0, 0, .001, 0, 0] or top.FontBBox[3] - top.FontBBox[1] <= 2000:
                continue
            old_box = list(top.FontBBox)
            output = BytesIO()
            cff.compile(output, SimpleNamespace(recalcBBoxes=True))
            if top.FontBBox != old_box:
                originals[stream_xref] = original
                document.update_stream(stream_xref, output.getvalue())
        if originals:
            # 重新打开才能使 MuPDF 丢弃已经缓存的字体指标。
            normalized = fitz.open(stream=document.tobytes(), filetype='pdf')
            document.close()
            document = normalized
            # 元数据调整必须在实际页面上无任何可见改变，才允许进入删除步骤。
            with fitz.open(source) as original:
                for before, after in zip(original, document):
                    a = before.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    b = after.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    if a.samples != b.samples:
                        raise ValueError('临时字体外框修正改变了原页面画面')
        return document, originals
    except BaseException:
        document.close()
        raise


def restore_source_fonts(document, originals):
    """写入前恢复原始 CFF 字节，最终 PDF 不携带临时字体指标修改。"""
    for xref, stream in originals.items():
        document.update_stream(xref, stream)


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
