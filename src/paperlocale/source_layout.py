"""源页保版计划：将逻辑阅读顺序与物理位置分离。

同一逻辑段落可以有任意多个跨页、跨栏或绕图的文本块。公式/引用以占位符
参加语序校验，但其原始 PDF 字符从不删除或重写。整张表格、图像、书目等
保护区域不进入翻译。自动识别是启发式，不能据此宣称任意 PDF 的语义完备。
计划仅作为自动阶段的可审计断点；源文本和坐标由原 PDF 重新核对。
"""
from __future__ import annotations

from .font_geometry import open_source_pdf

import hashlib
import json
import math
import re
from pathlib import Path
from statistics import median

import pymupdf as fitz
from PIL import Image, ImageChops, ImageDraw

from .contracts import FORMULA_RE, URL_RE, segment_id, scientific_literal_spans
from .references import _reference_geometry
from .image_geometry import visible_image_regions
from .safe_text import writing_rectangles, fixed_text_rectangles
from .font_geometry import line_ink, open_source_for_editing
from .quantities import find_quantities, standalone_units

CITATION = re.compile(r'\[\s*\d+(?:\s*[,;–−-]\s*\d+)*\s*\]|'
                      r'\(\s*refs?\.?\s*\d+(?:\s*[,;–−-]\s*\d+)*\s*\)|'
                      r'\((?!January\b|February\b|March\b|April\b|May\b|June\b|July\b|'
                      r'August\b|September\b|October\b|November\b|December\b)'
                      r"[A-Z][A-Za-z'’−–-]+(?:\s+(?:[A-Z][A-Za-z'’−–-]+|et|al\.?|and|&))*"
                      r',?\s+(?:18|19|20)\d{2}[a-z]?(?:[,;][^()]*)?\)')
# 拉丁扩展姓名仍是源引文，保留原字形而不猜测重音拼写或依赖中文字体。
LEGACY_CITATION = CITATION
CITATION = re.compile(CITATION.pattern.replace('[A-Z]', '[A-ZÀ-ÖØ-ÞĀ-ž]').replace('[A-Za-z', '[A-Za-zÀ-ÖØ-öø-ž'))
MATH_FONT = re.compile(r'math|mth|symbol|cmsy|cmmi|cmex|msam|msbm', re.I)
# 图注编号后须有标点，或以大写词开启图注正文；正文中的“Figure 5
# displays”和“Figure 2a”不是图注。罗马数字表号同样构成独立标题，
# 防止连续的 Table III/IV/V 被合并为一个段落。无标点且小写开头的
# 非标准图注仍依靠视觉检测器分类，不能只凭正文中的图号引用推断。
LEGACY_CAPTION = re.compile(r'^(?:(?:Extended Data|Supplementary)\s+)?(?:Fig(?:ure)?\.?|Table)\s*\d', re.I)
CAPTION = re.compile(
    r'^(?i:(?:(?:Extended Data|Supplementary)\s+)?(?:Fig(?:ure)?\.?|Table))'
    r'\s*(?:\d+|[IVXLCDM]+)(?:\s*[.|:]|\s+(?=[A-Z]))')
APPENDIX_CAPTION = re.compile(CAPTION.pattern.replace(r'\d+|', r'[A-Z]?\d+|'))
METADATA = re.compile(r'^(?:Received:|Accepted:|Published online:|Check for updates$)', re.I)
NO_LINE_START = set('，。、；：？！）》」』】％‰,.;:!?%)]}')
NO_LINE_END = set('（《「『【([{')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path: Path, value: dict) -> None:
    from .recovery import _save
    _save(path, value)


