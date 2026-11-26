"""保留嵌入字体中共享字形和 Unicode 补充平面字符的检索身份。

Source Han Serif 的 U+2012（数字横线）与 U+2013（连接号）共用字形，
而 MuPDF 默认按字形编号生成 ToUnicode，每个编号只对应一个字符。默认
映射也可能把补充平面码点写成无效的十六进制串，例如数学斜体 Ψ
（U+1D6F9）。修复默认映射，并为这些字符单独声明 UTF-16BE 检索映射，
共享原字体程序、字宽和轮廓。
"""
from contextlib import ExitStack
from io import BytesIO
from itertools import groupby
import re
import unicodedata

import pymupdf as fitz

from fontTools.misc.roundTools import otRound
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont


def supplement_default_font(primary, text: str, *, bold: bool = False) -> bytes | None:
    """为默认中文 TrueType 补入本次缺少的拉丁和数学字形，保留其余字宽与轮廓。"""
    missing = [char for char in sorted(set(text))
               if not char.isspace() and not primary.has_glyph(ord(char), fallback=False)]
    if not missing:
        return None
    with ExitStack() as stack:
        font = stack.enter_context(TTFont(BytesIO(primary.buffer), recalcTimestamp=False))
        order = font.getGlyphOrder().copy()
        fallbacks = {}
        unsupported = []
        for char in missing:
            point = ord(char)
            if unicodedata.name(char, '').startswith('LATIN '):
                family = 'latin'
                if family not in fallbacks:
                    from babeldoc.assets.assets import get_font_and_metadata
                    weight = 'Bold' if bold else 'Regular'
                    try:
                        path, _ = get_font_and_metadata(f'NotoSerif-{weight}.ttf')
                    except SystemExit as error:
                        raise RuntimeError('自动获取拉丁补充字体失败，等待资源恢复') from error
                    fallbacks[family] = stack.enter_context(TTFont(path))
            elif 0x1D400 <= point <= 0x1D7FF:
                family = 'math'
                if family not in fallbacks:
                    # MuPDF 自带数学字体。按与 Font.buffer 相同的方式保留引用，
                    # 复制字体程序后再用 cmap 核对码点，不以其他相似字形代替。
                    gid, face = fitz.mupdf.fz_encode_character_with_fallback(primary.this, point, 0, 0)
                    if not gid:
                        unsupported.append(char)
                        continue
                    buffer = fitz.mupdf.FzBuffer(fitz.mupdf.ll_fz_keep_buffer(face.m_internal.buffer))
                    data = fitz.mupdf.fz_buffer_extract_copy(buffer)
                    fallbacks[family] = stack.enter_context(TTFont(BytesIO(data)))
            else:
                unsupported.append(char)
                continue
            fallback = fallbacks[family]
            source_name = fallback.getBestCmap().get(point)
            if not source_name:
                unsupported.append(char)
                continue
            glyphs = fallback.getGlyphSet()
            outline = DecomposingRecordingPen(glyphs)
            glyphs[source_name].draw(outline)
            # CFF 三次曲线按目标字体单位转为 TrueType 二次曲线，误差最多
            # 半个字体单位。先展开复合字形，避免引用源字体中的其他字形编号。
            scale = font['head'].unitsPerEm / fallback['head'].unitsPerEm
            pen = TTGlyphPen(None)
            converter = Cu2QuPen(pen, max_err=.5, reverse_direction='CFF ' in fallback)
            outline.replay(TransformPen(converter, (scale, 0, 0, scale, 0, 0)))
            name = f'PLFallback{point:X}'
            order.append(name)
            font['glyf'][name] = pen.glyph()
            advance, bearing = fallback['hmtx'][source_name]
            font['hmtx'].metrics[name] = (otRound(advance*scale), otRound(bearing*scale))
            if 'vmtx' in font:
                # 段落输出使用横排。保留完整纵排字宽表以生成有效字体。
                font['vmtx'].metrics[name] = font['vmtx'].metrics['.notdef']
            for cmap in font['cmap'].tables:
                if cmap.isUnicode() and (cmap.format == 12 or cmap.format == 4 and point <= 0xFFFF):
                    cmap.cmap[point] = name
        if unsupported:
            raise ValueError(f'font-file 缺少 replacement 所需字形：{"".join(unsupported[:12])!r}')
        font.setGlyphOrder(order)
        output = BytesIO()
        font.save(output, reorderTables=True)
        return output.getvalue()


