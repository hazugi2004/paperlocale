"""源页保版计划：将逻辑阅读顺序与物理位置分离。

同一逻辑段落可以有任意多个跨页、跨栏或绕图的文本块。公式/引用以占位符
参加语序校验，但其原始 PDF 字符从不删除或重写。整张表格、图像、书目等
保护区域不进入翻译。自动识别是启发式，不能据此宣称任意 PDF 的语义完备。
计划仅作为自动阶段的可审计断点；源文本和坐标由原 PDF 重新核对。
"""
from __future__ import annotations

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
MATH_FONT = re.compile(r'math|mth|symbol|cmsy|cmmi|cmex|msam|msbm', re.I)
# 图注编号后须有标点，或以大写词开启图注正文；正文中的“Figure 5
# displays”和“Figure 2a”不是图注。罗马数字表号同样构成独立标题，
# 防止连续的 Table III/IV/V 被合并为一个段落。无标点且小写开头的
# 非标准图注仍依靠视觉检测器分类，不能只凭正文中的图号引用推断。
LEGACY_CAPTION = re.compile(r'^(?:(?:Extended Data|Supplementary)\s+)?(?:Fig(?:ure)?\.?|Table)\s*\d', re.I)
CAPTION = re.compile(
    r'^(?i:(?:(?:Extended Data|Supplementary)\s+)?(?:Fig(?:ure)?\.?|Table))'
    r'\s*(?:\d+|[IVXLCDM]+)(?:\s*[.|:]|\s+(?=[A-Z]))')
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
    size = median(c[0] for c in chars)
    baseline = median(c[1] for c in chars if c[0] >= .95*size)
    return span['size'] < .95*size or span['origin'][1] < baseline-.2*size


