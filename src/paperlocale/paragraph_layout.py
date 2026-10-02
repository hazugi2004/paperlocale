"""自然段排版：物理框保存位置，逻辑段保证连续，行内原字形随正文流动。

不根据译文中的换行新增段落，不用空格撑满行，也不把引文锁在旧英文位置。
跨页/栏/图的段落仍只翻译一次；按原物理框容量分配连续的译文词元。
独立公式、图内文字、书目沿用源版面保护。未知原字体的行内字形明确报错。
"""
from __future__ import annotations

from .font_geometry import open_source_pdf

import math
import re
from statistics import median

import pymupdf as fitz

from .font_geometry import line_ink, _source_anchor_glyphs
from .source_layout import CAPTION, APPENDIX_CAPTION, MAIN_HEADING, NO_LINE_START, NO_LINE_END, anchor_errors


def starts_paragraph(native_lines, preceding, line, *, dash_lists=False, structured_headings=False):
    """原生块可包含多段；用首行缩进或额外行距划分，忽略同行上标碎片。"""
    if not preceding:
        return False
    previous = preceding[-1]
    sizes = [s['size'] for s in line['spans'] for c in s['chars'] if not c['c'].isspace()]
    # 控制码空格恢复后可能留下纯空白的原生行；它不构成新段落，
    # 也没有可用于估算正文行距的字号样本。
    if not sizes:
        return False
    size = median(sizes)
    baseline = max(s['origin'][1] for s in line['spans'])
    previous_y = max(s['origin'][1] for s in previous['spans'])
    if baseline - previous_y < .6 * size:
        return False
    value = ''.join(c['c'] for s in line['spans'] for c in s['chars'])
    # 悬挂列表的新编号相对正文是向左退回，而不是首行向右缩进。
    # 即使PDF把上一条末行与下一条首行放在一个文本块，也必须分段。
    if (dash_lists and re.match(r'^[–—]\s+[A-Za-z]', value) or
            re.match(r'^(?:\(\d{1,3}\)|\d{1,3}[.)]|[a-z][.)]|[•●▪])(?:\s+[A-Za-z]|$)', value)):
        return True
    prefix = ' '.join(''.join(c['c'] for s in l['spans'] for c in s['chars']) for l in preceding)
    if structured_headings and MAIN_HEADING.fullmatch(prefix.strip()):
        return True
    if re.match(r'^\d+(?:\.\d+)+\.?\s', prefix) and len(prefix.split()) <= 18:
        # 正常字重的两行小节标题可能与正文共用PDF块。以相对本块
        # 正文行距的额外留白结束标题，保留同样行距的标题续行。
        ys = sorted({round(s['origin'][1],2) for l in native_lines for s in l['spans']})
        gaps = [b-a for a,b in zip(ys,ys[1:]) if b-a > .6*size]
        if gaps and baseline-previous_y > 1.15*median(gaps):
            return True
    # 同一编号项的正文左边界来自续行，不能把含编号的首行当左边界；
    # 否则每条续行都会被误判为新段落。
    if re.match(r'^(?:\(\d{1,3}\)|\d{1,3}[.)])\s+', prefix):
        body_left = min((l['bbox'][0] for l in preceding[1:]), default=line['bbox'][0])
        if abs(line['bbox'][0]-body_left) < .7*size and baseline-previous_y < 1.65*size:
            return False
    # 字母编号的斜体标题可以自然续行；正文开始时的字体变化终止续接。
    if (re.match(r'^[a-z]\.\s+', prefix) and baseline-previous_y < 1.65*size and
            all(s['flags'] & 2 for l in [*preceding, line] for s in l['spans'])):
        return False
    # 独立圆点/编号在左侧悬挂，不能拿它作为正文左边界，否则列表
    # 的每条续行都会被误判为首行缩进，拆成多个自然段。
    prose_lines = [l for l in native_lines if re.search(r'[A-Za-z]{2}',
                   ''.join(c['c'] for s in l['spans'] for c in s['chars']))]
    left = min(l['bbox'][0] for l in (prose_lines or native_lines))
    indent = line['bbox'][0] - left
    widths = [l['bbox'][2] - l['bbox'][0] for l in native_lines]
    # 公式后的小片段也可能从右侧开始；段首缩进应小于四个正常字号。
    return (.75 * size < indent < 4 * size and max(widths) > 10 * size or
            baseline - previous_y > 1.65 * size)


