"""自然段排版：物理框保存位置，逻辑段保证连续，行内原字形随正文流动。

不根据译文中的换行新增段落，不用空格撑满行，也不把引文锁在旧英文位置。
跨页/栏/图的段落仍只翻译一次；按原物理框容量分配连续的译文词元。
独立公式、图内文字、书目沿用源版面保护。未知原字体的行内字形明确报错。
"""
from __future__ import annotations

import math
import re
from statistics import median

import pymupdf as fitz

from .font_geometry import line_ink, _source_anchor_glyphs
from .source_layout import CAPTION, NO_LINE_START, NO_LINE_END, anchor_errors


def starts_paragraph(native_lines, preceding, line):
    """原生块可包含多段；用首行缩进或额外行距划分，忽略同行上标碎片。"""
    if not preceding:
        return False
    previous = preceding[-1]
    size = median(s['size'] for s in line['spans'] for c in s['chars'] if not c['c'].isspace())
    baseline = max(s['origin'][1] for s in line['spans'])
    previous_y = max(s['origin'][1] for s in previous['spans'])
    if baseline - previous_y < .6 * size:
        return False
    left = min(l['bbox'][0] for l in native_lines)
    indent = line['bbox'][0] - left
    widths = [l['bbox'][2] - l['bbox'][0] for l in native_lines]
    # 公式后的小片段也可能从右侧开始；段首缩进应小于四个正常字号。
    return (.75 * size < indent < 4 * size and max(widths) > 10 * size or
            baseline - previous_y > 1.65 * size)


def paragraph_groups(blocks):
    """正文与图注分别建立阅读链；缩进段首和标题是不可跨越的段落边界。"""
    groups = []
    for kind in ('body', 'caption'):
        previous = None
        for block in [b for b in blocks if b['kind'] == kind]:
            ordinary = [p for p in block['parts'] if not p['fixed']]
            if not ordinary:
                continue
            size = median(p['size'] for p in ordinary)
            indent = block['parts'][0]['rect'][0] - block['rect'][0]
            connect = False
            if previous:
                old = [p for p in previous['parts'] if not p['fixed']]
                a, b = fitz.Rect(previous['rect']), fitz.Rect(block['rect'])
                style = (abs(median(p['size'] for p in old) - size) < .6 and
                         is_bold(old) == is_bold(ordinary))
                new_frame = block['page'] != previous['page'] or b.y0 < a.y0 or b.y0 - a.y1 > 2 * size
                full_end = old[-1]['rect'][2] > a.x1 - 2 * size
                incomplete = not re.search(r'[.!?:;][\s\d)\]]*$', previous['text'])
                connect = (style and block['page'] <= previous['page'] + 2 and indent < .7 * size and
                           (len(previous['text']) >= 25 or not new_frame) and
                           (incomplete or new_frame and full_end))
                if kind == 'caption':
                    connect = (style and block['page'] == previous['page'] and
                               not CAPTION.match(block['text']))
            if connect:
                groups[-1].append(block['id'])
            else:
                groups.append([block['id']])
            previous = block
    return groups


def attach_frames(units, plan):
    """每个原段落片段保留一个框；同一栏连续的原生块合并，不跨图填空。"""
    blocks = {b['id']: b for b in plan['blocks']}
    for unit, ids in zip(units, plan['groups']):
        frames = []
        for sid in ids:
            b = blocks[sid]
            parts = b['parts']
            ordinary = [p for p in parts if not p['fixed']]
            size = median(p['size'] for p in ordinary)
            rect = fitz.Rect(b['rect'])
            ys = sorted(set(round(p['baseline'], 2) for p in ordinary))
            gaps = [y-x for x,y in zip(ys, ys[1:]) if .8*size < y-x < 1.7*size]
            frame = {'page': b['page'], 'rect': list(rect), 'size': size,
                     'bold': is_bold(ordinary),
                     'leading': median(gaps) if gaps else size * 1.2,
                     'indent': max(0, parts[0]['rect'][0] - rect.x0),
                     'weight': len(b['text']), 'source_block': sid}
            last = frames[-1] if frames else None
            if (last and last['page'] == frame['page'] and
                    abs(last['rect'][0] - rect.x0) < 1 and abs(last['rect'][2] - rect.x1) < 2 and
                    -.5*size <= rect.y0-last['rect'][3] < size and
                    frame['indent'] < .7*size):
                last['rect'] = list(fitz.Rect(last['rect']) | rect)
                last['weight'] += frame['weight']
            else:
                frames.append(frame)
        unit['frames'] = frames
        unit['kind'] = blocks[ids[0]]['kind']


