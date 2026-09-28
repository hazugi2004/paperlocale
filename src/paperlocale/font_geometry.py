"""用实际字形轮廓测量墨迹，不把字体全局升降部当作每个汉字的高度。"""
from functools import lru_cache
from io import BytesIO

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen


def _font_descriptor(document, xref):
    """取得简单字体或单一 CID 后代的原描述符；不猜测未知字体结构。"""
    import re
    if document.xref_get_key(xref, 'Subtype')[1] == '/Type0':
        kind, value = document.xref_get_key(xref, 'DescendantFonts')
        if kind == 'xref':
            value = document.xref_object(int(value.split()[0]))
        match = re.fullmatch(r'\[\s*(\d+) 0 R\s*\]', value)
        if not match:
            return None
        xref = int(match[1])
        if document.xref_get_key(xref, 'Subtype')[1] not in {'/CIDFontType2', '/CIDFontType0'}:
            return None
    kind, value = document.xref_get_key(xref, 'FontDescriptor')
    return int(value.split()[0]) if kind == 'xref' else None


def _truetype_trace_fonts(page, cache):
    """在只读内存副本中标记字体资源名，找回 trace 丢失的子集身份。

    同页 TimesNewRoman 两个子集可能拥有相同的 Unicode/GID，却包含不同
    字体程序。不能凭名称或 cmap 任选其一。仅改副本的 PDF 名称后重新读取
    绘制记录；原点、GID、Unicode、字号必须逐项一致，才接受资源引用。
    字体字节和源文件完全不改。结果在本次绑定内缓存，不持有打开的副本文档。
    """
    import pymupdf as fitz
    import re
    key = ('truetype-traces',)
    if key not in cache:
        with fitz.open(stream=page.parent.tobytes(), filetype='pdf') as probe:
            refs = {font[0] for p in probe for font in p.get_fonts(full=True) if font[1] == 'ttf' or font[2] == 'Type0' and font[1] in {'cff', 'cid'}}
            descriptors = {}
            for ref in refs:
                descriptor = _font_descriptor(probe, ref)
                if descriptor is not None:
                    # 共享同一描述符的重复引用使用同一个名字，其字体流相同。
                    owner = descriptors.setdefault(descriptor, ref)
                    name = f'/PLTrueType{owner}'
                    probe.xref_set_key(ref, 'BaseFont', name)
                    probe.xref_set_key(descriptor, 'FontName', name)
                    if probe.xref_get_key(ref, 'Subtype')[1] == '/Type0':
                        kind, value = probe.xref_get_key(ref, 'DescendantFonts')
                        if kind == 'xref':
                            value = probe.xref_object(int(value.split()[0]))
                        descendant = int(re.fullmatch(r'\[\s*(\d+) 0 R\s*\]', value)[1])
                        probe.xref_set_key(descendant, 'BaseFont', name)
            data = probe.tobytes()
        with fitz.open(stream=data, filetype='pdf') as tagged:
            cache[key] = {}
            for p in tagged:
                traces = cache[key][p.number] = {}
                for span in p.get_texttrace():
                    if span['font'].startswith('PLTrueType'):
                        for char in span['chars']:
                            traces.setdefault(char[:3], []).append(span)
    return cache[key].get(page.number, {})


def _native_font_evidence(document, xref, cache):
    """读取渲染器实际使用的字体编码；只接纳水平单字节或 Identity-H。

    输入源 PDF 字体引用，输出 GID→原编码及原生字体句柄。保留/释放
    MuPDF 引用计数，缓存不会持有已释放的 font descriptor。编码别名
    存在歧义时由调用者拒绝，不用 Unicode 反猜原科学字形。
    """
    import pymupdf as fitz
    key = ('native-font', xref)
    if key not in cache:
        subtype = document.xref_get_key(xref, 'Subtype')[1]
        encoding = document.xref_get_key(xref, 'Encoding')[1]
        if subtype == '/Type0' and encoding != '/Identity-H':
            cache[key] = None
        else:
            pdf = fitz.mupdf.pdf_document_from_fz_document(document.this)
            obj = fitz.mupdf.pdf_load_object(pdf, xref)
            descriptor = fitz.mupdf.ll_pdf_load_font(pdf.m_internal, None, obj.m_internal)
            try:
                codes, widths = {}, {}
                size = 2 if subtype == '/Type0' else 1
                for code in range(65536 if size == 2 else 256):
                    cid = fitz.mupdf.ll_pdf_lookup_cmap(descriptor.encoding, code)
                    gid = fitz.mupdf.ll_pdf_font_cid_to_gid(descriptor, cid) if cid >= 0 else 0
                    if gid > 0:
                        codes.setdefault(gid, []).append(code)
                        widths[code] = fitz.mupdf.ll_pdf_lookup_hmtx(descriptor, cid).w
                cache[key] = {'codes': codes, 'widths': widths, 'bytes': size,
                    'font': fitz.mupdf.FzFont(fitz.mupdf.ll_fz_keep_font(descriptor.font))}
                if descriptor.wmode:
                    cache[key] = None
            finally:
                fitz.mupdf.ll_pdf_drop_font(descriptor)
    return cache[key]