def paragraph_groups(blocks, *, section_boundaries=True, mixed_first_page=True, dash_lists=False, pipe_headings=False, structured_headings=False, appendix_captions=False, heading_styles=False):
    """正文与图注分别建立阅读链；缩进段首和标题是不可跨越的段落边界。"""
    groups = []
    # AGU 首页为窄侧栏加宽正文，按页面中线分类会把侧栏续行与摘要交错。
    # 只在首面存在明确的宽正文和完全位于其左侧的窄栏时分别读取两条链；
    # 常规双栏/通栏及后续跨页顺序沿用原计划。
    first = [b for b in blocks if b['page'] == 1 and b['kind'] == 'body']
    wide = [b for b in first if 'page_width' in b and b['rect'][2]-b['rect'][0] > .5*b['page_width']
            and .25*b['page_width'] < b['rect'][0] < .5*b['page_width']]
    gutter = min((b['rect'][0] for b in wide), default=None)
    sidebar = gutter is not None and any(b['rect'][2] < gutter and
                  b['rect'][2]-b['rect'][0] < .3*b['page_width'] for b in first)
    if sidebar:
        top, bottom = min(b['rect'][1] for b in wide), max(b['rect'][3] for b in wide)
        lower = [b for b in first if b['rect'][1] >= bottom and
                 .3*b['page_width'] < b['rect'][2]-b['rect'][0] < .5*b['page_width']]
        # 必须在摘要下方同时看到正常宽度的左右两栏，才认定版式已切换。
        # AGU 窄侧栏可能比主栏更长，仅因超过主栏底部不能截断侧栏阅读链。
        mixed_first_page = mixed_first_page and any(b['rect'][0] < .25*b['page_width'] for b in lower) \
                           and any(b['rect'][0] >= .5*b['page_width'] for b in lower)
        def reading_key(block):
            # Elsevier 首页上半是关键词侧栏+宽摘要，下半已恢复标准双栏。
            # 不能把摘要的分栏线沿用到整页，否则下半左/右正文会按 y
            # 交错。只有侧栏高度范围内使用侧栏分栏线，其余按正常栏序。
            band = (0 if block['rect'][3] <= top else 1 if block['rect'][1] < bottom else 2) \
                   if mixed_first_page and block['page'] == 1 else 0
            sidebar_column = block['page'] == 1 and (not mixed_first_page or band == 1)
            column = int(block['rect'][2] >= gutter) if sidebar_column else int(block['rect'][0] >= block['page_width']/2)
            return block['page'], band, column, block['rect'][1]
        blocks = sorted(blocks, key=reading_key)
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
                if heading_styles and is_bold(old) and is_bold(ordinary) and previous.get('italic') != block.get('italic'):
                    style = False
                new_frame = block['page'] != previous['page'] or b.y0 < a.y0 or b.y0 - a.y1 > 2 * size
                full_end = old[-1]['rect'][2] > a.x1 - 2 * size
                incomplete = (not re.search(r'[.!?:;][\s\d)\]]*$', previous['text']) or
                              bool(re.search(r'\b(?:i\.e|e\.g)\.$', previous['text'])))
                connect = (style and block['page'] <= previous['page'] + 2 and indent < .7 * size and
                           (len(previous['text']) >= 25 or not new_frame) and
                           (incomplete or new_frame and full_end))
                # 编号首行可能以句号结束，后续悬挂行仍属于该列表项。
                # 只在同页紧邻、字号字重一致且左侧确有悬挂缩进时连接。
                if (kind == 'body' and style and block['page'] == previous['page'] and
                        (re.match(r'^(?:\(\d{1,3}\)|\d{1,3}[.)]|[a-z][.)]|[•●▪])\s+[A-Za-z]', previous['text']) or dash_lists and re.match(r'^[–—]\s+[A-Za-z]', previous['text'])) and
                        .7*size < b.x0-a.x0 < 4*size and -.5*size <= b.y0-a.y1 < size):
                    connect = True
                if kind == 'caption':
                    connect = (style and block['page'] == previous['page'] and
                               not (APPENDIX_CAPTION if appendix_captions else CAPTION).match(block['text']))
                elif block.get('list_start') or dash_lists and re.match(r'^[–—]\s+[A-Za-z]', block['text']) or re.match(r'^(?:\(\d{1,3}\)|\d{1,3}[.)]|[a-z][.)]|[•●▪])\s+[A-Za-z]', block['text']):
                    connect = False
                # 编号小节标题及独立公式之间的连接词各占原物理位置，
                # 不能把正文分配到标题/“and”的短框中。
                heading = lambda text: len(text.split()) <= 18 and bool(
                    re.match(r'^\d+(?:\.\d+)+\.?\s+[A-Z]', text) or
                    pipe_headings and re.match(r'^\d+(?:\.\d+)*\s*\|\s*[A-Z]', text) or
                    structured_headings and MAIN_HEADING.fullmatch(text))
                if sidebar and kind == 'body' and previous['page'] == block['page'] == 1 and (
                        (a.x1 < gutter) != (b.x1 < gutter)):
                    connect = False
                if kind == 'body' and (previous.get('heading') or block.get('heading') or heading(previous['text']) or heading(block['text']) or
                                       new_frame and re.fullmatch(r'and|or', block['text'], re.I)):
                    connect = False
                # 标题可能被出版社拆成两个原生块。仅连续接纳同样斜体、
                # 同栏紧邻且没有新编号的续行；普通正文永远终止标题组。
                title_group = any(b.get('heading') for b in blocks if b['id'] in groups[-1])
                if kind == 'body' and title_group:
                    connect = (style and block.get('italic') and not block.get('heading') and
                               block['page'] == previous['page'] and
                               -.5*size <= b.y0-a.y1 < size and abs(b.x0-a.x0) < 4*size)
                # 独立公式是物理和语义边界：公式两侧的“with/where”
                # 指向不同原位等式，不能合并后将解释文字移到另一式旁。
                # 以公式中心位于两个正文框之间为证据，同行的行内碎片
                # 不触发；公式自身继续使用源对象并接受像素保护检查。
                if kind == 'body' and previous['page'] == block['page'] and any(
                        f['kind'] == 'formula' and f['page'] == block['page'] and
                        a.y1 < (f['rect'][1]+f['rect'][3])/2 < b.y0 and
                        min(a.x0,b.x0) < f['rect'][2] and max(a.x1,b.x1) > f['rect'][0]
                        for f in blocks):
                    connect = False
                # 独立的摘要/关键词标题是语义边界，即使与作者同字号、
                # 同字重，也不能因作者行没有句号而跨过日期行合并。
                if section_boundaries and kind == 'body' and any(
                        re.fullmatch(r'Abstract|Keywords', value.strip(), re.I)
                        for value in (previous['text'], block['text'])):
                    connect = False
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
            # A citation can occupy a whole continuation frame. Its native
            # glyph runs still supply the original size, weight and baselines.
            metrics = ordinary or parts
            size = median(p['size'] for p in metrics)
            rect = fitz.Rect(b['rect'])
            ys = sorted(set(round(p['baseline'], 2) for p in metrics))
            gaps = [y-x for x,y in zip(ys, ys[1:]) if .8*size < y-x < 1.7*size]
            frame = {'page': b['page'], 'rect': list(rect), 'size': size,
                     'bold': is_bold(metrics),
                     'leading': median(gaps) if gaps else size * 1.2,
                     'indent': max(0, parts[0]['rect'][0] - rect.x0),
                     'weight': len(b['text']), 'source_block': sid}
            last = frames[-1] if frames else None
            # 悬挂缩进列表常被PDF拆为“带编号首行”和“内缩的后续行”。
            # 它们属于同一连续段落，不应按字符比例分给两个容量失衡的框。
            # 仅合并同页、右边界一致、紧邻且有明确列表编号的片段；后续
            # 行仍保留内缩，不能借合并框侵入编号左侧空白或邻近公式。
            hanging = (last and last['page'] == frame['page'] and
                       re.match(r'^(?:\(\d{1,3}\)|\d{1,3}[.)]|[a-z][.)]|[•●▪–—])\s+',
                                blocks[last['source_block']]['text']) and
                       .7*size < rect.x0-last['rect'][0] < 4*size)
            # 同段末行可比前行短；原生数学片段也可能与同一行正文框
            # 交叠。合并这些连续框，不把最后一句硬塞到短末行里。
            overlap = (last and (fitz.Rect(last['rect']) & rect).get_area() > 0)
            if (last and last['page'] == frame['page'] and
                    ((abs(last['rect'][0] + last.get('hanging_indent', 0) - rect.x0) < 1 and rect.x1 <= last['rect'][2]+2) or
                     hanging and rect.x1 <= last['rect'][2]+2 or overlap) and
                    (overlap or -.5*size <= rect.y0-last['rect'][3] < size) and
                    frame['indent'] < .7*size):
                if hanging:
                    last['hanging_indent'] = rect.x0-last['rect'][0]
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
    with open_source_pdf(source) as document:
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
                            from .diagnostics import LocatedError, unit_location
                            location = unit_location(unit)
                            location['anchor_text'] = anchor['text']
                            location['anchor_region'] = {'page': anchor['page'], 'rect': anchor['rect']}
                            raise LocatedError(
                                f"第{anchor['page']}页行内字形尚不支持原字体重排：{anchor['text']!r}",
                                [location], 'source-glyph')
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
                # A wrapped URL can have no prose on its final native line. Its
                # real hyperlink and character rectangles prove the continuation;
                # do not align that line relative to the preceding prose baseline.
                if re.search(r'[/#&]', anchor['text']) and not re.search(r'\s', anchor['text']):
                    url_text = anchor['text']
                    url_chars = [c for c in anchor['chars'] if c['text'].strip()]
                    # An opening prose parenthesis is outside the hyperlink.
                    # Keep its native glyph, but prove only the enclosed URL.
                    if re.match(r'\(https?://', url_text) and url_chars[0]['text'] == '(':
                        url_text, url_chars = url_text[1:], url_chars[1:]
                    for link in page.get_links():
                        uri = link.get('uri', '')
                        if (re.match(r'https?://', uri) and url_text in uri and
                                all(fitz.Point((c['rect'][0]+c['rect'][2])/2,
                                               (c['rect'][1]+c['rect'][3])/2) in link['from']
                                    for c in url_chars)):
                            anchor['source_url'] = uri
                            break