def bind_inline_glyphs(source, units):
    """翻译前绑定原嵌入字体字形，拒绝无证据的数学字体替换。

    CFF 按原字形名重放轮廓、颜色和上下标偏移；标准 Base14 字体使用原字体。
    不把数学字符交给中文字体，不把行内公式栅格化。
    """
    cache = {}
    with fitz.open(source) as document:
        for unit in units:
            for anchor in unit['anchors']:
                page = document[anchor['page'] - 1]
                glyphs = _source_anchor_glyphs(page, anchor['chars'], cache)
                if not glyphs:
                    # 标准 PDF 字体用于普通出版社与自有回归样例；其 Unicode
                    # 映射必须可打印且每个位置唯一，不能处理未知嵌入字体。
                    glyphs = []
                    for char in anchor['chars']:
                        if not char['text'].strip():
                            continue
                        matches = [(s,c) for s in page.get_texttrace() for c in s['chars']
                                   if c[0] == ord(char['text']) and
                                   abs(c[2][0]-char['origin'][0]) < .001 and abs(c[2][1]-char['origin'][1]) < .001]
                        if len(matches) != 1 or matches[0][0]['font'].lower() not in fitz.Base14_fontdict:
                            raise ValueError(f"第{anchor['page']}页行内字形尚不支持原字体重排：{anchor['text']!r}")
                        span, charinfo = matches[0]
                        glyphs.append({'base14': span['font'], 'text': char['text'],
                                       'origin': char['origin'], 'size': span['size'],
                                       'color': span['color'], 'rect': list(charinfo[3])})
                for g in glyphs:
                    g['rect'] = list(g['rect'])
                ink = fitz.Rect(glyphs[0]['rect'])
                for g in glyphs[1:]:
                    ink |= fitz.Rect(g['rect'])
                anchor['glyphs'] = glyphs
                anchor['ink'] = list(ink)
                peers = [p for slot in unit['slots'] for p in slot if p['page'] == anchor['page']]
                # 用最近正文基线保留上标/下标的相对高度；上标本身的基线不是正文基线。
                anchor['body_baseline'] = min(peers, key=lambda p: abs(p['baseline']-anchor['baseline']))['baseline'] if peers else anchor['baseline']


def compact_target(text):
    """仅清除中文排版的无语义空白；英文词组及数值与单位之间仍可有空格。"""
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'(?<=[\u3400-\u9fff，。；：！？（）]) +| +(?=[\u3400-\u9fff，。；：！？（）])', '', text)