def _plain(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def _rect_union(chars: list) -> list[float]:
    rect = fitz.Rect(chars[0]['bbox'])
    for char in chars[1:]:
        rect |= fitz.Rect(char['bbox'])
    return list(rect)


def _bold(span, *, paragraph=False):
    """出版社子集字体有时遗漏 PDF 粗体 flag，但保留明确的字重名称。"""
    suffix = r'(?:\+[A-Za-z0-9]+)?' if paragraph else ''
    return bool(span['flags'] & 16 or re.search(r'(?:[.-]B|Bold(?:Italic|Oblique)?)'+suffix+'$', span['font']))


def _superscript(span, line, *, paragraph=False):
    """段落模式用字号及基线复核上标flag，避免低位下标使整行正文被误标。

    PyMuPDF在同一行先读到公式下标时，可把其后正常基线的正文标为上标。
    以该行常规字号字符的多数基线作参照；真正小字号或抬高基线仍保护。
    旧引擎保持原flag解释，避免更改已有保护计划。
    """
    if not span['flags'] & 1 or not paragraph:
        return bool(span['flags'] & 1)
    chars = [(s['size'], c['origin'][1]) for s in line['spans']
             for c in s['chars'] if not c['c'].isspace()]
    # Springer PDF 可把纯空格单独标成上标；没有可见字时不能估计基线。
    if not chars:
        return bool(span['flags'] & 1)
    size = median(c[0] for c in chars)
    baseline = median(c[1] for c in chars if c[0] >= .95*size)
    return span['size'] < .95*size or span['origin'][1] < baseline-.2*size


def _parts(block: dict, page_number: int, links: list, *, paragraph=False, greek_variants=True, extended_citations=True, italic_words=False, short_prose=False, script_geometry=False) -> list[dict]:
    """按字符坐标分开普通文字和固定锚点，不用白块覆盖公式/引用。

    上标、数学字体及明确引文模式保持原对象。普通斜体不能一律当公式，
    否则会把物种名或强调正文误排除；独立数学区域另外由自动检测保护。
    """
    if paragraph and script_geometry >= 2:
        # 正体文字等式也有数学含义，例如 Risk = Hazard × Vulnerability ×
        # Exposure，且可跨视觉行。只识别由明确操作数/运算符组成的等式；
        # 在源字符上做标记，按原物理行保留字形，不让模型重写字体或符号。
        flat = []
        for source_line in block.get('lines', []):
            flat.extend(c for s in source_line['spans'] for c in s['chars'])
            flat.append({'c': '\n'})
        value = ''.join(c['c'] for c in flat)
        operand = r'(?:[A-Za-zα-ωΑ-Ω][A-Za-zα-ωΑ-Ω0-9]*|\d+(?:\.\d+)?)'
        operator = r'\s*[+−*/×÷-]\s*'
        parenthesized = r'\(\s*' + operand + r'(?:' + operator + operand + r')+\s*\)'
        atom = r'(?:' + parenthesized + '|' + operand + ')'
        side = atom + r'(?:' + operator + atom + r')*'
        for equation in re.finditer(r'(?<![\w])' + side + r'\s*=\s*' + side, value):
            if re.search(r'[+−*/×÷-]', equation.group()):
                for char in flat[equation.start():equation.end()]:
                    char['_plain_math'] = True
    result = []
    # 全行斜体自然语言（常见小节标题）中的短词不是变量。变量混排、
    # 数学字体、真实上下标和希腊字母仍走原保护规则。
    for line in block.get('lines', []):
        chars = []
        for span in line['spans']:
            superscript = _superscript(span, line, paragraph=paragraph)
            math_font = MATH_FONT.search(span['font']) or paragraph and re.match(r'^MT(?:MI|SY|EX)', span['font'])
            for char in span['chars']:
                # 出版社数学字体可能将括号映射到控制码，普通中文字体
                # 无法重建其字形；保留源字形，不能把控制码当正文传给模型。
                # 普通正文 U+00AD 是可选断词符，须随正文删除而非重放为
                # 公式锚点；chars 仍完整保存原字符/坐标，旧模式保持原行为。
                chars.append({**char, 'fixed': bool(superscript or math_font or char.get('_plain_math')
                                                  or 'native_text' in char or (not char['c'].isprintable()
                                                      and not (paragraph and char['c'] == '\u00ad'))),
                              'size': span['size'], 'italic': bool(span['flags'] & 2 or
                                  re.search(r'(?:[.-]I|Italic|Oblique)$', span['font'])),
                              'bold': _bold(span, paragraph=paragraph),
                              'superscript': superscript})
        if not chars:
            continue
        text = ''.join(c['c'] for c in chars)
        # 行内变量未必使用数学字体：真实论文的 β 带帽、R_n、T_d 分布在
        # 普通斜体和小字号下标中。按完整变量词元保护基字及上下标，避免
        # 只留下原帽号、却用中文字体重写 β。普通斜体物种名/强调词不适用。
        normal_size = median(c['size'] for c in chars)
        words = re.findall(r'[A-Za-z]+', text)
        italic_prose = (paragraph and sum(len(w) > 3 for w in words) >= 2 and
                        all(c['italic'] for c in chars if c['c'].isalpha()))
        # ϕ/ϑ/ϖ 等数学变体不在 α–ω 范围内；原字形保护不能依赖中文字体覆盖。
        # 旧计划按记录的符号规则复核，新提取显式记录修订号，避免暗改缓存身份。
        greek = 'α-ωΑ-Ω' + ('ϐϑϒϕϖϰϱϲϴϵ϶' if greek_variants and paragraph else '')
        # 普通斜体连接词/拉丁缩写不构成数学变量。只豁免明确词元；
        # 数学字体、原生符号、希腊字母与真实上下标仍保留原字形。
        prose_spans = [m.span() for m in re.finditer(r'\b(?:i\.e\.|e\.g\.)', text)] if italic_words else []
        for token in re.finditer(r'(?<![A-Za-z])[A-Za-z'+greek+'][A-Za-z'+greek+'0-9∧]*', text):
            run = chars[token.start():token.end()]
            prose_word = italic_words and (token.group().lower() in {'the', 'and', 'or', 'for'} or
                                            short_prose and token.group() in {'to', 'as'} or
                                            any(a <= token.start() and token.end() <= b for a,b in prose_spans))
            variable = (bool(re.search('['+greek+']', token.group())) or
                        (paragraph and all(c['italic'] for c in run) and
                         min(c['size'] for c in run) < .85*max(c['size'] for c in run)) or
                        (not italic_prose and not prose_word and len(token.group()) <= 3 and (run[0]['italic'] or
                         run[0]['fixed'] and any(c['italic'] for c in run)) and
                         not (len(token.group()) > 1 and re.fullmatch(r'-\s*', text[token.end():]))) or
                        (token.group().isupper() and len(token.group()) <= 6 and
                         all(c['size'] < .95 * normal_size for c in run)))
            if variable:
                for char in run:
                    char['fixed'] = True
        # 带幂的单位可能横跨普通字体与数学上标。已有单位解析器提供明确
        # 量值/复合单位边界；只要其中有数学字形，就整体保留，不能只留 -2
        # 却重写 m。普通正文里的无数学标记量值仍由翻译内容合同校验。
        if paragraph and script_geometry:
            # Copernicus 正体 PEI₃₀、二进制 0001₂ 可没有任何上下标 flag。
            # 同一紧邻词元内同时存在常规字符和缩小且偏离基线的字符才
            # 保护整个词元；不能只重放下标而重排它的基字，也不靠数字值猜。
            for token in re.finditer(r'[A-Za-z0-9α-ωΑ-Ω]+', text):
                run = chars[token.start():token.end()]
                largest = max(c['size'] for c in run)
                bases = [c for c in run if c['size'] >= .95*largest]
                baseline = median(c['origin'][1] for c in bases)
                if any(c['size'] < .85*largest and abs(c['origin'][1]-baseline) > .08*largest
                       for c in run):
                    for char in run:
                        char['fixed'] = True
        quantities = [(q.start, q.end) for q in find_quantities(text)]
        quantities += [(a, b) for a, b, _ in standalone_units(text)]
        for start, end in quantities:
            if any(c['fixed'] for c in chars[start:end]):
                for char in chars[start:end]:
                    char['fixed'] = True
        # 坐标和科学计数法必须完整保护，不能把度/负号/指数拆散后
        # 让模型把余下数字当作普通整数。单位文字可翻译，数值不换算。
        if paragraph:
            literals = [r'[<>≤≥]?\s*[−-]\d+(?:\.\d+)?', r'[<>≤≥]\s*\d+(?:\.\d+)?', r'\d{1,3}°\d{1,2}′(?:[–−-]\d{1,3}°\d{1,2}′)?[EWNS]?',
                        r'[+−-]?(?:\d+(?:\.\d+)?\s*×\s*)?10[−-]\d+']
            for pattern in literals:
                for match in re.finditer(pattern, text):
                    for char in chars[match.start():match.end()]:
                        char['fixed'] = True
        ranges = [m.span() for pattern in (CITATION if extended_citations else LEGACY_CITATION, URL_RE) for m in pattern.finditer(text)] + scientific_literal_spans(text)
        # 实测 (r: 0.79–0.99; p < 0.01) 曾只保留斜体 r/p，数字和
        # 运算符却被重新排字。括号内完全由短变量、数字和数学分隔符构成
        # 且含关系运算符时，保护整个表达式。含自然语言说明的括号不适用。
        for expression in re.finditer(r'\([^()]+\)', text):
            inside = expression.group()[1:-1]
            if (re.search(r'[=<>≤≥]', inside) and
                    re.fullmatch(r'[A-Za-zα-ωΑ-Ω0-9\s.,;:+*/=<>≤≥−–%±^-]+', inside) and
                    all(len(word) <= 3 for word in re.findall(r'[A-Za-z]+', inside))):
                ranges.append(expression.span())
        for start, end in ranges:
            for char in chars[start:end]:
                char['fixed'] = True
        # F1,180544 = 11887、p < 0.001 等统计量可跨普通字、斜体和
        # 小字号下标。已有变量证据时保护整条数值关系；否则把下标数字
        # 作为正文字号重排既改变公式排印，又会制造并不存在的空间不足。
        relation = r'(?<![A-Za-z])[A-Za-zα-ωΑ-Ω][A-Za-zα-ωΑ-Ω0-9]*(?:,\d+)*\s*[=<>≤≥]\s*[+−-]?\d+(?:[.,]\d+)*(?:[eE][+−-]?\d+)?'
        for expression in re.finditer(relation, text):
            if chars[expression.start()]['fixed'] or (paragraph and any(
                    'native_text' in c for c in chars[expression.start():expression.end()]) and
                    re.match(r'[A-Z][A-Z0-9]*\s*[=<>≤≥]', expression.group())):
                for char in chars[expression.start():expression.end()]:
                    char['fixed'] = True
        # 标准数学函数名可能用普通正体；仅在紧随已识别数学变量或
        # 数学括号时保护，正文中讨论“log”等单词仍交给翻译器。
        # 用 ASCII 单词边界；数学字体把括号映射为 ð/Þ 时，Unicode
        # 的 \b 会错误地把 SDð 当成同一个单词，漏掉整个函数。
        for function in re.finditer(r'(?<![A-Za-z])(?:exp|log|ln|sin|cos|tan|SD)(?![A-Za-z])', text):
            right = function.end()
            while right < len(chars) and (chars[right]['c'].isspace() or chars[right]['c'] in '('):
                right += 1
            if right < len(chars) and chars[right]['fixed']:
                for char in chars[function.start():function.end()]:
                    char['fixed'] = True
        # 上下标常被提取成独立字形；两侧均已有数学锚点的运算符也是
        # 表达式的一部分，不能把加号等当成需要翻译的极窄正文片段。
        operators = r'[+−=<>≤≥×÷±*/′’][0-9.\s+−=<>≤≥×÷±*/′’]*' if paragraph else r'[+−=<>≤≥×÷±*/][0-9.\s+−=<>≤≥×÷±*/]*'
        for operator in re.finditer(operators, text):
            left, right = operator.start() - 1, operator.end()
            while left >= 0 and chars[left]['c'].isspace():
                left -= 1
            while right < len(chars) and chars[right]['c'].isspace():
                right += 1
            # 变量下标中的 + 1 即使后面接正文，也属于公式；要求整个
            # 非空白运算片段都使用小于正文的字号，不能吞掉正文数值。
            subscript = all(c['size'] < .95 * normal_size
                            for c in chars[operator.start():operator.end()] if not c['c'].isspace())
            if left >= 0 and chars[left]['fixed'] and (
                    right < len(chars) and chars[right]['fixed'] or subscript):
                for char in chars[operator.start():operator.end()]:
                    char['fixed'] = True
        for link in links:
            # 引用框轻微碰到正文词尾时用字符中心归属；但“可点击”本身
            # 也不等于引用。出版社的宽链接框曾覆盖版权说明、附加信息等
            # 正文，因此只保护明确编号或与目标 URI 相符的可见网址片段。
            inside = [c for c in chars if fitz.Point((c['bbox'][0] + c['bbox'][2]) / 2,
                                                    (c['bbox'][1] + c['bbox'][3]) / 2) in fitz.Rect(link['from'])]
            label = ''.join(c['c'] for c in inside).strip(' ()[],.;')
            number = re.fullmatch(r'(?:Fig(?:ure)?\.?\s*)?\d+[a-z]?(?:\s*[,;–−-]\s*(?:\d+[a-z]?|[a-z]))*', label)
            address = ''.join(c['c'] for c in inside if not c['superscript']).strip(' ()[],.;')
            compact_address = re.sub(r'\s+', '', address)
            uri = str(link.get('uri', ''))
            url = compact_address.startswith(('https://', 'http://', 'www.')) or (
                bool(re.search(r'[./?#]', compact_address)) and compact_address in uri) or (
                bool(compact_address) and not re.search(r'\s', address) and uri.endswith(compact_address))
            if number or url:
                for char in inside:
                    char['fixed'] = True
        # 紧贴固定公式/网址的原括号也属于该引用表达式，不能只保留里面
        # 的字符，却把原本约 3 pt 的右括号区域交给一个全角中文字形。
        # URL 跨栏尾段可仅剩 mary 等纯字母，由目标 URI 尾段匹配识别。
        for index, char in enumerate(chars):
            direction = 1 if char['c'] == '(' else -1 if char['c'] == ')' else 0
            if not direction:
                continue
            neighbor = index + direction
            while 0 <= neighbor < len(chars) and chars[neighbor]['c'].isspace():
                neighbor += direction
            if 0 <= neighbor < len(chars) and chars[neighbor]['fixed']:
                char['fixed'] = True
        if paragraph:
            # 分开绘制的组合重音属于自然语言单词（如 Niño），不是数学
            # 上标。只解除两侧至少三个拉丁字母且其余字符均为普通正文
            # 的标记；变量 x̂、已有数学/引文锚点仍保持原保护规则。
            for word in re.finditer(r'[A-Za-z]+[\u0300-\u036f][A-Za-z]*', text):
                run = chars[word.start():word.end()]
                letters = [c for c in run if c['c'].isascii() and c['c'].isalpha()]
                if len(letters) >= 3 and not any(c['fixed'] for c in letters):
                    for c in run:
                        c['fixed'] = False
        runs = []
        for char in chars:
            if not runs or (runs[-1][0]['fixed'], runs[-1][0]['bold']) != (char['fixed'], char['bold']):
                runs.append([])
            runs[-1].append(char)
        for run in runs:
            value = ''.join(c['c'] for c in run)
            if not value.strip():
                continue
            result.append({'page': page_number, 'rect': _rect_union(run),
                           'text': value, 'fixed': run[0]['fixed'],
                           'bold': run[0]['bold'],
                           'size': median(c['size'] for c in run),
                           'baseline': median(c['origin'][1] for c in run),
                           'chars': [{'text': c.get('native_text', c['c']), 'rect': list(c['bbox']), 'origin': list(c['origin']),
                                      **({'semantic_text': c['c']} if 'native_text' in c else {}),
                                      **({'source_blank': c['source_blank']} if 'source_blank' in c else {})}
                                     for c in run]})
    return result


def _protect_group_citations(blocks, groups):
    """Match complete citations in established reading order, retaining glyph runs."""
    by_id = {b['id']: b for b in blocks}
    for group in groups:
        text, locations, previous = '', [], None
        for sid in group:
            for index, part in enumerate(by_id[sid]['parts']):
                if previous and (part['page'] != previous['page'] or
                        abs(part['baseline']-previous['baseline']) >= .2*part['size'] or
                        part['rect'][0]-previous['rect'][2] > .15*part['size']):
                    text += ' '
                    locations.append(None)
                for offset, char in enumerate(part['chars']):
                    value = char.get('semantic_text', char['text'])
                    text += value
                    locations.extend([(sid, index, offset)] * len(value))
                previous = part
        fixed = {location for match in CITATION.finditer(text)
                 for location in locations[match.start():match.end()] if location is not None}
        for sid in group:
            parts = []
            for index, part in enumerate(by_id[sid]['parts']):
                if part['fixed'] or not any((sid, index, i) in fixed for i in range(len(part['chars']))):
                    parts.append(part)
                    continue
                runs = []
                for offset, char in enumerate(part['chars']):
                    protected = (sid, index, offset) in fixed
                    if not runs or runs[-1][0] != protected:
                        runs.append((protected, []))
                    runs[-1][1].append(char)
                for protected, chars in runs:
                    value = ''.join(c.get('semantic_text', c['text']) for c in chars)
                    if not value.strip():
                        continue
                    bounds = fitz.Rect(chars[0]['rect'])
                    for char in chars[1:]:
                        bounds |= fitz.Rect(char['rect'])
                    parts.append({**part, 'fixed': protected, 'text': value,
                                  'rect': list(bounds), 'chars': chars})
            by_id[sid]['parts'] = parts
    # A standalone wrapped citation has no prose to translate. Mixed groups
    # retain their citation-only continuation frames for native glyph placement.
    retained = []
    for group in groups:
        if any(not p['fixed'] for sid in group for p in by_id[sid]['parts']):
            retained.append(group)
        else:
            for sid in group:
                by_id[sid]['kind'] = 'formula'
    return retained


# 辅助章节按阅读顺序持续保护，直到再次遇到明确的正文一级标题。
# 有些期刊把 Methods 放在 References 之后，不能把参考文献之后一律排除。
AUXILIARY_HEADING = re.compile(
    r"^(?:acknowledg(?:e)?ments?|references|bibliography|literature cited|"
    r"author (?:information|contributions?|affiliations?)|affiliations?|"
    r"competing interests?|conflicts? of interest|declarations?|funding(?: information)?|"
    r"(?:data|code|data and code|data/code) availability(?: statement)?|"
    r"availability of data and materials|ethics (?:approval|statement)|"
    r"consent (?:to participate|for publication)|online content|additional information|"
    r"supplementary (?:information|material(?:s)?)|supporting information|"
    r"publisher[’']s note|open access|correspondence(?: and requests for materials)?|"
    r"peer review information|reprints and permissions)(?:[.:]?(?:\s|$))", re.I)
MAIN_HEADING = re.compile(
    r"^(?:(?:\d+[.]?\s+)|(?:[IVX]+[.]\s+))?"
    r"(?:abstract|introduction|background|results(?: and discussion)?|discussion|"
    r"conclusions?|methods|materials and methods|methodology|experimental methods)"
    r"[.:]?$", re.I)


def _preserve_front_matter(blocks: list[dict], *, paragraph=False, first_page=1, middle_dot_names=False, asce_credentials=False, spaced_names=False, pipe_names=False) -> None:
    """保护显著大号主标题下、摘要/长正文之前的作者与机构。

    短作者名单不一定带多个逗号，不能仅依赖人数。这里联合字号、页面位置
    以及姓名/机构文字证据；不把所有短行都当成作者，也不翻译姓名推测汉字。
    没有可辨识标题层次时不据此扩大保护范围，交由其他分类证据处理。
    """
    first = [b for b in blocks if b['page'] == first_page and b['kind'] == 'body' and b['parts']]
    if len(first) < 2:
        return
    size = lambda b: median(p['size'] for p in b['parts'])
    title = max(first, key=size)
    if size(title) < median(size(b) for b in first) + 1:
        return
    lower = max(b['rect'][3] for b in first if abs(size(b) - size(title)) < .5)
    for block in sorted(first, key=lambda b: b['rect'][1]):
        if block['rect'][1] < lower:
            continue
        value = block['text']
        if (MAIN_HEADING.fullmatch(value) or paragraph and re.match(r'^(?:Abstract|Introduction)\b', value, re.I)
                or not paragraph and len(value.split()) >= 25):
            break
        # 数字、星号、匕首是常见机构/通讯上标。必须仍有两个姓名词，
        # 并限定于标题和首个正文段之间，避免吞掉正文的小标题或实体名。
        name_text = value
        if paragraph and all('text' in p for p in block['parts']):
            # 字母机构上标可能紧贴姓氏（例如Singh后接d,e），不能用
            # 普通字符串删除a–f猜姓名。只在原字号明显较小的上标处
            # 加分隔符供姓名结构识别；最终整个作者块仍保留原PDF。
            # parts 已去除各片段边缘空格；新计划在识别副本中恢复词界，
            # 防止 Qimin + Deng 被拼成 QiminDeng。旧计划保留原识别路径。
            normal = max(p['size'] for p in block['parts'])
            name_text = (' ' if spaced_names else '').join(p['text'] if p['size'] >= .85*normal else ',' for p in block['parts'])
        # ASCE 姓名后带会员资格，如 Aff.M.ASCE / M.ASCE；它不是作者
        # 姓名的一部分，只在该刊首页作者识别的临时副本中移除。
        if asce_credentials:
            name_text = re.sub(r'\s*,?\s*(?:Aff\.)?M\.ASCE\b', '', name_text)
        if pipe_names:
            name_text = name_text.replace("|", ";")
        names = re.sub(r"[\d*†‡]+", "", name_text).strip()
        names = re.split(r"\s*(?:,|;|&|·|\band\b)\s*" if middle_dot_names else r"\s*(?:,|;|&|\band\b)\s*", names)
        name_pattern = r"(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]*\s+){1,5}[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]*"
        is_name = (all(re.fullmatch(name_pattern, name.strip()) for name in names if name.strip())
                   if paragraph else all(re.fullmatch(name_pattern, name) for name in names))
        # 多作者名单可能超过25词；先验证每个分隔项都是姓名，再应用
        # 长正文停止条件。不能因作者多就猜测姓名译法或把名单送入正文。
        affiliation = re.search(r"\b(?:university|institute|department|laboratory|school of|"
                                r"faculty of|hospital|college|e-mail|email)\b|\S+@\S+", value, re.I)
        if paragraph and len(value.split()) >= 25 and not (is_name and len(names) > 1 or affiliation):
            break
        if is_name or affiliation:
            block['kind'] = 'preserve'


def _preserve_auxiliary_sections(blocks: list[dict], *, paragraph=False,
                                 visual_boundaries=False, frontiers_sidebar_page=None) -> None:
    """原位设置辅助文本分类；小标题与跨页后续内容都不发给翻译器。

    输入为已按阅读顺序排列的源块。依赖明确章节名称，不根据一般正文中
    的单词命中排除整段；短标题、粗体前缀与常见出版声明可以启动保护。
    这是自动识别规则而非任意文档的语义证明，未知版式仍受后续质量门禁约束。
    """
    auxiliary = False
    sidebar_right = None
    if frontiers_sidebar_page is not None:
        first = [b for b in blocks if b['page'] == frontiers_sidebar_page and b['parts']]
        titles = [b for b in first if b['kind'] == 'body']
        if titles:
            size = lambda b: median(p['size'] for p in b['parts'])
            title = max(titles, key=size)
            if (size(title) >= median(size(b) for b in first) + 1
                    and title['rect'][2]-title['rect'][0] > .35*title['page_width']
                    and any(b['text'].strip().upper() == 'OPEN ACCESS'
                            and b['rect'][2] < title['rect'][0] for b in first)):
                sidebar_right = title['rect'][0]
    # 新段落模式翻译摘要、致谢、数据/代码声明、贡献和利益声明等阅读内容。
    # 仅出版服务信息、作者机构和书目继续保护；旧模式保持既有缓存含义。
    preserved_heading = re.compile(
        r"^(?:references|bibliography|literature cited|author (?:information|affiliations?)|"
        r"affiliations?|publisher[’']s note|open access|correspondence|peer review information|"
        r"reprints and permissions)(?:[.:]?(?:\s|$))", re.I)
    for block in blocks:
        if (sidebar_right is not None and block['page'] == frontiers_sidebar_page
                and block['rect'][2] <= sidebar_right):
            # Frontiers' narrow publication sidebar is geometrically separate
            # from its unlabelled abstract. Its OPEN ACCESS state cannot leak
            # into the main column, or carry onto the next article page.
            if block['kind'] == 'body':
                block['kind'] = 'preserve'
                block['preserve_reason'] = 'Frontiers 首页出版信息侧栏原样保留'
            continue
        if visual_boundaries and block['kind'] in {'table','figure','formula','caption','header-footer'}:
            continue
        value = block['text']
        match = AUXILIARY_HEADING.match(value)
        # 附录可能放在致谢后、书目前，仍含需要翻译的科学论证。仅接受
        # 短的明确附录标题，不能因正文提到 Appendix 就结束辅助区保护。
        appendix = paragraph and len(value.split()) <= 18 and re.fullmatch(
            r'Appendix(?:\s+[A-Z0-9])?(?:\s*[:.\u2014\u2013-]\s*\S.*)?', value, re.I)
        if (MAIN_HEADING.fullmatch(value) or appendix or paragraph and
                re.match(r'^(?:Plain Language Summary|Abstract|Introduction)\b', value, re.I)) and block['kind'] == 'body':
            auxiliary = False
        elif match and (not value[match.end():].strip() or
                        any(AUXILIARY_HEADING.fullmatch(p['text'].strip()) for p in block['parts'][:1]) or
                        (block['parts'] and block['parts'][0]['bold']) or
                        re.match(r"^(?:Correspondence|Publisher[’']s note|Open Access|"
                                 r"Peer review information|Reprints and permissions)\b", value, re.I)):
            auxiliary = not paragraph or bool(preserved_heading.match(value))
        if auxiliary and block['kind'] == 'body':
            block['kind'] = 'preserve'


def _nonprinting_line(page: fitz.Page, line: dict, paint: dict) -> str | None:
    """识别有绘制证据的非可见文字，返回保留原因而非删除源对象。

    PDF 提取文字不等于页面可见正文。render mode 3 / 零透明度按逐字符
    绘制记录判断；白字还必须在其完整区域实测为纯白，不能丢掉深色底白字。
    未匹配绘制记录、低对比度或部分可见时都保守保留普通分类路径。
    """
    chars = [c for s in line['spans'] for c in s['chars'] if not c['c'].isspace()]
    keys = [(ord(c.get('native_text', c['c'])), round(c['origin'][0], 3), round(c['origin'][1], 3)) for c in chars]
    if keys and all(k in paint and all(paint[k]) for k in keys):
        return 'nonpainting-text'
    # PyMuPDF 版本可能返回带符号 ARGB；颜色仅取 RGB，透明度单独按绘制记录核验。
    if not chars or any(s['color'] & 0xffffff != 0xffffff for s in line['spans'] if s['chars']):
        return None
    rect = fitz.Rect(line['bbox']) & page.rect
    if rect.is_empty:
        return None
    pixels = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect,
                             colorspace=fitz.csRGB, alpha=False)
    if pixels.samples and min(pixels.samples) == 255:
        return 'white-text-on-blank-background'
    return None