def compact_target(text):
    """仅清除中文排版的无语义空白；英文词组及数值与单位之间仍可有空格。"""
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'(?<=[\u3400-\u9fff，。；：！？（）]) +| +(?=[\u3400-\u9fff，。；：！？（）])', '', text)


def frame_boundary(tokens, desired, anchors):
    """在容量比例附近择句读跨框，避免把“我们”拆为两个框的孤字。

    只移动切点，不改写或增删译文。括号内单位和行内原字形一起参与
    括号深度判断；优先附近句读，否则保留合法的原比例边界。
    """
    depth = 0
    balanced, clauses = [], []
    for i, token in enumerate(tokens[:-1], 1):
        match = re.fullmatch(r'\{v(\d+)\}', token)
        value = anchors[int(match[1])]['text'] if match else token
        for char in value:
            if char in '（([【':
                depth += 1
            elif char in '）)]】':
                depth = max(0, depth-1)
        if depth == 0 and tokens[i][0] not in NO_LINE_START and value[-1] not in NO_LINE_END:
            balanced.append(i)
            if value[-1] in '。；，！？;!?':
                clauses.append(i)
    # 窄侧栏的单行框通常只能容纳容量估算附近的字。跨出数十词元去追
    # 后一句逗号会把整句硬塞到该行，导致后续框尚有空间却提前报溢出。
    nearby = [i for i in clauses if abs(i-desired) <= 3]
    # 原文可能含跨栏长括号，甚至自身缺右括号；内容合同会保留原文写法。
    # 此处不能为了寻找“括号外”切点退回数百词元，否则首框大片空白、
    # 后框容量被人为耗尽。跨框只是物理换栏，可在比例附近连续排完整括号。
    choices = nearby or [i for i in balanced if abs(i-desired) <= 3]
    return min(choices, key=lambda i: (abs(i-desired), i)) if choices else desired


