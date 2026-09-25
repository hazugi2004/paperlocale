"""用已目视核验的 CFF 轮廓修复文本层，不按字体名或乱码字符猜测。

部分出版社把 minus 命名为 two，ToUnicode 也写成 2。轮廓摘要不依赖
子集字体名、字形编号或论文文件；只有绘制指令完全相同才恢复语义。
这不是通用 OCR：未知轮廓不替换，原始字符与坐标始终保留供删除/重放核验。
"""
import hashlib

from fontTools.pens.recordingPen import RecordingPen

from .font_geometry import _source_anchor_glyphs

# 来自已核验的出版社符号轮廓，含度、分角、比较、符号及乘号。
# 摘要对应标准 0.001 FontMatrix 下未缩放的 RecordingPen 指令序列。
REVIEWED_OUTLINES = {
    # Nature Communications 2026, DOI 10.1038/s41467-026-75130-5，第4页图2图注：
    # AdvMacMthSyN 的 01 -> C0 缺失 ToUnicode，rawdict 返回 U+0001。
    # 已核对完整概率差公式及源轮廓；仅精确轮廓匹配才恢复减号，不按控制码猜测。
    '26035219e9c7b476cd8fdd68c3497505e4c81f89236e2a37dbdf98f46ee84d9c': '−',
    'd0d47323e83727bb962f8383d9cdbbfa3cdde60d10a21533c23fd08935b5f596': '°',
    '5c24c11c5915d3aa2ee35890f86f6d47fef91e32b1029abd8a2aa489ac1867de': '′',
    '23fe3853f2e51be09c93eb4b683262f6414e864ffdcf03e7efd258f8ce1c6150': '<',
    '3013296ac23f327dfcf7d6141fb9384b79feeeb55a5663995eb665ae6ba2033c': '−',
    '289fde9e6ca31aeb3be347771f5f8a9cce8f0672c579a04c30616ebf2e787dc0': '≥',
    '5e94b86042fe5da503d7c6f34ebd7eaddf0c8679d7663d271b98b5fc0482d521': 'Δ',
    'b8429ad80ae9c8c316378c48e69d8a72cde8655489201fe77f9e2856088e800a': '>',
    '5e75abda713d7060a48ce00a8031b7ebb873c731549e80b0a1d70e881588f5bd': '=',
    'bc78ed0dd51ab859902fcb3bb7654b9b003de422970a308892d2c86e64da971c': '|',
    '53ac0545006225aa106a9a6842897cd0a26222782dc9a5ea110038b50a60a1d7': '≤',
    'da6a741932b36e8d0e84a85c93f5806fc221f60650f8c39eb357cb622de01716': '×',
}


def restore_source_symbols(page, raw, cache):
    """原地增加语义字符及原字符证据；保持数量、原点和字框完全不变。"""
    # 先按 span 绑定，复用已经严格校验的 CFF 字体/唯一绘制位置逻辑；
    # 未支持的字体不应阻止其普通文本读取，也不能套用其他字体的映射。
    by_font = {}
    for block in raw:
        for line in block.get('lines', []):
            for span in line['spans']:
                by_font.setdefault(span['font'], []).extend(
                    c for c in span['chars'] if c['c'] in '8023$D.5j#,\x01')
    for candidates in by_font.values():
        if not candidates:
            continue
        native = [{'text': c['c'], 'rect': c['bbox'], 'origin': c['origin']} for c in candidates]
        glyphs = _source_anchor_glyphs(page, native, cache)
        if not glyphs or len(glyphs) != len(candidates):
            continue
        for char, glyph in zip(candidates, glyphs):
            key = ('semantic', glyph['xref'], glyph['name'])
            if key not in cache:
                top = cache[glyph['xref']]
                pen = RecordingPen()
                top.CharStrings[glyph['name']].draw(pen)
                signature = hashlib.sha256(repr(pen.value).encode()).hexdigest()
                cache[key] = REVIEWED_OUTLINES.get(signature)
            semantic = cache[key]
            if semantic and semantic != char['c']:
                char['native_text'] = char['c']
                char['c'] = semantic
