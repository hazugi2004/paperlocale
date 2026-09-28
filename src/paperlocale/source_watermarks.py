"""保留出版社明确标记的背景水印，不把跨整页字框当成正文障碍。

仅处理绘制流开头、/Artifact + /Subtype /Watermark 的独立背景层。
未知标记、前景水印和其他旋转文字仍交给原有诊断；不按浅灰颜色猜测。
"""
from io import BytesIO
import hashlib
import pymupdf as fitz
from pypdf import PdfReader
from pypdf.generic import ContentStream


def background_layers(document):
    """拆分已声明的背景水印与正文；输出源绘制指令及逐字符证据。"""
    if hasattr(document, '_paperlocale_watermarks'):
        return document._paperlocale_watermarks
    layers = {}
    # 无 Artifact 的常规论文不进入 PDF 操作符解析路径。
    candidates = [p.number for p in document if any(b'/Artifact' in document.xref_stream(r)
                                                   for r in p.get_contents())]
    if not candidates:
        document._paperlocale_watermarks = layers
        return layers
    reader = PdfReader(BytesIO(document.tobytes()))
    for number in candidates:
        source = reader.pages[number]
        properties = source['/Resources'].get('/Properties', {})
        operations = source.get_contents().operations
        start = next((i for i, (_, op) in enumerate(operations) if op not in {b'q', b'cm'}), None)
        if start is None:
            continue
        args, op = operations[start]
        if op != b'BDC' or len(args) != 2 or args[0] != '/Artifact':
            continue
        prop = properties.get(args[1]) if isinstance(args[1], str) else args[1]
        if prop is None or prop.get_object().get('/Subtype') != '/Watermark':
            continue
        depth = 1
        end = start + 1
        while end < len(operations) and depth:
            op = operations[end][1]
            depth += (op in {b'BDC', b'BMC'}) - (op == b'EMC')
            end += 1
        if depth:
            continue
        # 部分出版社在 EMC 后才关闭水印的 q；连同其对应的 Q 一起拆分。
        graphics_depth = sum((op == b'q') - (op == b'Q') for _, op in operations[start:end])
        while graphics_depth > 0 and end < len(operations) and operations[end][1] == b'Q':
            graphics_depth -= 1
            end += 1
        if graphics_depth or any(op in {b'Do', b'BI', b'INLINE IMAGE', b'S', b'f', b'f*', b'B', b'B*'}
                                 for _, op in operations[start:end]):
            continue
        def encode(ops):
            stream = ContentStream(None, reader)
            stream.operations = ops
            return stream.get_data()
        prefix = operations[:start]
        balance = sum((op == b'q') - (op == b'Q') for _, op in prefix)
        if balance < 0:
            continue
        background = encode(operations[:end] + [([], b'Q')] * balance)
        foreground = encode(prefix + operations[end:])
        # 读取隔离层的真实原字形，而不是用字符串搜索正文中的同名词。
        with fitz.open(stream=document.tobytes(), filetype='pdf') as probe:
            page = probe[number]
            ref = probe.get_new_xref(); probe.update_object(ref, '<<>>')
            probe.update_stream(ref, background)
            probe.xref_set_key(page.xref, 'Contents', f'{ref} 0 R')
            page = probe.reload_page(page)
            chars = [c for b in page.get_text('rawdict')['blocks'] for l in b.get('lines', [])
                     for s in l['spans'] for c in s['chars']]
            if not chars:
                continue
            layers[number+1] = {'background': background, 'foreground': foreground,
                'characters': [(c['c'], tuple(c['origin'])) for c in chars],
                'text': ''.join(c['c'] for c in chars),
                'sha256': hashlib.sha256(background).hexdigest()}
    document._paperlocale_watermarks = layers
    return layers


def set_page_stream(page, data):
    document = page.parent
    ref = document.get_new_xref(); document.update_object(ref, '<<>>')
    document.update_stream(ref, data)
    document.xref_set_key(page.xref, 'Contents', f'{ref} 0 R')


def restore_background(page, source_page, layer):
    """正文删除后，将原水印指令封装为背景 Form；原字体/颜色/裁剪不改。"""
    doc = page.parent
    ref = doc.get_new_xref()
    resources = source_page.parent.xref_get_key(source_page.xref, 'Resources')[1]
    doc.update_object(ref, f'<< /Type /XObject /Subtype /Form /BBox [0 0 {page.rect.width} '
                          f'{page.rect.height}] /Resources {resources} >>')
    doc.update_stream(ref, layer['background'])
    doc.xref_set_key(page.xref, 'PLWatermark', f'{ref} 0 R')
    # 本页资源字典独立拷贝，避免加别名时改写背景 Form 所沿用的原资源。
    kind, value = doc.xref_get_key(page.xref, 'Resources')
    current = doc.xref_object(int(value.split()[0])) if kind == 'xref' else value
    owner = doc.get_new_xref(); doc.update_object(owner, current)
    kind, value = doc.xref_get_key(owner, 'XObject')
    if kind == 'xref':
        copy = doc.get_new_xref(); doc.update_object(copy, doc.xref_object(int(value.split()[0])))
        doc.xref_set_key(owner, 'XObject', f'{copy} 0 R')
        doc.xref_set_key(copy, 'PLWatermark', f'{ref} 0 R')
    elif kind == 'null':
        doc.xref_set_key(owner, 'XObject', '<<>>')
    if kind != 'xref':
        doc.xref_set_key(owner, 'XObject/PLWatermark', f'{ref} 0 R')
    doc.xref_set_key(page.xref, 'Resources', f'{owner} 0 R')
    content = doc.get_new_xref(); doc.update_object(content, '<<>>')
    doc.update_stream(content, b'q /PLWatermark Do Q')
    refs = [content] + page.get_contents()
    doc.xref_set_key(page.xref, 'Contents', '['+' '.join(f'{r} 0 R' for r in refs)+']')


def verify_backgrounds(original, written):
    """独立核验原水印全层像素及背景顺序；正文重排不影响此项检查。"""
    layers = background_layers(original)
    evidence = []
    for number, layer in layers.items():
        page = written[number-1]
        kind, value = written.xref_get_key(page.xref, 'PLWatermark')
        if kind != 'xref':
            raise ValueError(f'第{number}页原背景水印缺失')
        ref = int(value.split()[0])
        if written.xref_stream(ref) != layer['background'] or written.xref_stream(page.get_contents()[0]) != b'q /PLWatermark Do Q':
            raise ValueError(f'第{number}页原水印指令或背景顺序改变')
        pixels = []
        for doc, stream in [(original, layer['background']), (written, b'q /PLWatermark Do Q')]:
            with fitz.open(stream=doc.tobytes(), filetype='pdf') as probe:
                p = probe[number-1]; set_page_stream(p, stream); p = probe.reload_page(p)
                pix = p.get_pixmap(matrix=fitz.Matrix(2,2), alpha=True)
                pixels.append((pix.width, pix.height, pix.samples))
        if pixels[0] != pixels[1]:
            raise ValueError(f'第{number}页原水印字形或颜色改变')
        evidence.append({'page': number, 'source_sha256': layer['sha256'], 'pixels_equal': True})
    return evidence