def fit_paragraph(unit, target, font, min_size, bold_font):
    """在完整段落框中逐词元顺序排字；不分散行、不右推引文前的文字。

    单段最多收缩至原字号的 80%，保持全部内容；显式 min_size 优先。
    达到下限仍溢出则抛错，不截断、不自动生成缺字 PDF。
    """
    errors = anchor_errors(unit, target)
    if errors:
        raise ValueError('; '.join(errors))
    tokens = re.findall(r'\{v\d+\}|[A-Za-z0-9]+(?:[.−–/-][A-Za-z0-9]+)*|.', compact_target(target))
    size = median(f['size'] for f in unit['frames'])
    floor = size*.8 if min_size is None else min_size
    if not 0 < floor <= size:
        raise ValueError('最小字号必须为正数且不大于原字号')
    for step in range(math.ceil((size-floor)*10)+1):
        current = max(floor, size-step/10)
        remaining, placed = list(tokens), []
        for fi, frame in enumerate(unit['frames']):
            rect = fitz.Rect(frame['rect'])
            face = bold_font if frame['bold'] and bold_font is not None else font
            leading = max(current*1.13, frame['leading']*current/size)
            # 跨物理边界保留两侧的内容；分配完整连续词元，不另起逻辑段落。
            share = frame['weight']/sum(f['weight'] for f in unit['frames'][fi:])
            limit = len(remaining) if fi == len(unit['frames'])-1 else max(1, round(len(remaining)*share))
            while limit < len(remaining) and (remaining[limit][0] in NO_LINE_START or remaining[limit-1][-1] in NO_LINE_END):
                limit += 1
            portion = remaining[:limit]
            cursor = 0
            baseline = rect.y0 + current*.88
            row = 0
            while cursor < len(portion) and baseline <= rect.y1+.01:
                x = rect.x0 + (min(frame['indent'], 2*current) if row == 0 and fi == 0 else 0)
                line_items = []
                while cursor < len(portion):
                    token = portion[cursor]
                    match = re.fullmatch(r'\{v(\d+)\}', token)
                    anchor = unit['anchors'][int(match[1])] if match else None
                    if anchor:
                        ink = fitz.Rect(anchor['ink'])
                        width = ink.width + .5
                        top, bottom = anchor['body_baseline']-ink.y0, anchor['body_baseline']-ink.y1
                    else:
                        width = face.text_length(token, fontsize=current)
                        _, bottom, _, top = line_ink(face, token, current) if token.strip() else (0,0,0,0)
                    if x+width > rect.x1+.001 or baseline-bottom > rect.y1+.001:
                        break
                    if baseline-top < rect.y0-.001:
                        delta = rect.y0+top-baseline
                        baseline += delta
                        for prior in line_items:
                            prior['baseline'] += delta
                            prior['rect'][1] += delta
                            prior['rect'][3] += delta
                            if 'shift' in prior:
                                prior['shift'][1] += delta
                        if baseline-bottom > rect.y1+.001:
                            break
                    if token == ' ' and not line_items:
                        cursor += 1
                        continue
                    item = {'page': frame['page'], 'rect': [x, baseline-top, x+width, baseline-bottom],
                            'font_size': current, 'baseline': baseline, 'origin_x': x,
                            'bold': frame['bold'], 'target': token, 'paragraph_id': unit['id']}
                    if anchor:
                        item['inline_anchor'] = anchor
                        item['shift'] = [x-ink.x0+.25, baseline-anchor['body_baseline']]
                    line_items.append(item)
                    x += width
                    cursor += 1
                # 避头尾规则只挪动最后词元，不注入空格或丢弃标点。
                while line_items and cursor < len(portion) and (
                        portion[cursor][0] in NO_LINE_START or line_items[-1]['target'][-1] in NO_LINE_END):
                    cursor -= 1
                    line_items.pop()
                if not line_items:
                    break
                placed.extend(line_items)
                baseline += leading
                row += 1
            if cursor < len(portion):
                break
            del remaining[:limit]
        if not remaining:
            return merge_text_runs(placed)
    raise ValueError(f"完整译文无法放入段落框：{unit['id'][:12]}，剩余 {len(remaining)} 个词元")