def _list_marker_box(page, box):
    """排除被视觉检测器误标成独立公式的正文列表编号。

    必须同时看到完整编号和同一行后续英文正文；仅有“(1)”的公式编号
    不满足条件。检测框也不得伸入编号后的正文，避免放过真正公式。
    """
    rect = fitz.Rect(box['rect'])
    for block in page.get_text('rawdict', flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
        for line in block.get('lines', []):
            chars = [c for span in line['spans'] for c in span['chars']]
            value = ''.join(c['c'] for c in chars)
            match = re.match(r'^(\(\d{1,3}\)|\d{1,3}[.)]|[a-z][.)])\s+[A-Za-z]{2}', value)
            if not match:
                continue
            marker = fitz.Rect()
            for char in chars[:match.end(1)]:
                marker |= fitz.Rect(char['bbox'])
            if (marker.get_area() and (marker & rect).get_area() > .8 * marker.get_area()
                    and rect.x1 <= marker.x1 + 1 and rect.x0 >= marker.x0 - 1
                    and rect.y0 >= marker.y0 - 1 and rect.y1 <= marker.y1 + 1):
                return True
    return False


def normalize_traced_spaces(raw, traces, *, repeated_spaces=False):
    """将有绘制证据的控制码空格恢复为空格，不改动 PDF 或未知数学字形。

    部分 CFF PDF 的 rawdict 将空格提取为控制码，texttrace 却正确识别为
    空格。正文首字母可能与该空格同原点，因此还要核对字体和横向字框，
    且只接受唯一匹配。没有此证据的控制码仍交给原字形安全检查。
    """
    spaces = [(s['font'], c) for s in traces if tuple(s['dir']) == (1, 0)
              for c in s['chars'] if c[0] == 32 and c[1] >= 0]
    for block in raw:
        for line in block.get('lines', []):
            for span in line['spans']:
                for char in span['chars']:
                    # 可选断词符在行内可没有墨迹，texttrace 会把它报告为空格。
                    # 仍须保留 U+00AD 的语义，避免 daily-\u00adscale 被变成
                    # daily- scale 后误按行末断词合并成 dailyscale。
                    if char['c'].isprintable() or char['c'].isspace() or char['c'] == '\u00ad':
                        continue
                    matches = [c for font, c in spaces if font == span['font']
                               and all(abs(a-b) < .001 for a, b in zip(c[2], char['origin']))
                               and abs(c[3][0]-char['bbox'][0]) < .001
                               and abs(c[3][2]-char['bbox'][2]) < .001]
                    if repeated_spaces:
                        # 同一空格可能重复绘制两三次；只合并完全相同的
                        # GID、原点及字框证据，不把同位置的正文首字母混入。
                        if matches and len({c[:2] for c in matches}) == 1:
                            matches = matches[:1]
                    if len(matches) == 1:
                        char['c'] = ' '


def normalize_nonadvancing_spaces(raw):
    """去掉实际占位为零的空格；不按词典猜词，不移除有可见字距的单位间隔。

    出版辅助 U+200B 在内存解包后可能成为 space 字形，但其下一字母
    与空格同原点。该空格不产生视觉间隔，却会把 mainland 拆成 m a i n…。
    仅用同一水平行内的同原点证据；普通空格和跨行间隔均保持。
    """
    for block in raw:
        for line in block.get('lines', []):
            if tuple(line['dir']) != (1, 0):
                continue
            chars = [c for span in line['spans'] for c in span['chars']]
            discard = {id(a) for a, b in zip(chars, chars[1:])
                       if a['c'] == ' ' and not b['c'].isspace()
                       and all(abs(x-y) < .01 for x, y in zip(a['origin'], b['origin']))}
            for span in line['spans']:
                span['chars'] = [c for c in span['chars'] if id(c) not in discard]


def _preserve_vector_fractions(source, blocks, *, include_label=False, glyph_structure=False):
    """有原生横线的短行内分式整体原位保留，不能只让字形随中文流动。

    输入是已分类的标准文本块。要求细短水平矢量线、紧贴线两侧的数学
    字形、相同横向范围共同提供证据；普通下划线和表格线不满足此条件。
    不猜测分式含义，不重画横线，分子分母和源路径继续接受保护区核验。
    """
    def slice_part(part, start, end):
        chars = part['chars'][start:end]
        bounds = fitz.Rect(chars[0]['rect'])
        for char in chars[1:]:
            bounds |= fitz.Rect(char['rect'])
        return {**part, 'chars': chars, 'text': ''.join(c['text'] for c in chars),
                'rect': list(bounds)}

    detached = []
    with open_source_pdf(source) as document:
        for number, page in enumerate(document, 1):
            candidates = [(b, p) for b in blocks if b['page'] == number
                          and b['kind'] in {'body', 'caption'} for p in b['parts'] if p['fixed']]
            for drawing in page.get_drawings():
                r = fitz.Rect(drawing['rect'])
                if (drawing['type'] != 's' or len(drawing['items']) != 1
                        or drawing['items'][0][0] != 'l' or r.height > .01
                        or not 5 < r.width < 100 or drawing['width'] > 1):
                    continue
                slices = []
                if glyph_structure:
                    # 分子可与 LMF = 或其他文字合成宽锚点；按横线内的真实
                    # 字形截取，不能放宽整个锚点左界而把邻近正文一并保护。
                    selected = []
                    for block, part in candidates:
                        if part not in block['parts']:
                            continue
                        chars = part['chars']
                        indices = [i for i, c in enumerate(chars) if c['text'].strip()
                                   and r.x0-1 <= c['rect'][0] and c['rect'][2] <= r.x1+1]
                        # 距横线近的基字只负责确认此数学 run 属于分式。
                        # _parts 已把同一物理行的相连数学字形绑定为 run；
                        # 一旦确认就保留横线范围内的整个 run，包括低位尾下标，
                        # 不能按每个字符到横线的距离再次截断分子或分母。
                        if not any(abs(chars[i]['origin'][1]-r.y0) <
                                   max(part['size'],chars[i]['rect'][3]-chars[i]['rect'][1])
                                   for i in indices):
                            continue
                        start, end = min(indices), max(indices)+1
                        prefix = ''.join(c['text'] for c in chars[:start])
                        label = re.search(r'(?<!\w)(?:[A-Z]{2,8}\s*)?=\s*$', prefix)
                        if include_label and label:
                            offsets = [0]
                            for char in chars[:start]:
                                offsets.append(offsets[-1]+len(char['text']))
                            if label.start() in offsets:
                                first = offsets.index(label.start())
                                label_chars = [c for c in chars[first:start] if c['text'].strip()]
                                if max(c['origin'][1] for c in label_chars)-min(c['origin'][1] for c in label_chars) < 1:
                                    start = first
                        anchor = slice_part(part, start, end)
                        selected.append((block, anchor))
                        slices.append((block, part, start, end, anchor))
                else:
                    selected = [(b, p) for b, p in candidates
                                if p in b['parts'] and p['rect'][0] >= r.x0-3*p['size']
                                and p['rect'][2] <= r.x1+1
                                and p['rect'][2] > r.x0
                                and abs(p['baseline']-r.y0) < 1.2*p['size']]
                # 必须实际有字形基线位于横线上下，且不是两条普通正文行。
                above = [p for _, p in selected if any(
                    r.x0-1 <= c['origin'][0] <= r.x1 and 0 < r.y0-c['origin'][1] < p['size']
                    for c in p['chars'] if c['text'].strip())]
                below = [p for _, p in selected if any(
                    r.x0-1 <= c['origin'][0] <= r.x1 and 0 < c['origin'][1]-r.y0 < p['size']
                    for c in p['chars'] if c['text'].strip())]
                if not above or not below or (not glyph_structure and len(selected) < 2):
                    continue
                if glyph_structure:
                    def embedded_in_prose(part):
                        block = next(b for b, p in selected if p is part)
                        neighbors = [p for p in block['parts'] if not p['fixed']
                                     and re.search(r'[A-Za-z]{2,}', p['text'])
                                     and abs(p['baseline']-part['baseline']) < .15*part['size']
                                     and abs(p['size']-part['size']) < .1*part['size']]
                        return (any(p['rect'][0] < part['rect'][0]
                                    and abs(part['rect'][0]-p['rect'][2]) < part['size'] for p in neighbors)
                                and any(p['rect'][2] > part['rect'][2]
                                        and abs(p['rect'][0]-part['rect'][2]) < part['size'] for p in neighbors))
                    # Two variables embedded in ordinary prose on both baselines
                    # are not a numerator/denominator. Keep their movable anchors;
                    # an underline between the lines must not remove either variable
                    # from the sentences sent for translation.
                    if all(embedded_in_prose(p) for p in above+below):
                        continue
                for block, part, start, end, anchor in slices:
                    index = block['parts'].index(part)
                    before = [slice_part(part, 0, start)] if start else []
                    after = [slice_part(part, end, len(part['chars']))] if end < len(part['chars']) else []
                    block['parts'][index:index+1] = before + [anchor] + after
                    candidates.extend((block, p) for p in before+after)
                if include_label:
                    # 紧邻等号的 LMF 等拉丁标识与分式一起保留，避免中文
                    # 段落变短后“LMF”和等号相隔数行。仅拆出同基线的
                    # 全大写词尾，前面的自然语言仍交给翻译，不猜变量名。
                    for block, anchor in list(selected):
                        if not anchor['text'].lstrip().startswith('='):
                            continue
                        index = block['parts'].index(anchor)
                        if index == 0:
                            continue
                        previous = block['parts'][index-1]
                        raw = ''.join(c['text'] for c in previous['chars'])
                        match = re.search(r'\b[A-Z]{2,8}$', raw)
                        equals = next((c for c in anchor['chars'] if c['text'] == '='), None)
                        if (previous['fixed'] or not match or not equals
                                or abs(previous['baseline']-equals['origin'][1]) > 1
                                or abs(previous['rect'][2]-anchor['rect'][0]) > 1):
                            continue
                        if match.start() == 0:
                            previous['fixed'] = True
                            selected.insert(0, (block, previous))
                            continue
                        label = {**previous, 'fixed': True, 'text': match.group(),
                                 'chars': previous['chars'][match.start():]}
                        box = fitz.Rect(label['chars'][0]['rect'])
                        for char in label['chars'][1:]:
                            box |= fitz.Rect(char['rect'])
                        label['rect'] = list(box)
                        previous['chars'] = previous['chars'][:match.start()]
                        previous['text'] = raw[:match.start()]
                        box = fitz.Rect(previous['chars'][0]['rect'])
                        for char in previous['chars'][1:]:
                            box |= fitz.Rect(char['rect'])
                        previous['rect'] = list(box)
                        block['parts'].insert(index, label)
                        selected.insert(0, (block, label))
                parts = [p for _, p in selected]
                bounds = fitz.Rect(parts[0]['rect'])
                for part in parts[1:]:
                    bounds |= fitz.Rect(part['rect'])
                if glyph_structure:
                    bounds |= fitz.Rect(r.x0, r.y0-max(.01, drawing['width']/2),
                                        r.x1, r.y1+max(.01, drawing['width']/2))
                value = ' '.join(p['text'] for p in parts)
                template = selected[0][0]
                detached.append({**template, 'id': segment_id(json.dumps([number, list(bounds), value])),
                                 'kind': 'formula', 'rect': list(bounds), 'text': value, 'parts': parts})
                for block, part in selected:
                    block['parts'].remove(part)
                    block['text'] = _plain(' '.join(p['text'] for p in block['parts']))
    blocks[:] = [b for b in blocks if b['parts']] + detached


def _detach_formula_openers(blocks):
    """把误挂在正文末尾的独立开括号归回相邻公式；原字符和坐标保持不变。"""
    # 原生块可把下一行公式的左括号挂到上一段末尾。若该独立左括号
    # 紧接同页的固定公式，原位保留它，不能把它移到中文段落末端。
    # 只接纳纯开括号和横向相邻证据，不扩大到普通文字或完整行内式。
    formulas = [b for b in blocks if b['kind'] == 'formula']
    detached = []
    for body in blocks:
        if body['kind'] != 'body' or len(body['parts']) < 2:
            continue
        part = body['parts'][-1]
        if not part['fixed'] or not re.fullmatch(r'[({\[]', part['text'].strip()):
            continue
        a = fitz.Rect(part['rect'])
        if not any(f['page'] == body['page'] and 0 <= f['rect'][0]-a.x1 < .5*part['size']
                   and min(a.y1,f['rect'][3]) > max(a.y0,f['rect'][1]) for f in formulas):
            continue
        body['parts'] = body['parts'][:-1]
        # parts 已是标准化文本 run，坐标键为 rect；原始 chars 才使用 bbox。
        # 去掉独立括号后用剩余 run 的边界重算正文框，不能沿用含公式的旧框。
        bounds = fitz.Rect(body['parts'][0]['rect'])
        for remaining_part in body['parts'][1:]:
            bounds |= fitz.Rect(remaining_part['rect'])
        body['rect'] = list(bounds)
        body['text'] = _plain(' '.join(p['text'] for p in body['parts']))
        detached.append({**body, 'id': segment_id(json.dumps([part['page'], part['rect'], part['text']])),
                         'kind': 'formula', 'parts': [part], 'rect': part['rect'], 'text': part['text']})
    blocks.extend(detached)


def extract_layout(source: Path, detections: list[dict] | None = None, *, paragraph: bool = False, ocr_dir: Path | None = None, journal_adapt: bool = True, section_boundaries: bool = True, reference_line_starts: bool = True, content_geometry: bool = True, corpus_evidence: bool = True, symbol_revision: int = 4, frontmatter_after_cover: bool = True, publisher_font_styles: bool = True, nonadvancing_spaces: bool = True, extended_citations: int = 2, middle_dot_names: bool = True, publisher_layout: bool = True, italic_words: bool = True, formula_openers: bool = True, dash_lists: bool = True, spaced_names: bool = True, vector_fractions: bool = True, fraction_labels: bool = True, fraction_structure: bool = True, short_prose: bool = True, reading_structure: int = 5, script_geometry: int = 2) -> dict:
    """收集所有可见文本块，自动分类并形成跨区域逻辑段落断点。

    图表矩形与书目通过源文几何提取；扫描页/旋转文字不猜测性 OCR。
    每块均保留明确分类，不能把未识别正文静默算作已翻译。
    """
    # preserved 旧断点逐字核对旧自动计划；修复只用于新的段落模式，
    # 避免改变已发布旧引擎的保护区域和缓存身份。
    corpus_evidence = corpus_evidence and paragraph
    greek_variants = paragraph and symbol_revision >= 1
    caption_pattern = (APPENDIX_CAPTION if reading_structure >= 3 else CAPTION) if paragraph else LEGACY_CAPTION
    _, _, _, references = _reference_geometry(source, supplementary_boundary=paragraph and reading_structure,
                                               styled_boundaries=paragraph and reading_structure >= 2,
                                               visual_exclusions=[r for r in (detections or []) if r['kind'] in {'table','figure'}]
                                               if paragraph and reading_structure >= 5 else None)
    blocks, protected, issues = [], [], []
    from .journals import identify_journal, auxiliary_page, preserve_line, publisher_style_flags
    journal = identify_journal(source) if paragraph and journal_adapt else None
    symbol_cache = {}
    # 版面计划沿用源描述符坐标，保持已有段落身份与译文缓存；只有实际
    # 删除及安全删除范围计算使用修正后的临时描述符，不能混入持久计划。
    document, _ = open_source_for_editing(source, normalize_descriptors=False, normalize_rotation=corpus_evidence)
    with document:
        from .source_watermarks import background_layers
        watermarks = background_layers(document) if paragraph and corpus_evidence else {}
        for number, page in enumerate(document, 1):
            cover_reason = auxiliary_page(page.get_text(), number) if journal else None
            if page.rotation or list(page.annots(types=(fitz.PDF_ANNOT_REDACT,)) or []):
                issues.append(f'第{number}页旋转或已有删除标注，尚不受自动源版面模式支持')
            images = visible_image_regions(page, detections or [])
            boxes = list(images)
            boxes.extend({'page': number, 'rect': list(t.bbox), 'kind': 'table'}
                         for t in page.find_tables().tables)
            boxes.extend({**r, 'kind': 'reference'} for r in references if r['page'] == number)
            visual_kinds = {'figure': 'figure', 'table': 'table', 'isolate_formula': 'formula',
                            'formula_caption': 'formula', 'figure_caption': 'caption',
                            'table_caption': 'caption', 'abandon': 'header-footer'}
            boxes.extend({'page': number, 'rect': r['rect'], 'kind': visual_kinds[r['kind']]}
                         for r in (detections or []) if r['page'] == number and r['kind'] in visual_kinds)
            # PDF 允许图片外框超出 CropBox；可见保护区域必须裁切到页面。
            # 但透明图片的大外框可能同时覆盖正文，不能因裁切合法就认定为图内文字。
            boxes = [{**box, 'rect': list(fitz.Rect(box['rect']) & page.rect)} for box in boxes
                     if not (fitz.Rect(box['rect']) & page.rect).is_empty]
            if paragraph:
                boxes = [b for b in boxes if b['kind'] != 'formula' or not _list_marker_box(page, b)]
            figure_regions = [fitz.Rect(r['rect']) for r in (detections or [])
                              if r['page'] == number and r['kind'] in {'figure', 'table'}]
            for region in (detections or []):
                if region['page'] != number or region['kind'] != 'plain text':
                    continue
                text_rect = fitz.Rect(region['rect'])
                if text_rect.is_empty:
                    continue
                if METADATA.match(_plain(page.get_textbox(text_rect))):
                    continue
                covered_by_image = any((text_rect & fitz.Rect(i['rect'])).get_area() > .5 * text_rect.get_area()
                                       for i in images)
                covered_by_figure = any((text_rect & r).get_area() > .5 * text_rect.get_area() for r in figure_regions)
                # 无文字的图片也可能是扫描正文，不能一概忽略。只有首页
                # 紧随收稿/接收日期、同栏同左边缘的小出版信息图片可保留。
                badge = corpus_evidence and number == 1 and not page.get_textbox(text_rect).strip() and any(
                    re.match(r'^(?:Received|Accepted)\s*:', b[4], re.I)
                    and abs(b[0] - text_rect.x0) < 2 and text_rect.x1 < page.rect.width / 2
                    and 0 <= text_rect.y0 - b[3] <= 3 * (b[3] - b[1])
                    # 位图文本框包含上下留白，可略高于相邻原生日期字框；
                    # 上限仍只容纳单行出版信息，不接受多行扫描正文。
                    and text_rect.height <= 1.5 * (b[3] - b[1])
                    for b in page.get_text('blocks') if b[6] == 0)
                if covered_by_image and not covered_by_figure and not badge:
                    issues.append(f'第{number}页图片外框覆盖自动识别的正文，尚不能自动区分透明图片与图内文字')
                    break
            if paragraph:
                # AGU 的书目总框横跨页面，可能罩住左侧独立致谢栏。只有
                # 看到明确 References 标题和完全位于其左侧的辅助章节，
                # 才从书目保护框扣除该原生块；不能把任意邻栏当成正文。
                native_blocks = page.get_text('blocks')
                reference_heads = [fitz.Rect(b[:4]) for b in native_blocks
                                   if b[6] == 0 and re.match(r'^References\s*$', b[4].strip(), re.I)]
                auxiliary = [fitz.Rect(b[:4]) for b in native_blocks if b[6] == 0
                             and AUXILIARY_HEADING.match(b[4].strip())
                             and not re.match(r'^(?:References|Bibliography)\b', b[4].strip(), re.I)
                             and any(b[2] < h.x0 and abs(b[1]-h.y0) < 2*(h.height+1)
                                     for h in reference_heads)]
                if auxiliary:
                    holes = [fitz.Rect(r.x0-.5,r.y0-.5,r.x1+.5,r.y1+.5) for r in auxiliary]
                    boxes = [piece for box in boxes for piece in (
                        [{**box, 'rect': list(r)} for r in writing_rectangles(box['rect'], holes)]
                        if box['kind'] == 'reference' else [box])]
            if paragraph and reading_structure >= 2:
                auxiliary_reading = []
                for native in page.get_text('dict')['blocks']:
                    lines = native.get('lines', [])
                    if not lines or not lines[0]['spans']:
                        continue
                    first_span = lines[0]['spans'][0]
                    label = first_span['text'].strip().rstrip(':.' if reading_structure >= 3 else ':')
                    if (_bold(first_span, paragraph=True) and re.fullmatch(
                            r'Acknowledg(?:e)?ments|Author contributions|Competing interests|'
                            r'Data availability|Code availability', label, re.I) or
                            reading_structure >= 3 and _bold(first_span, paragraph=True) and
                            len(label.split()) <= 18 and re.fullmatch(r'Appendix\s+[A-Z0-9]+[:.]\s+.+', label)):
                        auxiliary_reading.append(fitz.Rect(native['bbox']))
                if auxiliary_reading:
                    holes = [fitz.Rect(r.x0-.5, r.y0-.5, r.x1+.5, r.y1+.5) for r in auxiliary_reading]
                    boxes = [piece for box in boxes for piece in (
                        [{**box, 'rect': list(r)} for r in writing_rectangles(box['rect'], holes)]
                        if box['kind'] == 'header-footer' else [box])]
            protected.extend(b for b in boxes if not paragraph or b['kind'] != 'caption')
            # 同一坐标可能有不可见OCR层与可见文字重叠：只有全部绘制记录
            # 都不着色才认定为非打印对象，不能因一份隐藏副本而排除可见正文。
            paint = {}
            for span in page.get_texttrace():
                hidden = span['type'] == 3 or span['opacity'] == 0
                for code, _, origin, _ in span['chars']:
                    paint.setdefault((code, round(origin[0], 3), round(origin[1], 3)), []).append(hidden)
            raw = page.get_text('rawdict')['blocks']
            if publisher_font_styles and paragraph and journal and (journal['family'] == 'asce' or
                    reading_structure >= 2 and journal['family'] == 'science'):
                # ASCE 的 AdvOT 子集字体 .BI / .B / .I 明示字重，但 PDF flags
                # 可能只写 serif。恢复该刊明确命名的样式，防止 RF 等小标题
                # 与下方正文连成一段、把中文正文塞进仅两字宽的标题框。
                # 只改内存中的分类证据；源文件和字形对象保持不变。
                for native in raw:
                    for line in native.get('lines', []):
                        for span in line['spans']:
                            span['flags'] = publisher_style_flags(journal['family'], span['font'], span['flags'],
                                                                       science_styles=reading_structure >= 2)
            if number in watermarks:
                # 显式 /Artifact /Watermark 背景单独保留原绘制层；仅移出
                # 与隔离层字符逐项一致的原生行，不能用字符串忽略正文。
                keys = set(watermarks[number]['characters'])
                raw = [{**b, 'lines': [line for line in b.get('lines', [])
                    if not all((c['c'], tuple(c['origin'])) in keys
                               for span in line['spans'] for c in span['chars'])]}
                       if 'lines' in b else b for b in raw]
            normalize_traced_spaces(raw, page.get_texttrace(), repeated_spaces=corpus_evidence)
            if paragraph and nonadvancing_spaces:
                normalize_nonadvancing_spaces(raw)
            if paragraph:
                from .source_symbols import restore_source_symbols
                restore_source_symbols(page, raw, symbol_cache)
                if corpus_evidence:
                    from .source_symbols import restore_verified_symbols
                    restore_verified_symbols(page, raw, symbol_cache, printable_math=symbol_revision >= 3, latin_math=symbol_revision >= 4)
            text_blocks = []
            for native in raw:
                if not native.get('lines'):
                    continue
                if paragraph:
                    # 一个视觉行可能因上下标被MuPDF拆为多个“行”，而
                    # 这些片段仍在同一原生块内。只合并同一水平行带、
                    # 从左向右接续的片段，保持原字符顺序及原坐标；真正
                    # 下一正文行或反向开始的分式行不合并。
                    lines = []
                    for line in native['lines']:
                        last = lines[-1] if lines else None
                        a, b = fitz.Rect(last['bbox']) if last else fitz.Rect(), fitz.Rect(line['bbox'])
                        size = max(s['size'] for s in line['spans'])
                        old_size = max((s['size'] for s in last['spans']), default=0) if last else 0
                        baseline = max(s['origin'][1] for s in line['spans'])
                        old_baseline = max(s['origin'][1] for s in last['spans']) if last else 0
                        previous_text = ''.join(c['c'] for s in last['spans'] for c in s['chars']) if last else ''
                        if (last and tuple(line['dir']) == tuple(last['dir']) == (1,0)
                                and re.search(r'[A-Za-zα-ωΑ-Ω]', previous_text)
                                # 悬挂编号不是公式片段；保持独立，后续提取
                                # 才能保留编号与正文的间隔及列表缩进。
                                and not re.fullmatch(r'(?:\(\d{1,3}\)|\d{1,3}[.)]|[a-z][.)])', previous_text.strip())
                                and (a.x1-max(size, old_size) <= b.x0 <= a.x1+max(size, old_size) and b.x0 > a.x0
                                     # p 的长上标后，r 下标会退回到 p 右侧，不能
                                     # 按横向倒退拆成独立公式。只接纳小字号、位于
                                     # 已有行带内部的低位片段；分式/下一正文行不合并。
                                     or size < .8*old_size and a.x0 < b.x0 < a.x1
                                     and b.y0 > a.y0 and b.y1 < a.y1+.6*old_size) and
                                min(a.y1,b.y1) > max(a.y0,b.y0) and
                                abs(baseline-old_baseline) < .6*max(size, old_size)):
                            last['spans'].extend(line['spans'])
                            last['bbox'] = list(a | b)
                        else:
                            lines.append({**line,'spans':list(line['spans'])})
                    native = {**native,'lines':lines}
                split = []
                native_text = _plain(' '.join(''.join(c['c'] for s in line['spans'] for c in s['chars'])
                                             for line in native['lines']))
                # 独立符号行可能用错误Unicode表示圆点（例如提取为“&”）。
                # 依据同行、左侧悬挂位置识别标记并原位保留字形，不猜测
                # 字符编码；其后的正文明确作为新列表项开始。
                markers, list_starts = set(), set()
                if paragraph:
                    for index, candidate in enumerate(native['lines'][:-1]):
                        value = ''.join(c['c'] for s in candidate['spans'] for c in s['chars']).strip()
                        following = native['lines'][index+1]
                        next_text = ''.join(c['c'] for s in following['spans'] for c in s['chars'])
                        a, b = fitz.Rect(candidate['bbox']), fitz.Rect(following['bbox'])
                        if (len(value) == 1 and not value.isalnum() and re.match(r'[A-Za-z]', next_text)
                                and abs(a.y1-b.y1) < .5*b.height and .7*b.height < b.x0-a.x0 < 4*b.height):
                            markers.add(index)
                            list_starts.add(index+1)
                for index, line in enumerate(native['lines']):
                    lr = fitz.Rect(line['bbox'])
                    # 出版社常在短页眉后填充几百个空格，使原生行框横跨
                    # 全页。分类仅看非空字符的外框，否则明确识别的页眉
                    # 会因面积占比过小被误作正文。原生坐标、parts 和缓存
                    # 身份仍保留；旧计划按版本开关重放旧分类，不能静默变更。
                    visible_chars = [c for s in line['spans'] for c in s['chars'] if c['c'].strip()]
                    classification_rect = fitz.Rect(_rect_union(visible_chars)) if paragraph and content_geometry and visible_chars else lr
                    covers = [box for box in boxes if classification_rect.get_area() > 0 and
                              (classification_rect & fitz.Rect(box['rect'])).get_area() / classification_rect.get_area() > .5]
                    role = covers[0]['kind'] if covers else 'body'
                    if corpus_evidence and role == 'body':
                        # 数学字体的全局框可能比墨迹高三倍；显式公式内的
                        # 字符基线比整行外框面积更可靠。仅接纳全部基线在
                        # 同一公式框内的行，不扩大公式框或吸收邻近正文。
                        if visible_chars and any(box['kind'] == 'formula' and all(
                                fitz.Point(c['origin']) in fitz.Rect(box['rect'])
                                for c in visible_chars) for box in boxes):
                            role = 'formula'
                        # 预印本的公式与右侧编号可能同一原生行，中间填满
                        # 空格，视觉检测仅识别编号。要求明确赋值、数学字体
                        # 和无自然语言词的独立数学行，保留整条原公式。
                        line_text = ''.join(c['c'] for c in visible_chars)
                        words = re.findall(r'[A-Za-z]+', line_text)
                        if (re.match(r'^[A-Z]{2,6}=', line_text) and
                                re.search(r'[−≤≥∑𝜑𝑃𝑋𝑌]', line_text) and
                                all(len(w) <= 2 or w.isupper() and len(w) <= 6 for w in words)):
                            role = 'formula'
                    # 检测器可能把图上沿的面板标题同时标成图注。没有明确
                    # Figure/Table 标签、且全部字符基线位于图内时保留原图；
                    # 不扩大图框，不把图外无标签图注一概排除。
                    if role == 'caption' and paragraph:
                        line_text = ''.join(c['c'] for s in line['spans'] for c in s['chars'])
                        origins = [c['origin'] for s in line['spans'] for c in s['chars']
                                   if not c['c'].isspace()]
                        if (origins and not caption_pattern.match(_plain(line_text))
                                and any(all(region.contains(fitz.Point(origin)) for origin in origins)
                                        for region in figure_regions)):
                            role = 'figure'
                    if index in markers and role == 'body':
                        role = 'preserve'
                    reason = _nonprinting_line(page, line, paint) if role == 'body' else None
                    journal_reason = preserve_line(journal, page, line, cover_reason, publication_labels=reading_structure >= 2) if journal else None
                    reason = journal_reason or reason
                    if reason:
                        role = 'preserve'
                    # 同一个 PDF 文本块可能先有两行大号粗体章节标题，再接
                    # 小号正文。按普通字符加权的字号/字重分段，防止联合翻译
                    # 后把正文填入标题行。图注的粗体前缀仍与整条图注一起保护。
                    style_chars = [(s['size'], _bold(s, paragraph=paragraph)) for s in line['spans']
                                   if not _superscript(s, line, paragraph=paragraph)
                                   for c in s['chars'] if not c['c'].isspace()]
                    style = (median(s[0] for s in style_chars),
                             sum(s[1] for s in style_chars) > len(style_chars) / 2) if style_chars else (0, False)
                    if reading_structure >= 4:
                        style += (all(s['flags'] & 2 for s in line['spans']),)
                    changed = (split and role == 'body' and not caption_pattern.match(native_text) and
                               (abs(split[-1]['_style'][0] - style[0]) > .6 or split[-1]['_style'][1] != style[1]
                                or reading_structure >= 4 and style[1] and split[-1]['_style'][2] != style[2]))
                    paragraph_break = False
                    if paragraph and split and role == 'body' and not caption_pattern.match(native_text):
                        from .paragraph_layout import starts_paragraph
                        paragraph_break = starts_paragraph(native['lines'], split[-1]['lines'], line, dash_lists=dash_lists, structured_headings=reading_structure >= 2)
                    if not split or paragraph_break or index in list_starts or split[-1]['_role'] != role or split[-1]['_reason'] != reason or changed:
                        split.append({'lines': [], 'bbox': list(lr), '_role': role, '_style': style,
                                      '_reason': reason, '_list_start': index in list_starts})
                    split[-1]['lines'].append(line)
                    split[-1]['bbox'] = list(fitz.Rect(split[-1]['bbox']) | lr)
                text_blocks.extend(split)
            if not text_blocks:
                issues.append(f'第{number}页没有可提取正文，请核对扫描页或纯图页')
            for block in text_blocks:
                rect = fitz.Rect(block['bbox'])
                value = _plain(' '.join(''.join(c['c'] for s in l['spans'] for c in s['chars'])
                                       for l in block['lines']))
                if not value:
                    continue
                kind = block['_role']
                if kind == 'body' and caption_pattern.match(value):
                    kind = 'caption'
                elif kind == 'body' and re.fullmatch(r'(?:https?://|www\.)\S+', value):
                    # 独立网址（含句末标点）无需翻译。不能只留下网址对象、
                    # 却将其 2 pt 的句点当成正文用中文字体重新排字。
                    kind = 'preserve'
                elif kind == 'body' and not re.search(r'[A-Za-z]{2}', value) and not (
                        paragraph and re.fullmatch(r'(?:18|19|20)\d0s', value)):
                    kind = 'formula'
                elif kind == 'body' and (rect.y1 < 35 or rect.y0 > page.rect.height - 30):
                    kind = 'header-footer'
                elif kind == 'body' and any(tuple(l['dir']) != (1, 0) for l in block['lines']):
                    # 先排除有证据的符号、网址、页眉脚注；真正旋转的自然
                    # 语言正文仍需等待支持，不能把所有旋转文字都静默透传。
                    kind = 'review'
                # 真实 Nature 论文有两段 References 标题与书目合并的块；
                # 使用明确标题或多条编号+年份证据，不把任意数字段落当书目。
                # 编号必须出现在原生行首。拼接后匹配任意空格会把正文中的
                # “−2. These … Fig. 6. Except …”当成两条文献，导致漏译。
                if paragraph and reference_line_starts:
                    numbered_entries = sum(bool(re.match(r'^\s*\d{1,3}\.\s+[A-Z]',
                        ''.join(c['c'] for s in line['spans'] for c in s['chars'])))
                        for line in block['lines'])
                else:
                    numbered_entries = len(re.findall(r'(?:^|\s)\d{1,3}\.\s+[A-Z]', value))
                if (not paragraph or reading_structure < 5 or kind in {'body','reference'}) and (
                        re.match(r'^References(?:\s|$)', value, re.I) or (
                        numbered_entries >= 2 and
                        len(re.findall(r'\b(?:19|20)\d{2}\b', value)) >= 2)):
                    kind = 'reference'
                if METADATA.match(value):
                    kind = 'header-footer'
                words = re.findall(r'[A-Za-z]+', value)
                if (number == 1 and rect.y1 < page.rect.height / 2 and value.count(',') >= 4
                        and len(words) > 8 and sum(w[0].isupper() for w in words) / len(words) > .8):
                    kind = 'preserve'
                parts = _parts(block, number, page.get_links(), paragraph=paragraph, greek_variants=greek_variants, extended_citations=paragraph and extended_citations, italic_words=paragraph and italic_words, short_prose=paragraph and short_prose, script_geometry=paragraph and script_geometry)
                # 只有原位数学/引用锚点的片段没有可翻译正文，直接保护；
                # 例如被出版社拆成独立文本块的行内下标表达式。
                if kind == 'body' and parts and all(p['fixed'] for p in parts):
                    kind = 'formula'
                # 出版社将行内公式拆为独立块，分号/括号仍可能使用正文字体。
                # 只有确定锚点和数学标点、没有任何可翻译词的块整体保留；
                # 不把带“where/and”等解释文字的句子误归为公式。
                if (paragraph and symbol_revision >= 2 and kind == 'body' and parts
                        and any(p['fixed'] for p in parts)
                        and all(p['fixed'] or re.fullmatch(r'[\s,;:().\[\]{}]+', p['text']) for p in parts)):
                    kind = 'formula'
                if paragraph and number == 1 and (
                        re.match(r'^(?:Received|Accepted)\s+\d', value) or
                        re.match(r'^[A-Z][A-Za-z-]+,\s+[A-Z]\.', value) and
                        re.search(r'\(20\d{2}\)', value) and 'doi.' in value.lower()):
                    kind = 'preserve'
                if kind in ({'body', 'caption'} if paragraph else {'body'}):
                    from .local_ocr import anomalous_lines, diagnose_lines
                    anomalies = anomalous_lines([block])
                    if anomalies:
                        if ocr_dir is not None:
                            # 每块单独目录避免同页多块的局部截图互相覆盖。
                            diagnose_lines(page, anomalies, ocr_dir / f'block-{len(blocks):04d}')
                        if not corpus_evidence:
                            issues.append(f'第{number}页待译区域有未解决的异常编码；需核对局部 OCR 与源字形')
                # ASCE 下载水印的字体外框可伸出 CropBox 约 1 pt。它已由
                # 刊名、边缘位置、出版关键词共同识别为原样保留对象；仅把
                # 保护外框裁到可见页内，原生字形及 parts 坐标均不改写。
                if (journal and block['_reason'] and kind == 'preserve' or
                        corpus_evidence and kind in {'figure', 'table', 'reference', 'formula', 'header-footer', 'preserve'}):
                    visible = rect & page.rect
                    if not visible.is_empty:
                        rect = visible
                identity = segment_id(json.dumps([number, list(rect), value], ensure_ascii=False))
                blocks.append({'id': identity, 'page': number, 'rect': list(rect),
                               'text': value, 'parts': parts, 'kind': kind,
                               'list_start': block['_list_start'],
                               'preserve_reason': block['_reason'],
                               'italic': all(s['flags'] & 2 for l in block['lines'] for s in l['spans']),
                               'heading': bool(paragraph and re.match(r'^[a-z]\.\s+[A-Z]', value) and
                                   all(s['flags'] & 2 for l in block['lines'] for s in l['spans']) or
                                   paragraph and publisher_layout and journal and journal['family'] == 'asce'
                                   and kind == 'body' and value in {'Results', 'Discussion', 'Conclusions'}),
                               'page_width': page.rect.width})
    if corpus_evidence:
        # PDF 可把行内分式的横线和分母另写成原生块。分子已作为行内
        # 锚点时，必须一起删除/重放这些紧贴的数学片段；否则固定横线的
        # 全局字框会挡住分子的删除范围。仅合并被单个锚点横向包住、
        # 与之相交、无独立视觉公式框、且不含自然语言的短数学块。
        absorbed = set()
        for body in blocks:
            if body['kind'] not in {'body', 'caption'}:
                continue
            for anchor in body['parts']:
                if not anchor['fixed']:
                    continue
                bounds = fitz.Rect(anchor['rect'])
                for formula in blocks:
                    if (formula['id'] in absorbed or formula['kind'] != 'formula'
                            or formula['page'] != body['page']):
                        continue
                    r = fitz.Rect(formula['rect'])
                    if (r.x0 < bounds.x0-1 or r.x1 > bounds.x1+1 or not r.intersects(bounds)
                            or any(len(w) > 2 for w in re.findall(r'[A-Za-z]+', formula['text']))
                            or any(p['page'] == body['page'] and p['kind'] == 'formula'
                                   and fitz.Rect(p['rect']).intersects(r) for p in protected)):
                        continue
                    anchor['chars'] = anchor['chars'] + [c for part in formula['parts'] for c in part['chars']]
                    anchor['text'] += '\n' + formula['text']
                    anchor['rect'] = list(fitz.Rect(anchor['rect']) | r)
                    absorbed.add(formula['id'])
        blocks = [b for b in blocks if b['id'] not in absorbed]
    if paragraph and vector_fractions:
        _preserve_vector_fractions(source, blocks, include_label=fraction_labels,
                                   glyph_structure=fraction_structure and fraction_labels)
    if paragraph and formula_openers:
        _detach_formula_openers(blocks)
    # 期刊图注也会分成左右两栏；视觉模型有时只识别带 Fig. 标题的一栏。
    # 将同页、同字号、横向相邻且垂直带重合的另一栏归入图注，避免把它
    # 拼进跨页正文。该几何证据不延伸到下方独立正文，也不要求人工改计划。
    captions = [b for b in blocks if b['kind'] == 'caption']
    for body in blocks:
        if body['kind'] != 'body' or not body['parts']:
            continue
        rect = fitz.Rect(body['rect'])
        for caption in captions:
            if caption['page'] != body['page'] or not caption['parts']:
                continue
            other = fitz.Rect(caption['rect'])
            overlap = min(rect.y1, other.y1) - max(rect.y0, other.y0)
            same_size = abs(median(p['size'] for p in body['parts']) -
                            median(p['size'] for p in caption['parts'])) < .25
            adjacent = rect.x0 >= other.x1 or other.x0 >= rect.x1
            if adjacent and same_size and overlap > .5 * min(rect.height, other.height):
                body['kind'] = 'caption'
                break
    # 页内先判断是否确有双栏，再按栏阅读。通栏标题必须与下方双栏分开；
    # 这仍是自动排序启发式，不代表任意版式的语义完整性已得到证明。
    blocks.sort(key=lambda b: (b['page'], int(b['rect'][0] >= b['page_width'] / 2), b['rect'][1]))
    # 机构封面可占 PDF 第1页，真正的文章首页在第2/3页。只沿用已有
    # 姓名/机构证据和标题边界，不能把“第1个PDF页面”等同于文章首页。
    first_article = min((b['page'] for b in blocks if b['kind'] == 'body'), default=1) if paragraph and frontmatter_after_cover else 1
    _preserve_front_matter(blocks, paragraph=paragraph, first_page=first_article, middle_dot_names=middle_dot_names,
                           asce_credentials=publisher_layout and journal and journal['family'] == 'asce',
                           spaced_names=spaced_names, pipe_names=reading_structure)
    _preserve_auxiliary_sections(blocks, paragraph=paragraph,
        visual_boundaries=paragraph and reading_structure >= 5,
        frontiers_sidebar_page=first_article if paragraph and reading_structure >= 5
        and journal and journal['family'] == 'frontiers' else None)
    if paragraph:
        if corpus_evidence:
            # 作者、通讯标记等在最终前置信息分类后已原样保留，不能继续
            # 使用分类前的正文乱码诊断阻断整篇；未知正文仍逐块明确报错。
            for block in blocks:
                if block['kind'] in {'body', 'caption'} and any(
                        c == '\ufffd' or not c.isprintable() and not c.isspace() and c != '\u00ad'
                        for c in block['text']):
                    issues.append(f'第{block["page"]}页待译区域有未解决的异常编码；需核对局部 OCR 与源字形')
        from .paragraph_layout import paragraph_groups
        groups = paragraph_groups(blocks, section_boundaries=section_boundaries,
                                  mixed_first_page=content_geometry, dash_lists=dash_lists,
                                  pipe_headings=reading_structure, structured_headings=reading_structure >= 2,
                                  appendix_captions=reading_structure >= 3, heading_styles=reading_structure >= 4)
        if extended_citations >= 2:
            groups = _protect_group_citations(blocks, groups)
        return {'schema': 2, **({'journal': journal} if journal else {}),
                **({'symbol_revision': symbol_revision} if symbol_revision else {}),
                **({'frontmatter_revision': 2} if frontmatter_after_cover else {}),
                **({'reading_structure_revision': int(reading_structure)} if reading_structure else {}),
                **({'script_geometry_revision': int(script_geometry)} if script_geometry else {}),
                **({'publisher_font_revision': 1} if publisher_font_styles else {}),
                **({'spacing_revision': 1} if nonadvancing_spaces else {}),
                **({'author_separator_revision': 1} if middle_dot_names else {}),
                **({'publisher_layout_revision': 1} if publisher_layout else {}),
                **({'citation_revision': int(extended_citations)} if extended_citations else {}),
                **({'prose_revision': 2 if short_prose else 1} if italic_words else {}),
                **({'formula_opener_revision': 1} if formula_openers else {}),
                **({'dash_list_revision': 1} if dash_lists else {}),
                **({'author_spacing_revision': 1} if spaced_names else {}),
                **({'vector_fraction_revision': 3 if fraction_labels and fraction_structure else
                    2 if fraction_labels else 1} if vector_fractions else {}),
                **({'grouping_revision': 2} if section_boundaries else {}),
                **({'reference_revision': 2} if reference_line_starts else {}),
                **({'geometry_revision': 2} if content_geometry else {}),
                **({'evidence_revision': 1, 'watermarks': [
                    {'page': n, 'text': value['text'], 'sha256': value['sha256']}
                    for n, value in watermarks.items()]} if corpus_evidence and paragraph else {}),
                'source_sha256': digest(source), 'blocks': blocks,
                'protected_regions': protected,
                'groups': groups, 'issues': issues}
    bodies = [b for b in blocks if b['kind'] == 'body']
    groups = []
    for body in bodies:
        connect = False
        if groups:
            previous = next(b for b in bodies if b['id'] == groups[-1][-1])
            same_size = abs(median(p['size'] for p in previous['parts']) -
                            median(p['size'] for p in body['parts'])) < .6
            same_weight = (all(p['bold'] for p in previous['parts'] if not p['fixed']) ==
                           all(p['bold'] for p in body['parts'] if not p['fixed']))
            connect = (same_size and same_weight and len(previous['text'].split()) >= 8 and
                       not re.search(r'[.!?:;][\s\d)\]]*$', previous['text']) and
                       bool(re.match(r'[a-z(]', body['text'])) and
                       body['page'] <= previous['page'] + 1)
        if connect:
            groups[-1].append(body['id'])
        else:
            groups.append([body['id']])
    return {'schema': 1, 'source_sha256': digest(source), 'blocks': blocks,
            'protected_regions': protected, 'groups': groups, 'issues': issues}


def load_plan(source: Path, path: Path, detections: list[dict] | None = None, *, paragraph: bool = False, skip_block_ids: set[str] | None = None) -> tuple[dict, list[dict]]:
    """核对源页与全覆盖分组；不接受伪造原文、坐标、重复翻译或遗漏正文。"""
    plan = json.loads(path.read_text(encoding='utf-8'))
    if plan.get('schema') != (2 if paragraph else 1) or plan.get('source_sha256') != digest(source):
        raise ValueError('版面计划不属于当前源 PDF 或 schema 不受支持')
    # 旧断点按生成时的分组规则严格复核，不能暗改已有译文对应的段落。
    # 新计划显式记录标题边界规则；尚未翻译的任务可备份后重新提取。
    actual = extract_layout(source, detections, paragraph=paragraph, journal_adapt="journal" in plan,
                            section_boundaries=plan.get('grouping_revision') == 2,
                            reference_line_starts=plan.get('reference_revision') == 2,
                            content_geometry=plan.get('geometry_revision') == 2,
                            corpus_evidence=plan.get('evidence_revision') == 1,
                            symbol_revision=plan.get('symbol_revision', 0),
                            frontmatter_after_cover=plan.get('frontmatter_revision') == 2,
                            publisher_font_styles=plan.get('publisher_font_revision') == 1,
                            extended_citations=plan.get('citation_revision', 0),
                            nonadvancing_spaces=plan.get('spacing_revision') == 1,
                            middle_dot_names=plan.get('author_separator_revision') == 1,
                            publisher_layout=plan.get('publisher_layout_revision') == 1,
                            italic_words=plan.get('prose_revision') in {1, 2},
                            short_prose=plan.get('prose_revision') == 2,
                            formula_openers=plan.get('formula_opener_revision') == 1,
                            dash_lists=plan.get('dash_list_revision') == 1,
                            spaced_names=plan.get('author_spacing_revision') == 1,
                            vector_fractions=plan.get('vector_fraction_revision') in {1, 2, 3},
                            fraction_labels=plan.get('vector_fraction_revision') in {2, 3},
                            fraction_structure=plan.get('vector_fraction_revision') == 3,
                            reading_structure=plan.get('reading_structure_revision', 0),
                            script_geometry=plan.get('script_geometry_revision', 0))
    if plan != actual:
        raise ValueError('自动版面计划与源文件/检测结果不一致；拒绝人工改写或过期缓存')
    skip_block_ids = skip_block_ids or set()
    if skip_block_ids - {b['id'] for b in actual['blocks']}:
        raise ValueError('保留原文选项包含源 PDF 没有的块')
    original = {b['id']: b for b in actual['blocks']}
    blocks = plan.get('blocks', [])
    # 科研散点图可能在同一坐标重复绘制同一字符（如 Biogeosciences 的
    # G / 下划线图元）。保护对象允许重复，仍逐对象对照 actual；不能把
    # 字典去重后的长度当成源对象数量。正文重复分组仍由下方门禁拒绝。
    if len(blocks) != len(actual['blocks']) or {b['id'] for b in blocks} != set(original):
        raise ValueError('版面计划必须逐一覆盖全部原文块')
    for block in blocks:
        expected = original[block['id']]
        if any(block.get(key) != expected[key] for key in ('page', 'rect', 'text', 'parts')):
            raise ValueError('计划中的原文或坐标发生变化；不允许修改原文和坐标')
        if block['kind'] not in {'body', 'figure', 'table', 'reference', 'caption',
                                 'formula', 'header-footer', 'preserve', 'review'}:
            raise ValueError('未知版面分类')
        if block['kind'] == 'review' and block['id'] not in skip_block_ids:
            from .diagnostics import LocatedError
            raise LocatedError('版面计划仍有待确认的阅读区域',
                [{'id': block['id'], 'source': block['text'], 'pages': [block['page']],
                  'regions': [{'page': block['page'], 'rect': block['rect']}]}], 'extraction')
    # 编码诊断是块级问题；只允许用户明确保留全部相应异常块时继续。
    # 旋转整页、图片覆盖等页级结构错误没有安全的句级跳过，不可一并清空。
    from .diagnostics import LocatedError
    anomalous = [b for b in blocks if b['kind'] in {'body', 'caption'} and any(
        c == '\ufffd' or (not c.isprintable() and not c.isspace() and c != '\u00ad') for c in b['text'])]
    remaining = [b for b in anomalous if b['id'] not in skip_block_ids]
    unresolved = [issue for issue in plan.get('issues', []) if '待译区域有未解决的异常编码' not in issue]
    if not anomalous:
        unresolved.extend(i for i in plan.get('issues', []) if '待译区域有未解决的异常编码' in i)
    if remaining or unresolved:
        raise LocatedError('版面计划存在未解决诊断：' + '; '.join(unresolved or plan['issues']),
            [{'id': b['id'], 'source': b['text'], 'pages': [b['page']],
              'regions': [{'page': b['page'], 'rect': b['rect']}]} for b in remaining], 'extraction')
    # 自动发现的保护范围不可通过删除或改写 JSON 条目绕开。
    if any(p not in plan.get('protected_regions', []) for p in actual['protected_regions']):
        raise ValueError('计划遗漏源图、表格或参考文献的保护区域')
    translated_kinds = {'body', 'caption'} if paragraph else {'body'}
    body_ids = {b['id'] for b in blocks if b['kind'] in translated_kinds}
    assigned = [sid for group in plan['groups'] for sid in group]
    if len(assigned) != len(set(assigned)) or set(assigned) != body_ids:
        raise ValueError('逻辑分组必须覆盖每个正文块一次，不得重复或遗漏')
    by_id = {b['id']: b for b in blocks}
    protected = list(plan['protected_regions'])
    protected.extend(b for b in blocks if b['kind'] not in translated_kinds)
    with open_source_pdf(source) as document:
        for region in protected:
            number, values = region['page'], region['rect']
            if type(number) is not int or not 1 <= number <= len(document):
                raise ValueError('保护区域页号非法')
            if len(values) != 4 or not all(math.isfinite(v) for v in values):
                raise ValueError('保护区域坐标必须为有限数值')
            rect = fitz.Rect(values)
            if rect.is_empty or not document[number - 1].rect.contains(rect):
                raise ValueError('保护区域为空或越界')
    if skip_block_ids:
        from copy import deepcopy
        plan = deepcopy(plan)
        for block in plan['blocks']:
            if block['id'] in skip_block_ids:
                block['kind'] = 'preserve'
                block['preserve_reason'] = '用户选择跳过，保留原文'
        plan['groups'] = [[sid for sid in group if sid not in skip_block_ids] for group in plan['groups']]
        plan['groups'] = [group for group in plan['groups'] if group]
    return plan, units_from_plan(plan, paragraph=paragraph)


def units_from_plan(plan: dict, *, paragraph: bool) -> list[dict]:
    """由已验证的计划构建段落；调用者必须先核对来源及完整字符几何。"""
    blocks = plan['blocks']
    by_id = {b['id']: b for b in blocks}
    translated_kinds = {'body', 'caption'} if paragraph else {'body'}
    protected = list(plan['protected_regions']) + [b for b in blocks if b['kind'] not in translated_kinds]
    units = []
    for ids in plan['groups']:
        if not ids:
            raise ValueError('逻辑分组不能为空')
        parts = []
        for sid in ids:
            block = by_id[sid]
            for index, original_part in enumerate(block['parts']):
                part = dict(original_part)
                # 原段落的短末行也有可用栏内空白。例如 URL 后的英文句点
                # 只有约 2 pt，但中文句号需要整字宽。允许该行最后一段使用
                # 原段落右边界以内的空白；不扩列、不跨越公式或其他文本块。
                # 上下标基线不同但仍属同一物理行，不能因此把前面的正文
                # 扩到引文右侧空白。使用字框垂直重合判断后续同行对象。
                later_on_line = any(min(p['rect'][3], part['rect'][3]) > max(p['rect'][1], part['rect'][1])
                                    for p in block['parts'][index + 1:])
                if not part['fixed'] and not later_on_line:
                    rect = fitz.Rect(part['rect'])
                    rect.x1 = max(rect.x1, block['rect'][2])
                    if list(rect) != part['rect'] or 'writing_rect' in part:
                        part['writing_rect'] = list(rect)
                if not part['fixed']:
                    part['source_block'] = sid
                    rect = fitz.Rect(part.get('writing_rect', part['rect']))
                    # 原英文字框只描述该行字体高度，不包括行间留白。中文
                    # 墨迹可能略高于英文而仍完全容得下；按相邻正文行之间
                    # 空白的中线分配可写高度，保留原字号，不侵入邻行或保护区。
                    # 数学上下标会把一个物理段落拆成多个原生块，因此邻行
                    # 来自同页、同一水平范围内的正文，而不局限于原生块编号。
                    peers = [p for peer in blocks if peer['kind'] == 'body' and peer['page'] == part['page']
                             for p in peer['parts'] if not p['fixed']
                             and min(p['rect'][2], part['rect'][2]) > max(p['rect'][0], part['rect'][0])]
                    above = [p['rect'][3] for p in peers if p['rect'][3] <= rect.y0]
                    below = [p['rect'][1] for p in peers if p['rect'][1] >= rect.y1]
                    if above:
                        rect.y0 -= min(rect.y0 - max(above), part['size']) / 2
                    if below:
                        rect.y1 += min(min(below) - rect.y1, part['size']) / 2
                    if list(rect) != part['rect'] or 'writing_rect' in part:
                        part['writing_rect'] = list(rect)
                    # 邻段的总外框可能覆盖本段末行（例如下一段首行从右侧
                    # 公式后开始、第二行回到栏左边）。避让实际文字片段，
                    # 不把总外框中的空白误当成文字；图表等仍用完整保护区。
                    nearby = protected + [p for b in blocks if b['id'] != sid and b['kind'] == 'body'
                                          for p in b['parts']]
                    part['protected_rects'] = [list(box) for p in nearby if p['page'] == part['page']
                                               for box in fixed_text_rectangles(p) if rect.intersects(box)]
                parts.append(part)
        if paragraph:
            from .paragraph_layout import merge_inline_parts
            parts = merge_inline_parts(parts)
        source_parts, slots, anchors = [], [[]], []
        for part in parts:
            if part['fixed']:
                marker = '{v' + str(len(anchors)) + '}'
                anchors.append(part)
                source_parts.append(marker)
                slots.append([])
            else:
                # 相邻行的字体字框可重叠而墨迹不重叠（实测网址行上方正文）。
                # 不在这里仅凭整框相交否决：模型调用前的字符删除预检必须
                # 证明每个正文字符均能安全删除，排字再避开所有保护区域。
                slots[-1].append(part)
                source_parts.append(part['text'])
        if paragraph:
            from .paragraph_layout import source_text
            text = source_text(parts)
        else:
            text = _plain(' '.join(source_parts))
        # 跨行/页断词仅在英文小写续词前连接，保留减号与科学表达式。
        text = re.sub(r'(?<=[A-Za-z])- (?=[a-z])', '', text)
        if not text or not any(slots):
            raise ValueError('自动分组没有可翻译文字，正文分类尚未闭合')
        # 两端对齐的英文词有时被 PDF 提取器拆成多个同基线“行”。这些
        # 词之间没有固定锚点，应共用连续排字区域，不能把原空格变成不可
        # 使用的孔洞。限定同一原生块/字重/字号和相邻范围，避免跨栏合并。
        joined_slots = []
        for slot in slots:
            joined = []
            for part in slot:
                last = joined[-1] if joined else None
                if (last and last.get('source_block') == part.get('source_block')
                        and last['page'] == part['page'] and last['bold'] == part['bold']
                        and abs(last['baseline'] - part['baseline']) < .1
                        and abs(last['size'] - part['size']) < .1
                        and 0 <= part['rect'][0] - last['rect'][2] <= 2 * part['size']):
                    last['writing_rect'] = list(fitz.Rect(last.get('writing_rect', last['rect'])) |
                                                fitz.Rect(part.get('writing_rect', part['rect'])))
                    last['rect'] = list(fitz.Rect(last['rect']) | fitz.Rect(part['rect']))
                    last['chars'] = last['chars'] + part['chars']
                    last['text'] += ' ' + part['text']
                    last['protected_rects'] = last.get('protected_rects', []) + part.get('protected_rects', [])
                else:
                    joined.append(dict(part))
            joined_slots.append(joined)
        units.append({'id': segment_id(text), 'source': text, 'slots': joined_slots,
                      'anchors': anchors, 'blocks': ids})
    if paragraph:
        from .paragraph_layout import attach_frames
        attach_frames(units, plan)
    return units


def anchor_errors(unit: dict, target: str) -> list[str]:
    """先代回不可修改的锚点，再检查译文括号；仅数占位符不能发现重复右括号。

    跨物理分段的源文可能本身不配对，因此比较源/目标的末尾深度和最小深度，
    不假定每段均从零开始。允许中英文括号转换及合理新增的完整括号对。
    """
    markers = ['{v' + str(i) + '}' for i in range(len(unit['anchors']))]
    if FORMULA_RE.findall(target) != markers:
        return [f'固定公式/引用锚点的数量或顺序改变：原文要求 {markers!r}；译文实际 {FORMULA_RE.findall(target)!r}']
    if 'source' not in unit:
        return []
    def profile(text):
        for marker, anchor in zip(markers, unit['anchors']):
            text = text.replace(marker, anchor['text'])
        result = []
        for opening, closing in [('(（', ')）'), ('[［', ']］')]:
            depth = lowest = 0
            for character in text:
                depth += 1 if character in opening else -1 if character in closing else 0
                lowest = min(lowest, depth)
            result.append((depth, lowest))
        return result
    if profile(unit['source']) == profile(target):
        return []
    # 只在已经判定失败后抽取可读证据，不改变允许中英文括号等价的规则。
    bracket_sequences = []
    for text in (unit['source'], target):
        for marker, anchor in zip(markers, unit['anchors']):
            text = text.replace(marker, anchor['text'])
        bracket_sequences.append(''.join(c for c in text if c in '()（）[]［］'))
    return [f'还原固定锚点后括号不配对：不得重复锚点已有括号；'
            f'原文括号序列 {bracket_sequences[0]!r}；译文括号序列 {bracket_sequences[1]!r}']


def _fit_slot(slot, tokens, unit, current, regular_font, bold_font, *, spread=False):
    """在两个固定锚点之间排完整文字；均匀换行失败时由调用方采用已验证布局。"""
    rest, placements = list(tokens), []
    remaining_parts = list(slot)
    while remaining_parts and rest:
        part = remaining_parts.pop(0)
        if part.get('bold') and bold_font is None:
            raise ValueError('粗体源文字缺少对应中文粗体字体')
        font = bold_font if part.get('bold') else regular_font
        # 保护检查在 144 dpi 的整像素上执行；排字避让同一像素范围，
        # 再留半个像素的抗锯齿边界，防止字框不交叠而边缘墨迹互相覆盖。
        obstacles = []
        for anchor in unit['anchors']:
            if anchor['page'] == part['page'] and anchor.get('rect'):
                for rect in fixed_text_rectangles(anchor):
                    box = fitz.Rect((rect * 2).irect) / 2
                    obstacles.append(fitz.Rect(box.x0 - .25, box.y0 - .25, box.x1 + .25, box.y1 + .25))
        obstacles.extend(fitz.Rect(r) for r in part.get('protected_rects', []))
        free = writing_rectangles(part.get('writing_rect', part['rect']), obstacles)
        if not free:
            continue
        best = None
        for rect in sorted(free, key=lambda r: r.get_area(), reverse=True):
            width = rect.width - .1
            if spread and remaining_parts:
                # 按剩余源行宽度分配译文，避免短中文全部挤在段首、原引用
                # 却孤立在几行之后。只调整已有文字的换行，不增加或删减内容。
                following = sum(fitz.Rect(p.get('writing_rect', p['rect'])).width
                                for p in remaining_parts)
                fraction = width / (width + following)
                desired = font.text_length(''.join(rest), fontsize=current) * fraction
                width = min(width, max(desired, font.text_length(rest[0], fontsize=current)))
            line, count = '', 0
            while count < len(rest) and font.text_length(line + rest[count], fontsize=current) <= width:
                line += rest[count]
                count += 1
            # 中文避头尾：不能把逗号等标点留到下一行，也不把左括号
            # 单独留在行末。只调整本段换行，不把文字移过固定引用锚点。
            while count > 0 and count < len(rest) and rest[count][0] in NO_LINE_START:
                count -= 1
            while count > 0 and count < len(rest) and rest[count - 1][-1] in NO_LINE_END:
                count -= 1
            line = ''.join(rest[:count])
            if not line.strip():
                continue
            left, bottom, right, top = line_ink(font, line, current)
            # 保持原行区域，在区域内微调基线以容纳真实汉字墨迹；
            # 引用上下方的空白高度不足时不能仅凭字符串宽度判定成功。
            lowest, highest = rect.y0 + top, rect.y1 + bottom
            if lowest > highest or right - min(0, left) > rect.width:
                continue
            if '_line_baseline' in part:
                baseline = part['_line_baseline']
                if not lowest <= baseline <= highest:
                    continue
            else:
                baseline = min(highest, max(lowest, part.get('baseline', lowest)))
            item = {**part, 'rect': list(rect), 'target': line, 'font_size': current,
                    'baseline': baseline, 'origin_x': rect.x0 - min(0, left)}
            # 上一行下标侵入时，面积最大的空白矩形可能更窄，少放
            # 一个字；略矮但更宽的区域却能放全。按实际容字数选择，
            # 不能选到第一个局部可行矩形就误报整段溢出。
            if best is None or count > best[0]:
                best = count, item
        if best is not None:
            placements.append(best[1])
            del rest[:best[0]]
            # 上一行的下标可能只侵入本行中间，左右仍有足够高度。
            # 允许在同一基线上继续使用右侧不相交区域，避免只选一片
            # 空白而误报溢出；游标严格右移，不重复占用任何写入区域。
            outer = fitz.Rect(part.get('writing_rect', part['rect']))
            cursor = best[1]['rect'][2]
            if rest and outer.x1 - cursor > .1:
                remaining_parts.insert(0, {**part, 'writing_rect': [cursor, outer.y0, outer.x1, outer.y1],
                                           '_line_baseline': best[1]['baseline']})
    return placements, rest


def fit_unit(unit: dict, target: str, font: fitz.Font, min_size: float | None,
             bold_font: fitz.Font | None = None) -> list[dict]:
    """锚点之间分别排字，锚点保持源位置；全文译文仍来自一次联合翻译。

    不强迫把中文语序挪过固定引用。若无法在锚点之间容纳译文，等待可验证的自动恢复；
    不移动公式、不截断译文、不把溢出称为已解决。
    """
    if 'frames' in unit:
        from .paragraph_layout import fit_paragraph
        return fit_paragraph(unit, target, font, min_size, bold_font)
    errors = anchor_errors(unit, target)
    if errors:
        raise ValueError('; '.join(errors))
    regular_font = font
    chunks = FORMULA_RE.split(target)
    sizes = [p['size'] for slot in unit['slots'] for p in slot]
    size = median(sizes)
    floor = size if min_size is None else min_size
    if not math.isfinite(floor) or floor <= 0 or floor > size:
        raise ValueError('最小字号必须为正数，且不大于源正文字号')
    for step in range(math.ceil((size - floor) * 10) + 1):
        current = max(floor, size - step / 10)
        placements, complete = [], True
        for chunk_index, (chunk, slot) in enumerate(zip(chunks, unit['slots'])):
            # 中英文之间的可选空格不属于科学标记。窄引文间隙里一个空格
            # 就可能挤出完整模型名称；中文排版可直接相邻，数值与 ASCII
            # 单位内部的空格则保持，不能拆开或改写标识符来伪造容纳成功。
            compact = re.sub(r'(?<=[\u3400-\u9fff]) +(?=[A-Za-z0-9])|'
                             r'(?<=[A-Za-z0-9]) +(?=[\u3400-\u9fff])', '', _plain(chunk))
            rest = re.findall(r'https?://\S+|[A-Za-z0-9]+(?:[.−–/-][A-Za-z0-9]+)*|.', compact)
            # 已有范围线/连字符是合法换行点，例如窄行中的（1981–2014）。
            # 不拆英文单词或小数，不增删连字符；各行重新连接仍是完整原标记。
            rest = [piece for token in rest for piece in
                    ([token] if token.startswith(('http://', 'https://')) else re.split(r'(?<=[–/-])', token)) if piece]
            fitted, rest_left = _fit_slot(slot, rest, unit, current, regular_font, bold_font)
            if not rest_left and len(slot) > 1:
                # 保持正常行宽，把需要的行均匀放在原段落高度内。若强制
                # 每条英文源行都写中文，会产生只有半栏宽的碎行。跨页、
                # 跨栏及图片间隙两侧的边界行必须保留，不能把某一段腾空。
                boundaries = set()
                for i in range(1, len(slot)):
                    previous, following = slot[i - 1], slot[i]
                    a, b = fitz.Rect(previous['rect']), fitz.Rect(following['rect'])
                    if (previous['page'] != following['page'] or b.y0 < a.y0 - 1
                            or b.y0 - a.y1 > 2 * max(a.height, b.height)):
                        boundaries.update((i - 1, i))
                for count in range(min(len(slot), max(2, len(fitted))), len(slot) + 1):
                    indices = boundaries | {round(i * (len(slot) - 1) / (count - 1)) for i in range(count)}
                    selected = [slot[i] for i in sorted(indices)]
                    balanced, pending = _fit_slot(selected, rest, unit, current, regular_font, bold_font, spread=True)
                    if not pending and len(balanced) >= len(fitted):
                        fitted = balanced
                        break
            if fitted and chunk_index < len(unit['anchors']) and slot:
                last, original_last = fitted[-1], slot[-1]
                anchor = unit['anchors'][chunk_index]
                # 只有实际写到引用前的最后源行，才向该行右侧贴齐。引用与
                # 公式的位置始终不动；不能把上一行的字平移到另一页或跨过锚点。
                if (last['page'] == anchor['page'] and last['rect'][1] < original_last['rect'][3]
                        and last['rect'][3] > original_last['rect'][1]
                        and anchor['rect'][0] >= last['rect'][2]):
                    selected_font = bold_font if last.get('bold') else regular_font
                    left, _, right, _ = line_ink(selected_font, last['target'], current)
                    advance = selected_font.text_length(last['target'], fontsize=current)
                    last['origin_x'] = max(last['rect'][0] - min(0, left),
                                           last['rect'][2] - max(advance, right) - .05)
            placements.extend(fitted)
            rest = rest_left
            if rest:
                complete = False
                break
        if complete:
            return placements
    raise ValueError(f'完整译文无法放入原区域和固定引文之间：段落 {unit.get("id", "unknown")[:12]}，'
                     f'锚点间区域 {chunk_index + 1} 尚余 {len("".join(rest))} 字符；保留断点等待修正')


def verify_unchanged(source: Path, candidate: Path, editable: list[dict], *, scale: int = 2, expected_links: dict | None = None) -> dict:
    """比较每页所有非正文像素，并核对链接与页框，不用对象数量冒充内容相同。

    检查分辨率记录在报告中；这证明该分辨率下的可见内容一致，不声称文件字节
    完全一致。正文框边界外只留一像素抗锯齿容差，保护区域仍另外逐区比较。
    """
    pages = []
    body_masks = None
    with open_source_pdf(source) as before, fitz.open(candidate) as after:
        if len(before) != len(after):
            raise ValueError('源/译页数改变')
        for number, (a, b) in enumerate(zip(before, after), 1):
            if a.mediabox != b.mediabox or a.cropbox != b.cropbox or a.rotation != b.rotation:
                raise ValueError('源页面几何改变')
            def links(values):
                return sorted(str({k: (tuple(round(x, 3) for x in v) if expected_links is not None and isinstance(v, (fitz.Rect, fitz.Point)) else str(v)) for k, v in sorted(link.items()) if k not in {'xref', 'id'}})
                              for link in values)
            expected = expected_links[number] if expected_links is not None else a.get_links()
            if expected_links is not None:
                from .paragraph_layout import links_equal
                equal = links_equal(expected, b.get_links())
            else:
                equal = links(expected) == links(b.get_links())
            if not equal:
                raise ValueError(f'第{number}页链接发生变化')
            x, y = a.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False), b.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            left, right = Image.frombytes('RGB', (x.width, x.height), x.samples), Image.frombytes('RGB', (y.width, y.height), y.samples)
            diff = ImageChops.difference(left, right)
            mask = ImageDraw.Draw(diff)
            for part in editable:
                if part['page'] == number:
                    r = fitz.Rect(part.get('writing_rect', part['rect'])) * scale
                    mask.rectangle((math.floor(r.x0) - 1, math.floor(r.y0) - 1,
                                    math.ceil(r.x1) + 1, math.ceil(r.y1) + 1), fill=(0, 0, 0))
            if diff.getbbox():
                # 原始j等字形的负左侧承可能越过PDF字符推进框。只扣除
                # 已绑定原字体、原坐标的待译正文实际墨迹，不扩大矩形
                # 容差；未知字体仍严格失败，邻近固定字形另有独立校验。
                if body_masks is None:
                    from .font_geometry import source_anchor_masks
                    body_masks, _ = source_anchor_masks(source, editable, scale=scale)
                if number in body_masks:
                    ink = body_masks[number].point(lambda value: 255 if value else 0)
                    diff.paste((0, 0, 0), mask=ink)
                if diff.getbbox():
                    from .diagnostics import LocatedError
                    region = [v/scale for v in diff.getbbox()]
                    # 差异区不一定属于某个句子；显示真实像素范围及该范围
                    # 的原页上下文，不把邻近正文伪称为已定位的失败译文。
                    raise LocatedError(f'第{number}页正文区域外像素改变，拒绝发布候选', [{
                        'pages': [number], 'source': a.get_textbox(fitz.Rect(region)),
                        'regions': [{'page': number, 'rect': region}],
                        'errors': ['preservation.pixels 原文保护范围出现可见差异；可能存在越界排字、位移或源内容缺失。'],
                    }], 'preservation')
            pages.append({'page': number, 'outside_pixels_equal': True})
    return {'dpi': scale * 72, 'pages': pages}