def wrap_citation_anchors(unit, tokens):
    """宽引文按原分号、明确网址按原路径/参数分隔符换行，公式不拆分。

    模型仍只见原来的一个标记；内容合同先校验整个标记。这里只拆已有
    字形列表，每个字形恰好出现一次，字体、颜色、字号不改。避免一整行
    引文加句末中文标点超过栏宽，导致避头规则反复退回而误报容量不足。
    """
    from .source_layout import CITATION
    anchors = list(unit['anchors'])
    # A complete source citation may span several native line anchors. Confirm
    # its full syntax in reading order before allowing any fragment to wrap.
    source, expanded, cursor, spans = unit.get('source', ''), '', 0, []
    for marker in re.finditer(r'\{v(\d+)\}', source):
        expanded += source[cursor:marker.start()]
        index = int(marker[1])
        text = anchors[index]['text']
        spans.append((index, len(expanded)+len(text)-len(text.lstrip()),
                      len(expanded)+len(text.rstrip())))
        expanded += text
        cursor = marker.end()
    expanded += source[cursor:]
    citation_parts = {index for match in CITATION.finditer(expanded)
                      for index, start, end in spans if match.start() <= start < end <= match.end()}
    width = min(f['rect'][2]-f['rect'][0] for f in unit['frames'])
    replacements = {}
    for i, anchor in enumerate(unit['anchors']):
        citation = ';' in anchor['text'] and (i in citation_parts or CITATION.fullmatch(anchor['text']))
        url = anchor.get('source_url') or re.match(r'\(?https?://', anchor['text']) or (
                                       '&' in anchor['text'] and
                                       re.fullmatch(r'[\w%+=.&-]+', anchor['text']) and
                                       re.search(r'%[0-9A-Fa-f]{2}', anchor['text']) and '=' in anchor['text'])
        if (anchor.get('source_url') and anchor.get('glyphs') and
                all(abs(g['origin'][1]-anchor['baseline']) < .1*anchor['size'] for g in anchor['glyphs'])):
            anchor = {**anchor, 'body_baseline': anchor['baseline']}
            anchors[i] = anchor
        if (not (citation or url) or
                anchor['ink'][2]-anchor['ink'][0] < .8*width or not anchor.get('glyphs')):
            continue
        groups, current = [], []
        for position, glyph in enumerate(anchor['glyphs']):
            current.append(glyph)
            if (glyph['text'] in ('/&#' if url else ';') and not
                    (url and glyph['text'] == '/' and position+1 < len(anchor['glyphs'])
                     and anchor['glyphs'][position+1]['text'] == '/')):
                groups.append(current); current = []
        if current:
            groups.append(current)
        if len(groups) < 2:
            continue
        replacement = []
        for glyphs in groups:
            ink = fitz.Rect(glyphs[0]['rect'])
            for glyph in glyphs[1:]:
                ink |= fitz.Rect(glyph['rect'])
            replacement.append('{v'+str(len(anchors))+'}')
            anchors.append({**anchor, 'glyphs': glyphs, 'ink': list(ink),
                            'text': ''.join(g['text'] for g in glyphs)})
        replacements['{v'+str(i)+'}'] = replacement
    return anchors, [piece for token in tokens for piece in replacements.get(token, [token])]