def _source_anchor_glyphs(page, characters, cache, *, include_empty=False):
    """按嵌入 Type 1/CFF/TrueType 字形轮廓核验锚点，排除相邻正文墨迹。

    返回每个非空字形的完整墨迹矩形，不合并上下标/标点的空白角落。
    支持水平等比 FontMatrix 的 Type 1/CFF 和有资源级证据的嵌入 TrueType；未知字体、编码或
    缺失字符返回 None，由调用方继续使用原完整字框检查，不静默跳过。
    """
    import re
    import pymupdf as fitz
    from fontTools.cffLib import CFFFontSet
    font_candidates = {}
    resources = page.get_fonts(full=True)
    truetype_refs = {f[0] for f in resources if f[1] == 'ttf' or f[2] == 'Type0' and f[1] in {'cff', 'cid'}}
    for font in resources:
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
    resolved_spans = {}

    def font_for_span(span, item):
        """同名不同子集按整条绘制记录的 Unicode/GID 对绑定，不猜字体。

        出版社可在一页同时嵌入正文和公式的 STIX 子集，去掉子集前缀后
        名称相同但字形编号不同。只有原嵌入程序对所有非空白绘制字符的
        Unicode→GID 映射都吻合，且候选程序字节完全一致，才允许绑定。
        无映射、未知编码或不同轮廓仍有歧义时返回 None，保留原安全门禁。
        """
        # TrueType 的 cmap 可能被出版社整个移除，资源级绘制证据仍能
        # 唯一确定原 GID；没有证据时继续拒绝，而不是用系统字体替代。
        # MuPDF 会合并同名字体的连续绘制记录，改名后 span 边界和 seqno
        # 因而可能改变；按单字 Unicode/GID/原点及绘制属性对应，不按序号。
        if truetype_refs:
            tagged = _truetype_trace_fonts(page, cache).get(item[:3], [])
            matches = {int(s['font'].removeprefix('PLTrueType')) for s in tagged
                       if all(s[k] == span[k] for k in ('dir', 'size', 'color'))}
            if matches:
                return next(iter(matches)) if len(matches) == 1 else None
            if truetype_refs.intersection(font_candidates.get(span['font'], ())):
                unembedded = {r[0] for r in resources if r[0] in font_candidates.get(span['font'], ())
                              and r[1] == 'n/a' and r[2] == 'TrueType'}
                if unembedded:
                    return next(iter(unembedded)) if len(unembedded) == 1 else None
                # 同名的简单 CFF 不在 Type0/TTF 探针中；继续用下方
                # 原程序的完整 Unicode/GID 证据核对，不能被同名 CID 误拒。
        if span['font'] in fonts:
            return fonts[span['font']]
        key = id(span)
        if key not in resolved_spans:
            pairs = {(code, gid) for code, gid, _, _ in span['chars']
                     if gid >= 0 and not chr(code).isspace()}
            matches = {}
            for ref in font_candidates.get(span['font'], ()):
                if page.parent.xref_get_key(ref, 'Subtype')[1] != '/Type1':
                    continue
                cache_key = ('unicode-font', ref)
                if cache_key not in cache:
                    _, extension, _, data = page.parent.extract_font(ref)
                    cache[cache_key] = (fitz.Font(fontbuffer=data), data) if extension in {'cff', 'pfa'} else None
                candidate = cache[cache_key]
                if candidate and pairs and all(gid > 0 and candidate[0].has_glyph(
                        code, fallback=False) == gid for code, gid in pairs):
                    matches.setdefault(candidate[1], []).append(ref)
            resolved_spans[key] = min(next(iter(matches.values()))) if len(matches) == 1 else None
        return resolved_spans[key]

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
                    # PDF 声明字宽与字体内部宽度可能取整不同；零宽续字符
                    # 在 rawdict 主字形右边界，而不是 trace 计算的右边界。
                    raw_leaders = [c for c in characters if c['text'] == chr(leader[0])
                                   and tuple(c['origin']) == tuple(leader[2]) and 'rect' in c]
                    edge = raw_leaders[0]['rect'][2] if len(raw_leaders) == 1 else leader[3][2]
                    key = (item[0], round(edge, 3), round(item[2][1], 3))
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
            if matches:
                if len(matches) != 1 or not glyphs:
                    return None
                span, leader = matches[0]
                previous = glyphs[-1]
                # 只有已有主字形才可合并续字；独立零宽字必须走真实 trace
                # 绑定，例如出版物插入的 U+200B 空轮廓，不能当作损坏合字。
                if (previous['glyph_id'] != leader[1] or previous['origin'] != leader[2]
                        or previous['xref'] != font_for_span(span, leader)):
                    return None
                previous['text'] += char.get('semantic_text', char['text'])
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
        xref = font_for_span(span, item)
        if xref is None or item[1] < 0:
            return None
        subtype = page.parent.xref_get_key(xref, 'Subtype')[1]
        if subtype not in {'/Type1', '/TrueType', '/Type0'}:
            return None
        if subtype in {'/TrueType', '/Type0'} and page.parent.extract_font(xref)[1] != 'ttf':
            # 未嵌入 TrueType 以及 CID-CFF 直接沿用源字体资源和原编码。
            # 不替换系统字体，不转换科学字形；只有原 GID 唯一对应一个
            # 源编码时才重放，且落盘后仍逐字核验 GID、坐标、颜色。
            native = _native_font_evidence(page.parent, xref, cache)
            if native is None or item[1] not in native['codes']:
                return None
            codes = native['codes'][item[1]]
            if len(codes) != 1:
                # WinAnsi 的连字符与软连字符可共用 GID，但字宽不同。
                # 用原 rawdict 推进宽度选码，不能仅取最小编码猜测。
                if 'rect' not in char:
                    return None
                width = char['rect'][2] - char['rect'][0]
                codes = [code for code in codes if abs(
                    native['widths'][code] * span['size'] / 1000 - width) < .001]
                if not codes or len({native['widths'][c] for c in codes}) != 1:
                    return None
            bound = fitz.mupdf.fz_bound_glyph(native['font'], item[1],
                                             fitz.mupdf.FzMatrix(1, 0, 0, 1, 0, 0))
            x, y = item[2]
            scale = span['size']
            glyphs.append({'rect': fitz.Rect(x+bound.x0*scale, y-bound.y1*scale,
                                            x+bound.x1*scale, y-bound.y0*scale),
                           'xref': xref, 'name': f'gid{item[1]}', 'origin': (x, y),
                           'size': scale, 'text': char.get('semantic_text', char['text']),
                           'color': span['color'], 'glyph_id': item[1],
                           'source_code': codes[0].to_bytes(native['bytes'], 'big').hex()})
            continue
        if subtype in {'/TrueType', '/Type0'}:
            # 资源级 trace 已核对实际 GID；轮廓和 hinting 使用原字体流，
            # 不要求源字体保留 cmap，也不通过 Unicode 重新选择字体。
            key = ('truetype', xref)
            if key not in cache:
                _, extension, _, data = page.parent.extract_font(xref)
                cache[key] = TTFont(BytesIO(data)) if extension == 'ttf' else None
            table = cache[key]
            if table is None or 'glyf' not in table or item[1] >= len(table.getGlyphOrder()):
                return None
            descriptor = _font_descriptor(page.parent, xref)
            if descriptor is None:
                return None
            name = table.getGlyphName(item[1])
            outlines = table.getGlyphSet()
            pen = BoundsPen(outlines)
            outlines[name].draw(pen)
            if pen.bounds is None and not include_empty:
                continue
            left, bottom, right, upper = pen.bounds or (0, 0, 0, 0)
            x, y = item[2]
            scale = span['size'] / table['head'].unitsPerEm
            glyphs.append({'rect': fitz.Rect(x+left*scale, y-upper*scale,
                                            x+right*scale, y-bottom*scale),
                           'xref': xref, 'name': name, 'origin': (x, y), 'size': span['size'],
                           'text': char.get('semantic_text', char['text']), 'color': span['color'],
                           'glyph_id': item[1], 'truetype': True, 'empty': pen.bounds is None,
                           'descriptor': descriptor,
                           'advance': table['hmtx'][name][0] * 1000 / table['head'].unitsPerEm})
            continue
        if xref not in cache:
            _, extension, _, data = page.parent.extract_font(xref)
            cache[xref] = None
            if extension == 'cff':
                cff = CFFFontSet()
                cff.decompile(BytesIO(data), None)
                matrix, outlines = cff[0].FontMatrix, cff[0].CharStrings
                names = cff[0].charset
            elif extension == 'pfa':
                from fontTools.t1Lib import T1Font
                # T1Font 的公共构造函数只接受路径；直接解析已提取的原字体
                # 字节，避免临时文件，也不转换、重编码交付 PDF 的字体程序。
                font = T1Font.__new__(T1Font)
                font.data, font.encoding = data, 'ascii'
                font.parse()
                matrix, outlines = font['FontMatrix'], font['CharStrings']
                native = fitz.Font(fontbuffer=data)
                # Type 1 的 CharStrings 字典次序不等于 FreeType GID 次序。
                # 必须从同一个原程序查询每个真实 GID 的名字，不能按 Unicode
                # 或 Python 字典顺序猜字形；之后仍由落盘 verify_inline 核验。
                names = [fitz.mupdf.fz_get_glyph_name2(native.this, gid)
                         for gid in range(native.glyph_count)]
            else:
                matrix = None
            # 例如 Wingdings 使用约 1/2048 em，不能硬编码 1/1000。
            # 仅接受无旋转、无平移的正向等比矩阵；其他变换保留明确失败。
            if (matrix and len(matrix) == 6 and matrix[0] > 0
                    and matrix[0] == matrix[3] and all(matrix[i] == 0 for i in (1, 2, 4, 5))):
                cache[xref] = (names, outlines, matrix[0], extension)
        parsed = cache[xref]
        if parsed is None or item[1] >= len(parsed[0]):
            return None
        names, outlines, em_scale, _ = parsed
        name = names[item[1]]
        if not name or name == '.notdef' or name not in outlines:
            return None
        glyph = outlines[name]
        pen = BoundsPen(outlines)
        glyph.draw(pen)
        if pen.bounds is None and not include_empty:
            continue
        left, bottom, right, upper = pen.bounds or (0, 0, 0, 0)
        x, y = item[2]
        scale = span['size'] * em_scale
        glyphs.append({'rect': fitz.Rect(x + left * scale, y - upper * scale,
                                        x + right * scale, y - bottom * scale),
                       'xref': xref, 'name': name, 'origin': (x, y), 'size': span['size'],
                       'text': char.get('semantic_text', char['text']), 'color': span['color'], 'glyph_id': item[1], 'empty': pen.bounds is None})
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
    with open_source_pdf(source) as original, open_source_pdf(source) as scratch:
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
            # 与实际行内重排共用原字体写入路径，保证 CFF/TrueType
            # 的掩模与交付字形一致；掩模仅读取 alpha，不受原颜色影响。
            from .paragraph_layout import write_inline
            write_inline(page, {'shift': [0, 0], 'inline_anchor':
                                {'glyphs': list(glyphs.values())}}, font_refs)
            page = scratch.reload_page(page)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
            masks[number] = Image.frombytes('RGBA', (pix.width, pix.height), pix.samples).getchannel('A')
    return masks, supported