def _parts(block: dict, page_number: int, links: list, *, paragraph=False) -> list[dict]:
    """按字符坐标分开普通文字和固定锚点，不用白块覆盖公式/引用。

    上标、数学字体及明确引文模式保持原对象。普通斜体不能一律当公式，
    否则会把物种名或强调正文误排除；独立数学区域另外由自动检测保护。
    """
    result = []
    for line in block.get('lines', []):
        chars = []
        for span in line['spans']:
            superscript = _superscript(span, line, paragraph=paragraph)
            math_font = MATH_FONT.search(span['font']) or paragraph and re.match(r'^MT(?:MI|SY|EX)', span['font'])
            for char in span['chars']:
                # 出版社数学字体可能将括号映射到控制码，普通中文字体
                # 无法重建其字形；保留源字形，不能把控制码当正文传给模型。
                chars.append({**char, 'fixed': bool(superscript or math_font
                                                  or not char['c'].isprintable()),
                              'size': span['size'], 'italic': bool(span['flags'] & 2 or
                                  re.search(r'(?:[.-]I|Italic|Oblique)$', span['font'])),
                              'bold': _bold(span, paragraph=paragraph),
                              'superscript': superscript})
        text = ''.join(c['c'] for c in chars)
        # 行内变量未必使用数学字体：真实论文的 β 带帽、R_n、T_d 分布在
        # 普通斜体和小字号下标中。按完整变量词元保护基字及上下标，避免
        # 只留下原帽号、却用中文字体重写 β。普通斜体物种名/强调词不适用。
        normal_size = median(c['size'] for c in chars)
        for token in re.finditer(r'(?<![A-Za-z])[A-Za-zα-ωΑ-Ω][A-Za-zα-ωΑ-Ω0-9∧]*', text):
            run = chars[token.start():token.end()]
            variable = (bool(re.search('[α-ωΑ-Ω]', token.group())) or
                        (paragraph and all(c['italic'] for c in run) and
                         min(c['size'] for c in run) < .85*max(c['size'] for c in run)) or
                        (len(token.group()) <= 3 and (run[0]['italic'] or
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
        quantities = [(q.start, q.end) for q in find_quantities(text)]
        quantities += [(a, b) for a, b, _ in standalone_units(text)]
        for start, end in quantities:
            if any(c['fixed'] for c in chars[start:end]):
                for char in chars[start:end]:
                    char['fixed'] = True
        ranges = [m.span() for pattern in (CITATION, URL_RE) for m in pattern.finditer(text)] + scientific_literal_spans(text)
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
            if chars[expression.start()]['fixed']:
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
                           'chars': [{'text': c['c'], 'rect': list(c['bbox']), 'origin': list(c['origin'])}
                                     for c in run]})
    return result


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


def _preserve_front_matter(blocks: list[dict], *, paragraph=False) -> None:
    """保护显著大号主标题下、摘要/长正文之前的作者与机构。

    短作者名单不一定带多个逗号，不能仅依赖人数。这里联合字号、页面位置
    以及姓名/机构文字证据；不把所有短行都当成作者，也不翻译姓名推测汉字。
    没有可辨识标题层次时不据此扩大保护范围，交由其他分类证据处理。
    """
    first = [b for b in blocks if b['page'] == 1 and b['kind'] == 'body' and b['parts']]
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
            normal = max(p['size'] for p in block['parts'])
            name_text = ''.join(p['text'] if p['size'] >= .85*normal else ',' for p in block['parts'])
        names = re.sub(r"[\d*†‡]+", "", name_text).strip()
        names = re.split(r"\s*(?:,|;|&|\band\b)\s*", names)
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


def _preserve_auxiliary_sections(blocks: list[dict], *, paragraph=False) -> None:
    """原位设置辅助文本分类；小标题与跨页后续内容都不发给翻译器。

    输入为已按阅读顺序排列的源块。依赖明确章节名称，不根据一般正文中
    的单词命中排除整段；短标题、粗体前缀与常见出版声明可以启动保护。
    这是自动识别规则而非任意文档的语义证明，未知版式仍受后续质量门禁约束。
    """
    auxiliary = False
    for block in blocks:
        value = block['text']
        match = AUXILIARY_HEADING.match(value)
        # 附录可能放在致谢后、书目前，仍含需要翻译的科学论证。仅接受
        # 短的明确附录标题，不能因正文提到 Appendix 就结束辅助区保护。
        appendix = paragraph and len(value.split()) <= 18 and re.fullmatch(
            r'Appendix(?:\s+[A-Z0-9])?(?:\s*[:.\u2014\u2013-]\s*\S.*)?', value, re.I)
        if (MAIN_HEADING.fullmatch(value) or appendix) and block['kind'] == 'body':
            auxiliary = False
        elif match and (not value[match.end():].strip() or
                        (block['parts'] and block['parts'][0]['bold']) or
                        re.match(r"^(?:Correspondence|Publisher[’']s note|Open Access|"
                                 r"Peer review information|Reprints and permissions)\b", value, re.I)):
            auxiliary = True
        if auxiliary and block['kind'] == 'body':
            block['kind'] = 'preserve'


def _nonprinting_line(page: fitz.Page, line: dict, paint: dict) -> str | None:
    """识别有绘制证据的非可见文字，返回保留原因而非删除源对象。

    PDF 提取文字不等于页面可见正文。render mode 3 / 零透明度按逐字符
    绘制记录判断；白字还必须在其完整区域实测为纯白，不能丢掉深色底白字。
    未匹配绘制记录、低对比度或部分可见时都保守保留普通分类路径。
    """
    chars = [c for s in line['spans'] for c in s['chars'] if not c['c'].isspace()]
    keys = [(ord(c['c']), round(c['origin'][0], 3), round(c['origin'][1], 3)) for c in chars]
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


def extract_layout(source: Path, detections: list[dict] | None = None, *, paragraph: bool = False) -> dict:
    """收集所有可见文本块，自动分类并形成跨区域逻辑段落断点。

    图表矩形与书目通过源文几何提取；扫描页/旋转文字不猜测性 OCR。
    每块均保留明确分类，不能把未识别正文静默算作已翻译。
    """
    # preserved 旧断点逐字核对旧自动计划；修复只用于新的段落模式，
    # 避免改变已发布旧引擎的保护区域和缓存身份。
    caption_pattern = CAPTION if paragraph else LEGACY_CAPTION
    _, _, _, references = _reference_geometry(source)
    blocks, protected, issues = [], [], []
    document, _ = open_source_for_editing(source)
    with document:
        for number, page in enumerate(document, 1):
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
                if covered_by_image and not covered_by_figure:
                    issues.append(f'第{number}页图片外框覆盖自动识别的正文，尚不能自动区分透明图片与图内文字')
                    break
            protected.extend(b for b in boxes if not paragraph or b['kind'] != 'caption')
            # 同一坐标可能有不可见OCR层与可见文字重叠：只有全部绘制记录
            # 都不着色才认定为非打印对象，不能因一份隐藏副本而排除可见正文。
            paint = {}
            for span in page.get_texttrace():
                hidden = span['type'] == 3 or span['opacity'] == 0
                for code, _, origin, _ in span['chars']:
                    paint.setdefault((code, round(origin[0], 3), round(origin[1], 3)), []).append(hidden)
            raw = page.get_text('rawdict')['blocks']
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
                        baseline = max(s['origin'][1] for s in line['spans'])
                        old_baseline = max(s['origin'][1] for s in last['spans']) if last else 0
                        previous_text = ''.join(c['c'] for s in last['spans'] for c in s['chars']) if last else ''
                        if (last and tuple(line['dir']) == tuple(last['dir']) == (1,0)
                                and re.search(r'[A-Za-zα-ωΑ-Ω]', previous_text)
                                # 悬挂编号不是公式片段；保持独立，后续提取
                                # 才能保留编号与正文的间隔及列表缩进。
                                and not re.fullmatch(r'(?:\(\d{1,3}\)|\d{1,3}[.)]|[a-z][.)])', previous_text.strip())
                                and a.x1-size <= b.x0 <= a.x1+size and b.x0 > a.x0 and
                                min(a.y1,b.y1) > max(a.y0,b.y0) and abs(baseline-old_baseline) < .6*size):
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
                    covers = [box for box in boxes if lr.get_area() > 0 and
                              (lr & fitz.Rect(box['rect'])).get_area() / lr.get_area() > .5]
                    role = covers[0]['kind'] if covers else 'body'
                    if index in markers and role == 'body':
                        role = 'preserve'
                    reason = _nonprinting_line(page, line, paint) if role == 'body' else None
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
                    changed = (split and role == 'body' and not caption_pattern.match(native_text) and
                               (abs(split[-1]['_style'][0] - style[0]) > .6 or split[-1]['_style'][1] != style[1]))
                    paragraph_break = False
                    if paragraph and split and role == 'body' and not caption_pattern.match(native_text):
                        from .paragraph_layout import starts_paragraph
                        paragraph_break = starts_paragraph(native['lines'], split[-1]['lines'], line)
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
                elif kind == 'body' and not re.search(r'[A-Za-z]{2}', value):
                    kind = 'formula'
                elif kind == 'body' and (rect.y1 < 35 or rect.y0 > page.rect.height - 30):
                    kind = 'header-footer'
                elif kind == 'body' and any(tuple(l['dir']) != (1, 0) for l in block['lines']):
                    # 先排除有证据的符号、网址、页眉脚注；真正旋转的自然
                    # 语言正文仍需等待支持，不能把所有旋转文字都静默透传。
                    kind = 'review'
                # 真实 Nature 论文有两段 References 标题与书目合并的块；
                # 使用明确标题或多条编号+年份证据，不把任意数字段落当书目。
                if re.match(r'^References(?:\s|$)', value, re.I) or (
                        len(re.findall(r'(?:^|\s)\d{1,3}\.\s+[A-Z]', value)) >= 2 and
                        len(re.findall(r'\b(?:19|20)\d{2}\b', value)) >= 2):
                    kind = 'reference'
                if METADATA.match(value):
                    kind = 'header-footer'
                words = re.findall(r'[A-Za-z]+', value)
                if (number == 1 and rect.y1 < page.rect.height / 2 and value.count(',') >= 4
                        and len(words) > 8 and sum(w[0].isupper() for w in words) / len(words) > .8):
                    kind = 'preserve'
                parts = _parts(block, number, page.get_links(), paragraph=paragraph)
                # 只有原位数学/引用锚点的片段没有可翻译正文，直接保护；
                # 例如被出版社拆成独立文本块的行内下标表达式。
                if kind == 'body' and parts and all(p['fixed'] for p in parts):
                    kind = 'formula'
                identity = segment_id(json.dumps([number, list(rect), value], ensure_ascii=False))
                blocks.append({'id': identity, 'page': number, 'rect': list(rect),
                               'text': value, 'parts': parts, 'kind': kind,
                               'list_start': block['_list_start'],
                               'preserve_reason': block['_reason'],
                               'page_width': page.rect.width})
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
    _preserve_front_matter(blocks, paragraph=paragraph)
    _preserve_auxiliary_sections(blocks, paragraph=paragraph)
    if paragraph:
        from .paragraph_layout import paragraph_groups
        return {'schema': 2, 'source_sha256': digest(source), 'blocks': blocks,
                'protected_regions': protected, 'groups': paragraph_groups(blocks), 'issues': issues}
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


def load_plan(source: Path, path: Path, detections: list[dict] | None = None, *, paragraph: bool = False) -> tuple[dict, list[dict]]:
    """核对源页与全覆盖分组；不接受伪造原文、坐标、重复翻译或遗漏正文。"""
    plan = json.loads(path.read_text(encoding='utf-8'))
    if plan.get('schema') != (2 if paragraph else 1) or plan.get('source_sha256') != digest(source):
        raise ValueError('版面计划不属于当前源 PDF 或 schema 不受支持')
    actual = extract_layout(source, detections, paragraph=paragraph)
    if plan != actual:
        raise ValueError('自动版面计划与源文件/检测结果不一致；拒绝人工改写或过期缓存')
    original = {b['id']: b for b in actual['blocks']}
    blocks = plan.get('blocks', [])
    if len(blocks) != len(original) or {b['id'] for b in blocks} != set(original):
        raise ValueError('版面计划必须逐一覆盖全部原文块')
    for block in blocks:
        expected = original[block['id']]
        if any(block.get(key) != expected[key] for key in ('page', 'rect', 'text', 'parts')):
            raise ValueError('计划中的原文或坐标发生变化；不允许修改原文和坐标')
        if block['kind'] not in {'body', 'figure', 'table', 'reference', 'caption',
                                 'formula', 'header-footer', 'preserve', 'review'}:
            raise ValueError('未知版面分类')
        if block['kind'] == 'review':
            raise ValueError('版面计划仍有待确认的阅读区域')
    if plan.get('issues'):
        raise ValueError('版面计划存在未解决诊断：' + '; '.join(plan['issues']))
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
    with fitz.open(source) as document:
        for region in protected:
            number, values = region['page'], region['rect']
            if type(number) is not int or not 1 <= number <= len(document):
                raise ValueError('保护区域页号非法')
            if len(values) != 4 or not all(math.isfinite(v) for v in values):
                raise ValueError('保护区域坐标必须为有限数值')
            rect = fitz.Rect(values)
            if rect.is_empty or not document[number - 1].rect.contains(rect):
                raise ValueError('保护区域为空或越界')
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
    return plan, units


def anchor_errors(unit: dict, target: str) -> list[str]:
    """先代回不可修改的锚点，再检查译文括号；仅数占位符不能发现重复右括号。

    跨物理分段的源文可能本身不配对，因此比较源/目标的末尾深度和最小深度，
    不假定每段均从零开始。允许中英文括号转换及合理新增的完整括号对。
    """
    markers = ['{v' + str(i) + '}' for i in range(len(unit['anchors']))]
    if FORMULA_RE.findall(target) != markers:
        return ['固定公式/引用锚点的数量或顺序改变']
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
    return [] if profile(unit['source']) == profile(target) else ['还原固定锚点后括号不配对：不得重复锚点已有括号']


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
    with fitz.open(source) as before, fitz.open(candidate) as after:
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
                    raise ValueError(f'第{number}页正文区域外像素改变，拒绝发布候选')
            pages.append({'page': number, 'outside_pixels_equal': True})
    return {'dpi': scale * 72, 'pages': pages}
