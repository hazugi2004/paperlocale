"""用实际字形轮廓测量墨迹，不把字体全局升降部当作每个汉字的高度。"""
from functools import lru_cache
from io import BytesIO

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen


def _source_anchor_glyphs(page, characters, cache):
    """按嵌入 CFF 字形轮廓核验锚点，排除字框内其他基线的正文墨迹。

    返回每个非空字形的完整墨迹矩形，不合并上下标/标点的空白角落。
    仅支持已明确验证的水平、标准 FontMatrix CFF；未知字体、编码或
    缺失字符返回 None，由调用方继续使用原完整字框检查，不静默跳过。
    """
    import re
    import pymupdf as fitz
    from fontTools.cffLib import CFFFontSet
    font_candidates = {}
    for font in page.get_fonts(full=True):
        # MuPDF 的绘制字体名可先截到31字符，再去除六字母子集前缀；
        # get_fonts 返回完整 BaseFont。下方按完整描述符核对别名，若截短
        # 导致两个不同字体同名，仍拒绝绑定，不能任意选择一个公式字体。
        for name in (font[3], font[3][:31]):
            name = re.sub(r'^[A-Z]{6}\+', '', name)
            font_candidates.setdefault(name, set()).add(font[0])
    fonts = {}
    for name, refs in font_candidates.items():
        identities = set()
        for ref in refs:
            kind, descriptor = page.parent.xref_get_key(ref, 'FontDescriptor')
            identities.add((page.parent.xref_get_key(ref, 'Subtype')[1],
                            page.parent.xref_get_key(ref, 'BaseFont')[1],
                            page.parent.xref_object(int(descriptor.split()[0]))
                            if kind == 'xref' else descriptor))
        # 同一嵌入字体可被多个公式 XObject 重复引用。重放使用字形名，
        # 不使用各引用的字符编码；仅当原字体名、类型和完整描述符一致
        # （包括 FontFile3 程序引用）才将这些引用视为同一字体。
        if len(identities) == 1:
            fonts[name] = min(refs)
    traces = {}
    continuations = {}
    for span in page.get_texttrace():
        if tuple(span['dir']) != (1, 0):
            continue
        leader = None
        for item in span['chars']:
            if item[1] < 0:
                # MuPDF 将一个原字形的后续 Unicode 映射标为 gid=-1。
                # rawdict 把这些零宽字符放在主字形右边界，texttrace 却
                # 沿用主字形原点；它们不能作为第二个绘制字形参与匹配。
                if leader is not None:
                    key = (item[0], round(leader[3][2], 3), round(item[2][1], 3))
                    continuations.setdefault(key, []).append((span, leader))
                continue
            leader = item
            key = (item[0], round(item[2][0], 3), round(item[2][1], 3))
            traces.setdefault(key, []).append((span, item))
    glyphs = []
    for char in characters:
        if not char['text'].strip():
            continue
        key = (ord(char['text']), round(char['origin'][0], 3), round(char['origin'][1], 3))
        if 'rect' in char and abs(char['rect'][2] - char['rect'][0]) < .001:
            matches = continuations.get(key, [])
            if not matches:
                # 续字符同样可能受到不同 ToUnicode 的影响；原点唯一且
                # 下方主字形身份一致时，沿用原字符文本，不据此增加字形。
                matches = [value for k, values in continuations.items() if k[1:] == key[1:]
                           for value in values]
            if len(matches) != 1 or not glyphs:
                return None
            span, leader = matches[0]
            previous = glyphs[-1]
            # 只有本锚点内已经绑定的同一主字形才可接纳续字符；不猜测
            # 被拆到其他锚点的合字，也不丢弃文本。ToUnicode 保留完整映射。
            if (previous['glyph_id'] != leader[1] or previous['origin'] != leader[2]
                    or previous['xref'] != fonts.get(span['font'])):
                return None
            previous['text'] += char['text']
            continue
        matches = traces.get(key, [])
        if not matches:
            # 同一 CFF 程序在不同公式中可有不同 ToUnicode，rawdict 与
            # texttrace 的 Unicode 可能不一致。以唯一原点及实际字宽
            # 确认同一绘制字形；保留 rawdict 文本，绝不按字母猜公式轮廓。
            # 控制码兼容原来的唯一位置检查；可打印字符必须同时有字框证据。
            matches = [value for trace_key, values in traces.items() if trace_key[1:] == key[1:]
                       for value in values if not char['text'].isprintable() or
                       ('rect' in char and abs(value[1][3][0]-char['rect'][0]) < .001
                        and abs(value[1][3][2]-char['rect'][2]) < .001)]
        if len(matches) != 1:
            return None
        span, item = matches[0]
        xref = fonts.get(span['font'])
        if xref is None or item[1] < 0:
            return None
        if page.parent.xref_get_key(xref, 'Subtype')[1] != '/Type1':
            return None
        if xref not in cache:
            _, extension, _, data = page.parent.extract_font(xref)
            if extension != 'cff':
                cache[xref] = None
            else:
                cff = CFFFontSet()
                cff.decompile(BytesIO(data), None)
                cache[xref] = cff[0] if cff[0].FontMatrix == [.001, 0, 0, .001, 0, 0] else None
        top = cache[xref]
        if top is None or item[1] >= len(top.charset):
            return None
        name = top.charset[item[1]]
        glyph = top.CharStrings[name]
        pen = BoundsPen(top.CharStrings)
        glyph.draw(pen)
        if pen.bounds is None:
            continue
        left, bottom, right, upper = pen.bounds
        x, y = item[2]
        scale = span['size'] / 1000
        glyphs.append({'rect': fitz.Rect(x + left * scale, y - upper * scale,
                                        x + right * scale, y - bottom * scale),
                       'xref': xref, 'name': name, 'origin': (x, y), 'size': span['size'],
                       'text': char.get('semantic_text', char['text']), 'color': span['color'], 'glyph_id': item[1]})
    return glyphs or None