def _remove_placeholder_actualtext(document):
    """移除出版社误用的辅助替代文本，仅作用于内存副本。

    Springer 把真实公式的 /ActualText 写成 inline-eq-IEq1 等内部 ID，
    或把空白写成零宽空间，rawdict 因此丢失真实字符坐标。只移除这两种
    明确占位内容；保留其他辅助文本以及所有原绘制操作、字体与图形。
    """
    import re
    import pymupdf as fitz
    from pypdf import PdfReader
    from pypdf.generic import ContentStream, DecodedStreamObject, IndirectObject
    def placeholder(value):
        # 排版工具还会在标准拉丁合字的每个字母之间插入 U+200B，
        # rawdict 随后把这些辅助字符分摊到一个真实字形的零宽续字上。
        # 只识别空白及标准合字的这种形式；其他有意义的 ActualText 保留。
        import unicodedata
        ligatures = {unicodedata.normalize('NFKD', chr(c)) for c in range(0xFB00, 0xFB07)}
        return (bool(re.fullmatch(r'inline-eq-IEq\d+', value)) or
                ('\u200b' in value and value.replace('\u200b', '') in {'', *ligatures}))
    # /Size 允许包含空闲或未分配编号，xref_get_key 不能读取这些槽位。
    # 仅遍历 MuPDF 标记为普通对象(n)或压缩对象(o)的实际对象；损坏的
    # 有效对象仍会报错，不能以广泛捕获异常掩盖源文件结构错误。
    pdf = fitz.mupdf.pdf_document_from_fz_document(document.this)
    objects = [r for r in range(1, document.xref_length())
               if fitz.mupdf.ll_pdf_get_xref_entry(pdf.m_internal, r).type in {'n', 'o'}]
    forms = [r for r in objects
             if document.xref_get_key(r, 'Subtype')[1] == '/Form']
    # 同一错误也会出现在结构树的 /StructElem，而不在内容流 BDC。
    # 先清理这类已明确识别的替代 ID，再让 MuPDF 读取真实字形。
    for ref in objects:
        kind, value = document.xref_get_key(ref, 'ActualText')
        if kind == 'string' and placeholder(value):
            document.xref_set_key(ref, 'ActualText', 'null')
    reader = None
    # 页面 Contents 数组共同组成一个程序；TJ 操作数和操作符可以在不同
    # 流里。必须整体解析，否则逐流解析会丢掉末尾尚未遇到操作符的文字。
    # Form 自有完整程序。新页面流单独分配，避免改写被其他页共用的流。
    programs = [(page, b'\n'.join(document.xref_stream(r) for r in page.get_contents()))
                for page in document]
    programs.extend((ref, document.xref_stream(ref)) for ref in forms)
    for owner, data in programs:
        if not data or b'/ActualText' not in data:
            continue
        if reader is None:
            reader = PdfReader(BytesIO(document.tobytes()))
        raw = DecodedStreamObject(); raw.set_data(data)
        stream = ContentStream(raw, reader)
        changed = False
        for args, op in stream.operations:
            if op != b'BDC' or len(args) != 2:
                continue
            props = args[1].get_object()
            if not isinstance(props, dict) or '/ActualText' not in props:
                continue
            value = str(props['/ActualText'])
            if not placeholder(value):
                continue
            if isinstance(args[1], IndirectObject):
                document.xref_set_key(args[1].idnum, 'ActualText', 'null')
            else:
                del props['/ActualText']
                changed = True
        if changed:
            if isinstance(owner, int):
                ref = owner
            else:
                ref = document.get_new_xref()
                document.update_object(ref, '<<>>')
            document.update_stream(ref, stream.get_data())
            if not isinstance(owner, int):
                owner.set_contents(ref)


