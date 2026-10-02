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
            # 已核验语义表只包含 CFF 的 1000 em 轮廓；新支持的 TrueType
            # 可原样重放，但不能套用 CFF 的轮廓摘要来改变科学字符含义。
            if glyph.get('truetype') or 'source_code' in glyph:
                continue
            _, outlines, em_scale, extension = cache[glyph['xref']]
            # 原字形重放支持更广的字体，不意味着它们适用这张语义映射表。
            # PFA 或不同单位的 CFF 仍只保留原字，不推断科学符号含义。
            if extension != 'cff' or em_scale != .001:
                continue
            key = ('semantic', glyph['xref'], glyph['name'])
            if key not in cache:
                pen = RecordingPen()
                outlines[glyph['name']].draw(pen)
                signature = hashlib.sha256(repr(pen.value).encode()).hexdigest()
                cache[key] = REVIEWED_OUTLINES.get(signature)
            semantic = cache[key]
            if semantic and semantic != char['c']:
                char['native_text'] = char['c']
                char['c'] = semantic


# 2026-09 语料核验：Li 2021 补充、Sutanto 2024、Ridder 2020、
# Wang 2025、Hao 2019、Feng 2026 等原页截图与字形逐项对照。
# 键同时约束字体类型、em 单位和完整轮廓，原 PDF 字形从不替换。
CORPUS_REVIEWED_OUTLINES = {
    # Science Advances 2017, DOI 10.1126/sciadv.1700263 p8: source glyph
    # visibly theta, but ToUnicode is q. Exact CFF outline, never font-name guessing.
    ('cff', 1000, 'fa52c7bfd1455f0d35985b2ed79329ed45c72e3349e71925053e726623bf699d'): 'θ',
    # ASCE Swain 2024 第7页原页与轮廓核验：AdvP4C4E74 的可打印
    # ToUnicode ð/Þ/¼ 实际绘制括号/等号。只接受下列完整轮廓摘要，
    # 不能把其他字体中的正常拉丁字母或四分之一替换成数学符号。
    ('cff', 1000, '6a57c4966188f4b99005c9ac50f8b2e323b5246e60ac26df88165339d2f5a2f3'): '(',
    ('cff', 1000, '42412fd504944455c49cb6069544ae3c7de998a3f5ecce2c64592660da201a45'): ')',
    ('cff', 1000, '12a0dcdcc6b95615a4479200f021cc840b986cee1d7186f669d5b64d9fb3f2ec'): '=',

    ('cff', 1000, '009bf73f6cd5e6a4b9fad7ea090f889190737b331baf18c1215701cb2860c3c7'): '∑',
    ('cff', 1000, '41020f8e445e7438a3fcdcd376fa89b3a307a80736e95a6a3bf9a69d8ca855d2'): '=',
    ('cff', 1000, 'e7e216f90c92f695051d79db83522daca5ecbcb0f36897fb95ee05d35023a3e2'): '(',
    ('cff', 1000, 'e0c7c4fed2f8b18698c6fc2888a3a0f7097a73d7aeee4b802c2fd766b51d7b22'): ')',
    ('cff', 1000, '5c08202b0d7e64fedc0af06e0b1c2f3f71a819ad94b78b10572f6a8843379648'): '⏐',
    ('cff', 1000, 'f6f639476518630a7078db0865b979b359942beaa1b1b1ac5067dd71bf783744'): '(',
    ('cff', 1000, '42ecb0a1e9684ca4c98309a3c472d3011f14e77cd5006a8c927be97e861ef95c'): ')',
    ('cff', 1000, '14f9d6c42f2bbdfb7bb0014ec7a406bb36bd4e397943eedf89599b69dbbda2d2'): '√',
    ('ttf', 2048, 'b1ccb11f49a958bfd98944b4cc18579b540533bcd0a1c425af5eef7e6cfe99d6'): '⎛',
    ('ttf', 2048, '2ec2468ef8672179c298fc6eba9bd431f718e88cc3b658bc003fabb07d0a11f9'): '⎞',
    ('ttf', 2048, '9957be9a68ea5c04d28918810e4feb6e20e06396b893c89352b09172864383dd'): '⎜',
    ('ttf', 2048, '17253fb78d61e85bdd90ed4b0535c5719b7e9045027cad12807c421e261aae2f'): '⎟',
    ('ttf', 2048, '0d0cd94e1e309ece8b2597f72fff7f9827c454ed7f060f2b0ae5c38f9e4b0ed3'): '⎝',
    ('ttf', 2048, '19bd8302a7f21dac2996fb85f4e5e0ece7c8659c8e09b54b82f55523dd7b62df'): '⎠',
    ('ttf', 2048, '6419051da0bf5b3908e408445069ed0098908b2dc0a4ec3fe81f6da650f552e3'): '∑',
    ('ttf', 2048, '4298b98d839e3c6d7e744a2bcbadc61e23b190e8a123e1381194974037c10bfe'): '⎡',
    ('ttf', 2048, 'df05d3fb628dcd2172883acc963d16aae0ec8d0784e0adc9537be994c0104c07'): '⎤',
    ('ttf', 2048, '88e36f98dbcf44c1fd69f3c0da30994da16246e69f02359a592bf94821bc69cd'): '⎢',
    ('ttf', 2048, 'c58985fa72a30c8fe981e5360b448287f78695bd79c78e1ee624ec5788f4fc16'): '⎥',
    ('ttf', 2048, 'de457db4d17f6c484e186749493d16b0d58cf40c9869267e4b21068ff3370b35'): '⎣',
    ('ttf', 2048, '8e48365095eef7da3567e0ef04f57a084a1676adb7c15a82802d07ba1da9dd27'): '⎦',
    ('ttf', 2048, 'ed79de8cb014c1a6fd949e5b97a88881182ef2c78ab6af0f5f191b755343e2a6'): 'β',
    ('ttf', 2048, '4bf27e22441afe2932ef67e238a1cf58668461e0e45c9acf256cf113511e2148'): 'γ',
    ('ttf', 2048, '1c8cdb7b60d2b86c4f58bb7fdc1a4a147efaa98150e18f6d7f7b92ff6ac76dac'): 'α',
    ('ttf', 2048, 'a94eaf6a451b87d431d932f76cc8e360e21baa0301927bf40fed4caa469f0aa1'): '>',
    ('ttf', 2048, '0eb6fe07ebe2a079bb6657096f9503385fda16d0a9999277d6ff00f222d9d548'): '<',
    ('ttf', 2048, 'd023bd7584da6cec2eef11a76ae1c560fa0b500cc2c1d0a0dd0579c9642129dd'): '∞',
    ('cff', 1000, '3bb477f20dc8ce49361a67459e398e05a6da58759397ea066bb8367b467374a8'): '(',
    ('cff', 1000, '9fde6a664c07ba740e8632bb9ef55448f8040d1f77e63d6351f01477fc7cfae6'): ')',
    ('cff', 1000, '26035219e9c7b476cd8fdd68c3497505e4c81f89236e2a37dbdf98f46ee84d9c'): '−',
    ('cff', 1000, '785495dc9159c51c7b09e44a5a8787547200682cc58f9cf89b4000505cd452c3'): '̅',
    ('cff', 1000, 'd9bbc6408610b4288811bb06409fc82eab6e2a46311b868cb000a8f56ba47598'): '⏐',
    ('cff', 1000, '0a58c765145750118973e057ca014926ee09262497d0496470e0330c32960ebe'): 'σ',
    ('cff', 1000, '2b9cdb166f67a965e643d35ba2fc1d66ec5ca5a9cc69243f32c9245edce6a950'): ']',
    ('cff', 1000, '3974bdd14d7fae1bf21b64af844f7abca6e491d2a6038606b082600524adf828'): '∑',
    ('cff', 1000, '7e0be4a242d0da54c925b985e7e8c6459497d50f1b934cfac2d216d29176220e'): '{',
    ('cff', 1000, '6244b2d90a2e420c1f89c280561e79bcbbd44d8ea62f834f8fba81f452a08455'): ']',
    ('cff', 1000, '2d4214f05e2ac0bb6aca6a3fc7b5de33ccd975e05b2626eb1b9043d64f45efac'): '−',
    ('cff', 1000, '8c8cb8c6b71b7e344ef7957b174a1f14992ab06de59a589d8d893670854e76a6'): 'Ω',
    ('ttf', 1000, 'b5d5d0a5bc1430a373c84546ee8c672ed2e05a4d75cc192fc0d9bc8a7ebf2566'): '≤',
    ('cff', 1000, '968f26ab2aa4d354431347746f782fbba3cdd92a5c3f369e719ea0f074b3b7f6'): '≤',
    ('cff', 1000, 'c8859aef9c8ab66440994d068c13083bb66bf072b4ad7d5b3405a323cfed2c05'): '©',
    ('cff', 1000, 'a2169bfd1845d87e4a9ceb5e7e2935025401a57f64c8049feca20813d331fdff'): '(',
    ('cff', 1000, '6694f85c5299da7f47987ebb03c7aec5736c21c5d7c6539a2b097b6caeaa1b8b'): ')',
    ('cff', 1000, '9fc24a0b25f2ab347d69709480c6a4bff2a35fb0c28cec0f6853db2ea1639a6f'): '∑',
    ('cff', 1000, 'a15038662f48fa87e0655be0587d3d8d490056dbcab6ee8794b663a960b3720a'): '(',
    ('cff', 1000, '84d8d3a1d0be8190947e5c1f465e8e6f3d60842bf3fb3bf7e59da33ca81db3ea'): ')',
    ('cff', 1000, 'cb38dd84c3b564de0a3f750ad3ca068a43f77c722216e97ecc7f7556844d6501'): '*',
}