def fit_paragraph(unit, target, font, min_size, bold_font):
    """在完整段落框中逐词元顺序排字；不分散行、不右推引文前的文字。

    单段最多收缩至原字号的 80%，保持全部内容；显式 min_size 优先。
    达到下限仍溢出则抛错，不截断、不自动生成缺字 PDF。
    """
    errors = anchor_errors(unit, target)
    if errors:
        raise ValueError('; '.join(errors))
    tokens = re.findall(r'\{v\d+\}|[A-Za-z0-9]+(?:[.−–/-][A-Za-z0-9]+)*|.', compact_target(target))
    anchors, tokens = wrap_citation_anchors(unit, tokens)
    size = median(f['size'] for f in unit['frames'])
    floor = size*.8 if min_size is None else min_size
    if not 0 < floor <= size:
        raise ValueError('最小字号必须为正数且不大于原字号')
    for step in range(math.ceil((size-floor)*10)+1):
        current = max(floor, size-step/10)
        remaining, placed = list(tokens), []
        for fi, frame in enumerate(unit['frames']):
            rect = fitz.Rect(frame['rect'])
            obstacles = [fitz.Rect(b) for b in frame.get('obstacles', [])]
            face = bold_font if frame['bold'] and bold_font is not None else font
            leading = max(current*1.13, frame['leading']*current/size)
            # 跨物理边界保留两侧的内容；分配完整连续词元，不另起逻辑段落。
            share = frame['weight']/sum(f['weight'] for f in unit['frames'][fi:])
            limit = len(remaining) if fi == len(unit['frames'])-1 else max(1, round(len(remaining)*share))
            if limit < len(remaining):
                limit = frame_boundary(remaining, limit, anchors)
            while limit < len(remaining) and (remaining[limit][0] in NO_LINE_START or remaining[limit-1][-1] in NO_LINE_END):
                limit += 1
            portion = remaining[:limit]
            cursor = 0
            prose = ''.join(t for t in portion if not re.fullmatch(r'\{v\d+\}', t))
            _, prose_bottom, _, prose_top = line_ink(face, prose, current) if prose.strip() else (0,0,0,0)
            # 表格边缘紧贴单行注释时，上标 a/b/c 的墨迹高于中文正文。
            # 避让宽障碍须计入本框内的原字号锚点，否则上标会被横向挤到表外，
            # 或整行无谓下移一个行距，误报溢出。最终仍逐字核验所有障碍和框边界。
            anchor_tops = [anchors[int(match[1])]['body_baseline'] -
                           anchors[int(match[1])]['ink'][1]
                           for token in portion if (match := re.fullmatch(r'\{v(\d+)\}', token))]
            clearance_top = max([prose_top] + anchor_tops)
            baseline = rect.y0 + current*.88
            row = 0
            previous_bottom = rect.y0 - .5
            while cursor < len(portion) and baseline <= rect.y1+.01:
                # 图框边缘有时仅侵入图注首行不到 1 pt。若先排左侧几个字
                # 再横跳整幅图，会产生巨大的词间空白。宽障碍覆盖半行以上
                # 时先将整行基线降至其下沿，仍受段落边界和最终墨迹检查约束。
                while True:
                    wide = [b for b in obstacles if (b & rect).width > .5*rect.width
                            and b.intersects(fitz.Rect(rect.x0, baseline-clearance_top,
                                                       rect.x1, baseline-prose_bottom))]
                    if not wide:
                        break
                    baseline = max(b.y1 for b in wide) + clearance_top + .001
                line_start = cursor
                # 超过普通首行缩进的空白可能属于同行前置公式/另一文本块。
                # 不能把这种几何占位压成两个汉字，否则会与独立排版的连接词重叠。
                indent = frame['indent'] if frame['indent'] > 4*size else min(frame['indent'], 2*current)
                x = rect.x0 + (indent if row == 0 and fi == 0
                               else frame.get('hanging_indent', 0))
                line_items = []
                while cursor < len(portion):
                    token = portion[cursor]
                    match = re.fullmatch(r'\{v(\d+)\}', token)
                    anchor = anchors[int(match[1])] if match else None
                    if anchor:
                        ink = fitz.Rect(anchor['ink'])
                        # 同行公式被 PDF 拆为固定左半和可译块中的右括号时，
                        # 首个闭合片段必须接回原字形位置，不能随中文基线上移。
                        # 只处理首词元、短闭合片段及紧邻的已保护墨迹；完整
                        # 引文和后续普通行内公式仍正常流动。
                        if (row == 0 and fi == 0 and cursor == 0 and len(anchor['text']) <= 12
                                and re.search(r'[)\]}]$', anchor['text'])
                                and not re.search(r'[(\[{]', anchor['text'])
                                and any(0 <= ink.x0-b.x1 < .5*size and
                                        min(ink.y1,b.y1)>max(ink.y0,b.y0) for b in obstacles)):
                            x = ink.x0-.25
                            baseline = anchor['body_baseline']
                        width = ink.width + .5
                        top, bottom = anchor['body_baseline']-ink.y0, anchor['body_baseline']-ink.y1
                    else:
                        width = face.text_length(token, fontsize=current)
                        _, bottom, _, top = line_ink(face, token, current) if token.strip() else (0,0,0,0)
                    # 同行逐个跳过固定公式墨迹，整行被占用时改到下一行。
                    # 文字、行内锚点都适用；不覆盖、移动或删减固定公式。
                    while True:
                        hit = [b for b in obstacles if b.intersects(
                            fitz.Rect(x, baseline-top, x+width, baseline-bottom))]
                        if not hit:
                            break
                        x = max(b.x1 for b in hit) + .001
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
                    baseline += leading
                    row += 1
                    continue
                # 上标/下标的真实墨迹可能高于当前中文行高；按相邻两行
                # 的实际墨迹留出间隔，避免缩字号后公式撞入上一行。
                gap = min(p['rect'][1] for p in line_items) - previous_bottom
                if gap < .5:
                    delta = .5-gap
                    baseline += delta
                    for p in line_items:
                        p['baseline'] += delta
                        p['rect'][1] += delta
                        p['rect'][3] += delta
                        if 'shift' in p:
                            p['shift'][1] += delta
                # 上下标导致基线下移后重新检查；整行重新排，不能留下一条
                # 调整前安全、调整后撞入公式的文字。词元游标回退保证不丢字。
                if any(fitz.Rect(p['rect']).intersects(b) for p in line_items for b in obstacles):
                    cursor = line_start
                    baseline += leading
                    row += 1
                    continue
                previous_bottom = max(p['rect'][3] for p in line_items)
                if previous_bottom > rect.y1 + .001:
                    cursor = 0
                    break
                placed.extend(line_items)
                baseline += leading
                row += 1
            if cursor < len(portion):
                break
            del remaining[:limit]
        if not remaining:
            return merge_text_runs(placed)
    pages = sorted({frame['page'] for frame in unit['frames']})
    page_label = f"第{pages[0]}页" if len(pages) == 1 else f"第{pages[0]}–{pages[-1]}页"
    raise ValueError(f"{page_label}完整译文无法放入段落框：{unit['id'][:12]}，剩余 {len(remaining)} 个词元")


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
            # PyMuPDF 返回解码后的 PDF 名称；写回字典必须重新转义空格等字节。
            base = '/' + ''.join(chr(c) if 33 <= c <= 126 and chr(c) not in '#%()/<>[]{}'
                                 else f'#{c:02X}' for c in base.lstrip('/').encode())
            descriptor = document.xref_get_key(original, 'FontDescriptor')[1]
            if g.get('truetype'):
                descriptor = f'{g["descriptor"]} 0 R'
            name = ''.join(chr(c) if 33 <= c <= 126 and chr(c) not in '#%()/<>[]{}' else f'#{c:02X}' for c in g['name'].encode())
            cmap = document.get_new_xref()
            document.update_object(cmap, '<<>>')
            unicode = g['text'].encode('utf-16-be').hex()
            code = g.get('source_code', '0000' if g.get('truetype') else '00')
            limit = 'f' * len(code)
            document.update_stream(cmap, ('/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n'
                '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
                f'/CMapName /PLInline def /CMapType 2 def\n1 begincodespacerange <{code}> <{limit}> endcodespacerange\n'
                f'1 beginbfchar <{code}> <{unicode}> endbfchar\nendcmap CMapName currentdict /CMap defineresource pop end end').encode())
            ref = document.get_new_xref()
            if 'source_code' in g:
                # 编码和原字体字典原样复用；只更新检索文本的 ToUnicode。
                document.update_object(ref, document.xref_object(original))
                document.xref_set_key(ref, 'ToUnicode', f'{cmap} 0 R')
            elif g.get('truetype'):
                # CID 0 显式映射到原 GID，直接引用原 FontFile2 描述符；
                # 不转曲、不按 Unicode 猜字体，也不改变数值或字符基线。
                mapping = document.get_new_xref()
                document.update_object(mapping, '<<>>')
                document.update_stream(mapping, g['glyph_id'].to_bytes(2, 'big'))
                descendant = document.get_new_xref()
                document.update_object(descendant, f'<< /Type /Font /Subtype /CIDFontType2 /BaseFont {base} '
                    f'/FontDescriptor {descriptor} /DW {g["advance"]:.10f} /CIDToGIDMap {mapping} 0 R '
                    '/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>')
                document.update_object(ref, f'<< /Type /Font /Subtype /Type0 /BaseFont {base} '
                    f'/Encoding /Identity-H /DescendantFonts [{descendant} 0 R] /ToUnicode {cmap} 0 R >>')
            else:
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
        # 字形原点是 MuPDF 的裁切后页面坐标；PDF 内容流使用 MediaBox
        # 坐标。height-y 只适用于 CropBox 原点为零的页面。非零裁切边距
        # 必须逆变换，否则所有重放引文会一起偏移到栏外（HESS 实例）。
        pdf_point = fitz.Point(x, y) * ~page.transformation_matrix
        commands.append(f'q {paint} BT /{alias} {g["size"]:.10f} Tf 1 0 0 1 '
                        f'{pdf_point.x:.10f} {pdf_point.y:.10f} Tm <{g.get("source_code", "0000" if g.get("truetype") else "00")}> Tj ET Q')
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
    """从保留源字符/坐标的片段生成语义文本，复原合字与可选断词。

    U+00AD 仅在普通正文中作为排印控制移除，原始 parts 不变，供逐字
    删除校验。断词后的字母续接不插空格；显式连字符、减号和公式锚点
    保持原义，不把所有横线当作可删除断词符。
    """
    import unicodedata
    result, previous, anchor_index = '', None, 0
    for part in parts:
        value = '{v'+str(anchor_index)+'}' if part['fixed'] else part['text']
        anchor_index += int(part['fixed'])
        if previous:
            same_line = (part['page'] == previous['page'] and
                         abs(part['baseline']-previous['baseline']) < .2*part['size'])
            gap = part['rect'][0]-previous['rect'][2]
            soft_join = (not previous['fixed'] and not part['fixed']
                         and previous['text'].endswith('\u00ad')
                         and bool(re.match(r'[A-Za-z]', value)))
            if (not same_line or gap > .15*part['size']) and not soft_join:
                result += ' '
        if not part['fixed']:
            value = value.replace('\u00ad', '')
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
                (-max(part['size'],last['size']) < part['rect'][0]-last['rect'][2] < max(part['size'],last['size'])
                 # 上下标共享横向起点，较长上标后出现的下标仍属于同一
                 # 不可拆分锚点；保持字符原坐标，绝不把 r 另排到下一行。
                 or part['size'] < .8*last['size'] and
                 last['rect'][0] < part['rect'][0] < last['rect'][2]) and
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
            if not matches:
                continue
            moved_links = []
            for item in matches:
                glyphs = [g for g in item['inline_anchor']['glyphs'] if rect.intersects(fitz.Rect(g['rect']))]
                if not glyphs:
                    continue
                box = fitz.Rect(glyphs[0]['rect'])
                for g in glyphs[1:]:box |= fitz.Rect(g['rect'])
                dx,dy=item['shift'];box=fitz.Rect(box.x0+dx,box.y0+dy,box.x1+dx,box.y1+dy)
                changed = {k:v for k,v in link.items() if k not in ('xref','id')}
                changed['from'] = box
                moved_links.append((item['page'], changed))
            if not moved_links:
                continue
            deletion = link
            if not link.get('xref'):
                # 部分命名目标使MuPDF返回xref=0；delete_link此时静默无效，
                # 会留下旧点击区域。源对象不重编号，从原注释的唯一矩形
                # 找回真实编号；不凭顺序猜测，也不删除同页其他链接。
                candidates = []
                for xref, kind, _ in source.annot_xrefs():
                    if kind != fitz.PDF_ANNOT_LINK:
                        continue
                    value = original.xref_get_key(xref, 'Rect')
                    if value[0] != 'array':
                        continue
                    coords = [float(v) for v in value[1].strip('[]').split()]
                    box_source = fitz.Rect(coords).normalize() * source.transformation_matrix
                    if max(abs(a-b) for a,b in zip(box_source, rect)) < .002:
                        candidates.append(xref)
                if len(candidates) != 1:
                    raise ValueError(f'第{source_page}页链接原注释无法唯一定位')
                deletion = {**link, 'xref': candidates[0]}
            document[source_page-1].delete_link(deletion)
            expected[source_page].remove(link)
            # 一条长网址换行后各段仍指向同一原始目标；删除旧点击区一次，
            # 按实际字形组建立多个新区域，不把整条链接留在旧英文位置。
            for target_page, changed in moved_links:
                document[target_page-1].insert_link(changed)
                expected[target_page].append(changed)
    return expected


def links_equal(expected, actual):
    """链接目标逐字段相同；新浮点坐标仅容许PDF序列化的0.002点误差。"""
    pending = list(actual)
    def same(a,b):
        keys = (set(a)|set(b))-{'xref','id'}
        for key in keys:
            x,y=a.get(key),b.get(key)
            if isinstance(x,(fitz.Point,fitz.Rect)) and isinstance(y,type(x)):
                if max(abs(m-n) for m,n in zip(x,y)) > .002:
                    return False
            elif x != y:
                return False
        return True
    for link in expected:
        found = next((i for i,p in enumerate(pending) if same(link,p)),None)
        if found is None:
            return False
        pending.pop(found)
    return not pending