def open_source_pdf(source):
    """内存中消除页面 /Rotate，保持可见画面、链接和注释位置。

    检测器坐标来自最终可见页；rawdict 默认却使用未旋转坐标。统一到
    可见坐标后，横置表格及其图注走同一条水平排版路径。源文件不写回。
    """
    import pymupdf as fitz
    document = fitz.open(source)
    _remove_placeholder_actualtext(document)
    for page in document:
        if page.rotation:
            # 1.25.2 的 remove_rotation 会再次旋转 get_links 已返回的
            # 可见坐标，导致链接漂移。暂时分离原注释，完成页面变换后
            # 原位更新 Rect，保留 xref、目标及其他属性，避免重建链接。
            annotations = list(page.annots() or [])
            if annotations or list(page.widgets() or []):
                document.close()
                raise ValueError('旋转页包含非链接注释，尚不能证明其坐标可安全转换')
            links = page.get_links()
            annots = document.xref_get_key(page.xref, 'Annots')[1]
            document.xref_set_key(page.xref, 'Annots', 'null')
            page.remove_rotation()
            document.xref_set_key(page.xref, 'Annots', annots)
            for link in links:
                rect = link['from'] * ~page.transformation_matrix
                document.xref_set_key(link['xref'], 'Rect', '['+' '.join(map(str, rect))+']')
    return document


def verify_canonical_source_pixels(source):
    """模型调用前证明坐标/辅助标签归一化没有改变原页可见内容。

    逐页以 144 dpi 比较 RGB 像素，内存仅保留当前页；不以文字抽取成功
    替代此检查，否则丢行可能同时被计划和删除验证当作正常输入接受。
    这是源页处理门禁，不代表译文最终像素已经验收。
    """
    import pymupdf as fitz
    with fitz.open(source) as before, open_source_pdf(source) as after:
        for index in range(len(before)):
            a = before[index].get_pixmap(matrix=fitz.Matrix(2, 2))
            b = after[index].get_pixmap(matrix=fitz.Matrix(2, 2))
            if (a.width, a.height, a.samples) != (b.width, b.height, b.samples):
                raise ValueError(f'第{index + 1}页源页归一化改变了原文像素；已在翻译前停止')


def open_source_for_editing(source, *, normalize_descriptors=True, normalize_rotation=True):
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

    document = open_source_pdf(source) if normalize_rotation else fitz.open(source)
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
            with open_source_pdf(source) as original:
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