def write_inline(page, item, font_refs):
    """以原 CFF 程序重放字形，坐标只作平移；ToUnicode 保留可检索字符。"""
    document = page.parent
    commands = []
    dx, dy = item['shift']
    for g in item['inline_anchor']['glyphs']:
        x, y = g['origin'][0]+dx, g['origin'][1]+dy
        color = tuple(g['color'])
        if 'base14' in g:
            page.insert_text((x,y), g['text'], fontsize=g['size'], fontname=g['base14'].lower(), color=color)
            continue
        key = (g['xref'], g['name'], g['text'])
        if key not in font_refs:
            original = g['xref']
            base = document.xref_get_key(original, 'BaseFont')[1]
            descriptor = document.xref_get_key(original, 'FontDescriptor')[1]
            name = ''.join(chr(c) if 33 <= c <= 126 and chr(c) not in '#%()/<>[]{}' else f'#{c:02X}' for c in g['name'].encode())
            cmap = document.get_new_xref()
            document.update_object(cmap, '<<>>')
            unicode = g['text'].encode('utf-16-be').hex()
            document.update_stream(cmap, ('/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n'
                '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
                '/CMapName /PLInline def /CMapType 2 def\n1 begincodespacerange <00> <ff> endcodespacerange\n'
                f'1 beginbfchar <00> <{unicode}> endbfchar\nendcmap CMapName currentdict /CMap defineresource pop end end').encode())
            ref = document.get_new_xref()
            document.update_object(ref, f'<< /Type /Font /Subtype /Type1 /BaseFont {base} '
                f'/FontDescriptor {descriptor} /FirstChar 0 /LastChar 0 /Widths [0] '
                f'/Encoding << /Type /Encoding /Differences [0 /{name}] >> /ToUnicode {cmap} 0 R >>')
            font_refs[key] = ref
        ref = font_refs[key]
        alias = f'PLInline{ref}'
        owner, prefix = page.xref, ''
        for component in ('Resources', 'Font'):
            key = prefix+component
            kind, value = document.xref_get_key(owner, key)
            if kind == 'xref':
                owner, prefix = int(value.split()[0]), ''
            else:
                if kind == 'null':
                    document.xref_set_key(owner, key, '<<>>')
                prefix = key+'/'
        document.xref_set_key(owner, prefix+alias, f'{ref} 0 R')
        paint = ' '.join(str(v) for v in color) + (' g' if len(color)==1 else ' rg' if len(color)==3 else ' k')
        commands.append(f'q {paint} BT /{alias} {g["size"]:.10f} Tf 1 0 0 1 '
                        f'{x:.10f} {page.rect.height-y:.10f} Tm <00> Tj ET Q')
    if commands:
        ref = document.get_new_xref()
        document.update_object(ref, '<<>>')
        document.update_stream(ref, ('\n'.join(commands)).encode())
        refs = page.get_contents()+[ref]
        document.xref_set_key(page.xref, 'Contents', '['+' '.join(f'{r} 0 R' for r in refs)+']')


def verify_inline(candidate, placements):
    """落盘后核验每个原字形的 ID、字号、颜色及平移位置，不以截图近似代替。

    CFF 轮廓直接引用原 FontDescriptor；复核实际绘制记录，防止锚点漏排、
    控制码替换或上下标基线改变。源保护区的像素检查由公共流程另行执行。
    """
    evidence = []
    with fitz.open(candidate) as document:
        traces = {i: [(s,c) for s in p.get_texttrace() for c in s['chars']]
                  for i,p in enumerate(document,1)}
        for item in placements:
            if 'inline_anchor' not in item:
                continue
            dx,dy = item['shift']
            anchor = item['inline_anchor']
            for g in anchor['glyphs']:
                expected = (g['origin'][0]+dx, g['origin'][1]+dy)
                found = [(s,c) for s,c in traces[item['page']] if
                         abs(c[2][0]-expected[0]) < .002 and abs(c[2][1]-expected[1]) < .002 and
                         abs(s['size']-g['size']) < .002 and tuple(s['color']) == tuple(g['color']) and
                         (c[1] == g['glyph_id'] if 'glyph_id' in g else c[0] == ord(g['text']))]
                if len(found) != 1:
                    raise ValueError(f"第{item['page']}页行内原字形重放校验失败：{g['text']!r}")
            evidence.append({'source_page': anchor['page'], 'page': item['page'],
                             'text': anchor['text'], 'shift': [dx,dy], 'glyphs': len(anchor['glyphs'])})
    return evidence


