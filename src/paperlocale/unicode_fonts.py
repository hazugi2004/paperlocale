"""保留中文嵌入字体中共享字形的不同 Unicode 字符身份。

Source Han Serif 的 U+2012（数字横线）与 U+2013（连接号）共用字形。
MuPDF 默认以字形编号作为 PDF 字符码，一个 ToUnicode 只能为该编号记录
一个字符，导致画面正确却无法严格回读。这里仅为这类字符另建字体字典，
共享原字体程序、字宽和轮廓，单独声明检索字符；不归一化横线或科学负号。
"""
from io import BytesIO
from itertools import groupby

from fontTools.ttLib import TTFont


def unicode_glyph_aliases(font_bytes: bytes) -> dict[str, int]:
    """返回字体 cmap 中多字符共用的 GID；唯一字形继续使用原写入路径。"""
    with TTFont(BytesIO(font_bytes)) as font:
        groups: dict[str, list[int]] = {}
        for codepoint, name in (font.getBestCmap() or {}).items():
            groups.setdefault(name, []).append(codepoint)
        return {chr(codepoint): font.getGlyphID(name)
                for name, codepoints in groups.items() if len(codepoints) > 1
                for codepoint in codepoints}


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
    """按原字宽连续写入，独立 ToUnicode 仅消除真实 cmap 多对一歧义。

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
                # Identity-H 的字符码即 GID。克隆顶层字典，不复制字体程序，
                # 因而同形的两种 Unicode 保持完全相同的真实墨迹和进宽。
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