def restore_verified_symbols(page, raw, cache, *, printable_math=False, latin_math=False):
    """新计划专用：已核验异常轮廓恢复语义；无墨迹字符恢复空白。

    不推断未知控制码，也不向模型传入乱码。空白要由实际原字形的空
    轮廓证明；非空字形的原字符、原坐标和程序仍用于后续删除与重放。
    """
    for block in raw:
        for line in block.get('lines', []):
            for span in line['spans']:
                for char in span['chars']:
                    if (char['c'].isprintable() and not (printable_math and char['c'] in 'ðÞ¼' or latin_math and char['c'] == 'q')
                            or char['c'].isspace() or char['c'] == '\u00ad'):
                        continue
                    native = {'text': char['c'], 'rect': char['bbox'], 'origin': char['origin']}
                    glyphs = _source_anchor_glyphs(page, [native], cache, include_empty=True)
                    if not glyphs or len(glyphs) != 1:
                        continue
                    glyph = glyphs[0]
                    if glyph.get('empty'):
                        # 单独留存原编码证据；不能把空白提升为固定公式锚点。
                        char['source_blank'] = char['c']
                        char['c'] = ' '
                        continue
                    if 'source_code' in glyph:
                        continue
                    if glyph.get('truetype'):
                        table = cache[('truetype', glyph['xref'])]
                        kind, units, outlines = 'ttf', table['head'].unitsPerEm, table.getGlyphSet()
                    else:
                        _, outlines, scale, kind = cache[glyph['xref']]
                        units = 1 / scale
                    key = ('corpus-semantic', glyph['xref'], glyph['name'])
                    if key not in cache:
                        pen = RecordingPen()
                        outlines[glyph['name']].draw(pen)
                        signature = hashlib.sha256(repr(pen.value).encode()).hexdigest()
                        cache[key] = CORPUS_REVIEWED_OUTLINES.get((kind, units, signature))
                    semantic = cache[key]
                    if semantic:
                        char['native_text'], char['c'] = char['c'], semantic