def unicode_glyph_aliases(font_bytes: bytes) -> dict[str, int]:
    """查找需要单独声明检索映射的字体字形及编号。"""
    with TTFont(BytesIO(font_bytes)) as font:
        groups: dict[str, list[int]] = {}
        for codepoint, name in (font.getBestCmap() or {}).items():
            groups.setdefault(name, []).append(codepoint)
        return {chr(codepoint): font.getGlyphID(name)
                for name, codepoints in groups.items()
                for codepoint in codepoints if len(codepoints) > 1 or codepoint > 0xFFFF}


def repair_default_tounicode(document, font_xref: int, aliases: dict[str, int]) -> None:
    """纠正 MuPDF 新建字体的非 BMP 映射，保持原 CMap 中其余记录。"""
    supplementary = {ord(char): gid for char, gid in aliases.items() if ord(char) > 0xFFFF}
    if not supplementary:
        return
    kind, value = document.xref_get_key(font_xref, 'ToUnicode')
    if kind != 'xref':
        return
    cmap = int(value.split()[0])
    original = document.xref_stream(cmap)

    def replace(match):
        first = int(match[1], 16)
        last = int(match[2], 16) if match[2] else first
        start = int(match[3], 16)
        if start + last - first <= 0xFFFF:
            return match[0]
        points = range(start, start + last - first + 1)
        # 只改已由当前字体 cmap 核对的映射。连续码点可跨越代理对边界，
        # 因此范围逐项写出 UTF-16BE，避免把代理对当作整数递增。
        if any(point > 0xFFFF and supplementary.get(point) != gid
               for gid, point in enumerate(points, first)):
            return match[0]
        values = ' '.join(f'<{chr(point).encode("utf-16-be", "surrogatepass").hex()}>'
                          for point in points)
        if match[2]:
            return f'<{first:04x}> <{last:04x}> [{values}]'.encode('ascii')
        return f'<{first:04x}> {values}'.encode('ascii')

    corrected = re.sub(rb'^<([\da-f]{4})>(?: <([\da-f]{4})>)? <([\da-f]{4,6})>$',
                       replace, original, flags=re.MULTILINE | re.IGNORECASE)
    if corrected != original:
        document.update_stream(cmap, corrected)


def _bind_font(page, alias: str, reference: int) -> None:
    """把共享字体字典绑定到本页，兼容 Resources/Font 的直接或间接字典。"""
    document = page.parent
    owner, prefix = page.xref, ''
    for component in ('Resources', 'Font'):
        key = prefix + component
        kind, value = document.xref_get_key(owner, key)
        if kind == 'xref':
            owner, prefix = int(value.split()[0]), ''
        else:
            if kind == 'null':
                document.xref_set_key(owner, key, '<<>>')
            prefix = key + '/'
    document.xref_set_key(owner, prefix + alias, f'{reference} 0 R')


def insert_text_exact(page, origin, text: str, *, fontname: str, font_xref: int,
                      font, fontsize: float, aliases: dict[str, int],
                      font_refs: dict) -> None:
    """按原字宽连续写入，用独立 ToUnicode 保留需显式映射的字符。

    输入字体必须已经用同一份 font buffer 嵌入本页；font_refs 仅在当前输出
    文档内复用，不能跨 PDF 使用 xref。后续仍逐行严格回读，不把相似字符
    当成相等，也不使用 ActualText 掩盖缺字或错误轮廓。
    """
    document = page.parent
    x, y = origin
    for character, group in groupby(text, key=lambda c: c if c in aliases else None):
        run = ''.join(group)
        selected = fontname
        if character is not None:
            key = font_xref, character
            if key not in font_refs:
                # Identity-H 的字符码即 GID。克隆顶层字典以保持原墨迹和进宽；
                # 补充平面字符在 ToUnicode 中需要完整的 UTF-16BE 代理对。
                cmap = document.get_new_xref()
                document.update_object(cmap, '<<>>')
                code = f'{aliases[character]:04x}'
                value = character.encode('utf-16-be').hex()
                document.update_stream(cmap, (
                    '/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n'
                    '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
                    '/CMapName /PLUnicode def /CMapType 2 def\n'
                    '1 begincodespacerange <0000> <ffff> endcodespacerange\n'
                    f'1 beginbfchar <{code}> <{value}> endbfchar\n'
                    'endcmap CMapName currentdict /CMap defineresource pop end end').encode())
                reference = document.get_new_xref()
                document.update_object(reference, document.xref_object(font_xref))
                document.xref_set_key(reference, 'ToUnicode', f'{cmap} 0 R')
                font_refs[key] = reference
            reference = font_refs[key]
            selected = f'PLUnicode{reference}'
            _bind_font(page, selected, reference)
        page.insert_text((x, y), run, fontname=selected, fontsize=fontsize)
        x += font.text_length(run, fontsize=fontsize)