def merge_text_runs(placements):
    """同行相邻普通字一次写入，减少 PDF 对象并保持自然的文本提取顺序。"""
    result = []
    for item in placements:
        last = result[-1] if result else None
        if (last and 'inline_anchor' not in last and 'inline_anchor' not in item and
                last['page'] == item['page'] and last['bold'] == item['bold'] and
                abs(last['baseline']-item['baseline']) < .001 and
                abs(last['rect'][2]-item['rect'][0]) < .001):
            last['target'] += item['target']
            last['rect'] = list(fitz.Rect(last['rect']) | fitz.Rect(item['rect']))
        else:
            result.append(item)
    return result


def source_text(parts):
    """恢复物理行内相邻字体片段，避免 De + ﬁ + nition 变成三个词。"""
    import unicodedata
    result, previous, anchor_index = '', None, 0
    for part in parts:
        value = '{v'+str(anchor_index)+'}' if part['fixed'] else part['text']
        anchor_index += int(part['fixed'])
        if previous:
            same_line = (part['page'] == previous['page'] and
                         abs(part['baseline']-previous['baseline']) < .2*part['size'])
            gap = part['rect'][0]-previous['rect'][2]
            if not same_line or gap > .15*part['size']:
                result += ' '
        result += value
        previous = part
    # 只展开排印合字，不用全局 NFKC 改变上标数值或数学含义。
    for char in ('ﬀ','ﬁ','ﬂ','ﬃ','ﬄ'):
        result = result.replace(char,unicodedata.normalize('NFKC',char))
    return re.sub(r'\s+', ' ', result).strip()


def is_bold(parts):
    """标题中的普通数字/连接号不应把整条粗体标题降为正文。"""
    total = sum(len(p['text'].strip()) for p in parts)
    return sum(len(p['text'].strip()) for p in parts if p['bold']) > total/2


def merge_inline_parts(parts):
    """连续的数学字体片段合为同一锚点，保留原有上下标和内部间距。"""
    result = []
    for part in parts:
        last = result[-1] if result else None
        if (last and last['fixed'] and part['fixed'] and last['page'] == part['page'] and
                -part['size'] < part['rect'][0]-last['rect'][2] < part['size'] and
                min(last['rect'][3],part['rect'][3]) > max(last['rect'][1],part['rect'][1])):
            last['text'] += part['text']
            last['rect'] = list(fitz.Rect(last['rect'])|fitz.Rect(part['rect']))
            last['chars'] += part['chars']
        else:
            result.append({**part, 'chars': list(part['chars'])})
    return result


def relocate_links(document, original, placements):
    """引文移动时同步其点击区域，目标 URI/页号保持不变；未移动区域保持原样。"""
    expected = {i: list(p.get_links()) for i,p in enumerate(original,1)}
    moving = [p for p in placements if 'inline_anchor' in p]
    for source_page, source in enumerate(original, 1):
        for link in source.get_links():
            rect = fitz.Rect(link['from'])
            matches = [p for p in moving if p['inline_anchor']['page'] == source_page and
                       rect.intersects(fitz.Rect(p['inline_anchor']['ink']))]
            if len(matches) != 1:
                continue
            item = matches[0]
            glyphs = [g for g in item['inline_anchor']['glyphs'] if rect.intersects(fitz.Rect(g['rect']))]
            if not glyphs:
                continue
            box = fitz.Rect(glyphs[0]['rect'])
            for g in glyphs[1:]:box |= fitz.Rect(g['rect'])
            dx,dy=item['shift'];box=fitz.Rect(box.x0+dx,box.y0+dy,box.x1+dx,box.y1+dy)
            changed = {k:v for k,v in link.items() if k not in ('xref','id')}
            changed['from'] = box
            document[source_page-1].delete_link(link)
            document[item['page']-1].insert_link(changed)
            expected[source_page].remove(link)
            expected[item['page']].append(changed)
    return expected