def source_anchor_ink_rectangles(page, characters, cache):
    glyphs = _source_anchor_glyphs(page, characters, cache)
    return [g['rect'] for g in glyphs] if glyphs else None


def source_anchor_masks(source, regions, scale=4):
    """用原嵌入字体生成固定字形的独立透明掩模，不重绘或替换交付 PDF。

    实际字形的矩形仍可能包住旁边正文（尤其斜体 f）。掩模沿用原 CFF
    字形程序、字体描述和字形位置，仅为单字重绑编码；原/译图像只在
    固定字形实际覆盖的像素上比较。未知编码继续走完整原字框的严格检查。
    """
    import pymupdf as fitz
    from PIL import Image
    from .safe_text import fixed_characters
    masks, supported, cache, font_refs = {}, set(), {}, {}
    with fitz.open(source) as original, fitz.open(source) as scratch:
        pages = {}
        for index, region in enumerate(regions):
            page = original[region['page'] - 1]
            if page.rotation or page.rect.x0 or page.rect.y0:
                continue
            glyphs = _source_anchor_glyphs(page, fixed_characters(region), cache)
            if glyphs:
                supported.add(index)
                bucket = pages.setdefault(region['page'], {})
                for glyph in glyphs:
                    bucket[(glyph['xref'], glyph['name'], glyph['origin'], glyph['size'])] = glyph
        for number, glyphs in pages.items():
            bounds = original[number - 1].rect
            page = scratch.new_page(width=bounds.width, height=bounds.height)
            resources, commands = {}, []
            for glyph in glyphs.values():
                key = glyph['xref'], glyph['name']
                if key not in font_refs:
                    base = scratch.xref_get_key(glyph['xref'], 'BaseFont')[1]
                    descriptor = scratch.xref_get_key(glyph['xref'], 'FontDescriptor')[1]
                    name = ''.join(chr(c) if 33 <= c <= 126 and chr(c) not in '#%()/<>[]{}'
                                   else f'#{c:02X}' for c in glyph['name'].encode())
                    xref = scratch.get_new_xref()
                    scratch.update_object(xref, f'<< /Type /Font /Subtype /Type1 /BaseFont {base} '
                        f'/FontDescriptor {descriptor} /FirstChar 0 /LastChar 0 /Widths [0] '
                        f'/Encoding << /Type /Encoding /Differences [0 /{name}] >> >>')
                    font_refs[key] = xref
                xref = font_refs[key]
                alias = f'G{xref}'
                resources[alias] = xref
                x, y = glyph['origin']
                commands.append(f'BT /{alias} {glyph["size"]:.10f} Tf 1 0 0 1 '
                                f'{x:.10f} {bounds.height - y:.10f} Tm <00> Tj ET')
            scratch.xref_set_key(page.xref, 'Resources', '<< /Font << ' +
                ' '.join(f'/{name} {xref} 0 R' for name, xref in resources.items()) + ' >> >>')
            stream = scratch.get_new_xref()
            scratch.update_object(stream, '<<>>')
            scratch.update_stream(stream, ('\n'.join(commands)).encode())
            scratch.xref_set_key(page.xref, 'Contents', f'{stream} 0 R')
            page = scratch.reload_page(page)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
            masks[number] = Image.frombytes('RGBA', (pix.width, pix.height), pix.samples).getchannel('A')
    return masks, supported


def open_source_for_editing(source, *, normalize_descriptors=True):
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
            descriptor_xref = int(descriptor[1].split()[0])
            stream_ref = document.xref_get_key(descriptor_xref, 'FontFile3')
            if stream_ref[0] != 'xref':
                continue
            stream_xref = int(stream_ref[1].split()[0])
            original = originals.get(stream_xref, document.xref_stream(stream_xref))
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
                # MuPDF 还使用PDF描述符的外框和升降部。只更新CFF会留下
                # 旧的巨大字框，阻挡下一行正文删除。同步临时指标；同一流
                # 可被多个描述符引用，因此逐个保存并更新，最终全部还原。
                if normalize_descriptors:
                    originals.setdefault(descriptor_xref, document.xref_object(descriptor_xref))
                    document.xref_set_key(descriptor_xref, 'FontBBox', '[' + ' '.join(map(str, top.FontBBox)) + ']')
                    document.xref_set_key(descriptor_xref, 'Ascent', str(top.FontBBox[3]))
                    document.xref_set_key(descriptor_xref, 'Descent', str(top.FontBBox[1]))
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
        if isinstance(stream, str):
            document.update_object(xref, stream)
        else:
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
