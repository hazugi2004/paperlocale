"""以源 PDF 的刊名/DOI 证据选择版式规则，不按文件名猜期刊。

识别与版面分类分开：同刊的作者稿、补充材料不强套正式双栏模板；只保留
有几何和文字证据的出版辅助信息。未知/冲突来源使用通用规则，不删除正文。
所有坐标为 PyMuPDF 点坐标，页码从 1 开始；原对象只保护、不擦除。
"""
from collections import Counter
from pathlib import Path
import re
import pymupdf as fitz

# 刊名、出版社家族、具有刊物区分能力的 DOI/页眉标记。不可只用出版社名，
# 否则参考文献中的 Nature 或 Elsevier 会导致错误归属。
PROFILES = [
    ('Nature Climate Change', 'nature', r'10\.1038/s41558|nature\s+climate\s+change'),
    ('Nature Reviews Earth & Environment', 'nature', r'10\.1038/s43017|nature\s+reviews[.\s]+earth\s*&\s*environment'),
    ('Nature Communications', 'nature', r'10\.1038/s41467|nature\s+communications'),
    ('Communications Earth & Environment', 'nature', r'10\.1038/s43247|communications\s+earth\s*&\s*environment'),
    ('Scientific Reports', 'nature', r'10\.1038/s41598|scientific\s+reports'),
    ('Scientific Data', 'nature', r'10\.1038/s41597|www\.nature\.com/scientificdata'),
    ('Nature', 'nature', r'10\.1038/nature\d+|nature\s*\|\s*vol'),
    ('Science Advances', 'science', r'10\.1126/sciadv|sci\.\s*adv\.\s*20\d\d|s\s*c\s*i\s*e\s*n\s*c\s*e\s*a\s*d\s*v\s*a\s*n\s*c\s*e\s*s'),
    ('PNAS', 'pnas', r'10\.1073/pnas|www\.pnas\.org'),
    ('Environmental Research Letters', 'iop', r'10\.1088/1748-9326|environ(?:mental|\.)\s*res(?:earch|\.)\s*lett(?:ers|\.)'),
    ('Hydrology and Earth System Sciences', 'copernicus', r'10\.5194/hess-|hydrol\.\s*earth\s*syst\.\s*sci\.'),
    ('Biogeosciences', 'copernicus', r'10\.5194/bg-|biogeosciences,\s*\d'),
    ('Natural Hazards and Earth System Sciences', 'copernicus', r'10\.5194/nhess-|nat\.\s*hazards\s*earth\s*syst\.\s*sci\.'),
    ('Earth System Science Data', 'copernicus', r'10\.5194/essd-|earth\s*syst\.\s*sci\.\s*data'),
    ('Geophysical Research Letters', 'agu', r'10\.1029/\d{4}gl|geophysical\s+research\s+letters'),
    ('Journal of Geophysical Research: Atmospheres', 'agu', r'10\.1029/\d{4}jd|journal of\s+geophysical research:\s*atmospheres'),
    ("Earth's Future", 'agu', r"10\.1029/\d{4}ef\d+|earth[’']s\s+future"),
    ('Journal of Hydrometeorology', 'ams', r'10\.1175/jhm-|journal\s+of\s+hydrometeorology'),
    ('Journal of Hydrology', 'elsevier', r'10\.1016/j\.jhydrol\.|journal\s+of\s+hydrology|S00221694'),
    ('Earth-Science Reviews', 'elsevier', r'10\.1016/j\.earscirev\.|earth-science\s+reviews'),
    ('Atmospheric Research', 'elsevier', r'10\.1016/j\.atmosres\.|atmospheric\s+research'),
    ('International Journal of Applied Earth Observation and Geoinformation', 'elsevier', r'10\.1016/j\.jag\.|international\s+journal\s+of\s+applied\s+earth\s+observation'),
    ('International Journal of Climatology', 'wiley', r'10\.1002/joc\.|international\s+journal\s+of\s+climatology'),
    ('Climatic Change', 'springer', r'10\.1007/s10584|climatic\s+change\s*\(20'),
    ('Irrigation Science', 'springer', r'10\.1007/s00271|irrigation\s+science\s*\(20'),
    ('Climate Dynamics', 'springer', r'10\.1007/s00382|climate\s+dynamics\s*\(20'),
    ('Land', 'mdpi', r'10\.3390/land|www\.mdpi\.com/journal/land'),
    ('Journal of Hydrologic Engineering', 'asce', r'10\.1061/\(asce\)he\.|10\.1061/jhyeff|j\.\s*hydrol\.\s*eng\.'),
    ('Frontiers in Earth Science', 'frontiers', r'10\.3389/feart\.|frontiers\s+in\s+earth\s+science'),
    ('Frontiers in Environmental Science', 'frontiers', r'10\.3389/fenvs\.|frontiers\s+in\s+environmental\s+science'),
    ('Frontiers in Climate', 'frontiers', r'10\.3389/fclim\.|frontiers\s+in\s+climate'),
]

# 新增期刊使用可区分刊物的 DOI、官网路径或独立刊头。两个 Global Change
# Biology 刊头分别匹配，作者单位中的 Atmospheric Research 字样不构成刊名证据。
PUBLICATION_PROFILES = [
    ('Atmospheric Chemistry and Physics', 'copernicus', r'10\.5194/acp-|^[ \t]*(?:atmos\.[ \t]*chem\.[ \t]*phys\.|atmospheric chemistry and physics)(?=[ \t]*(?:,|$))'),
    ('Atmospheric Research', 'elsevier', r'10\.1016/j\.atmosres\.|elsevier\.com/locate/atmosres|^[ \t]*atmospheric research,?[ \t]+\d+[ \t]*\(\d{4}\)'),
    ('Global Biogeochemical Cycles', 'agu', r'10\.1029/\d{4}gb\d+|^[ \t]*global biogeochemical cycles(?=[ \t]*(?:,|\d|$))'),
    ('Philosophical Transactions of the Royal Society B: Biological Sciences', 'royal-society', r'10\.1098/rstb\.|royalsocietypublishing\.org/journal/rstb|^[ \t]*(?:phil\.[ \t]*trans\.[ \t]*r\.[ \t]*soc\.[ \t]*b|philosophical transactions of the royal society b(?:: biological sciences)?)\b'),
    ('Ecological Monographs', 'wiley', r'10\.1002/ecm\.|^[ \t]*ecological monographs\b(?=[ \t]*(?:[.,]|\d|$))'),
    ('Australian Journal of Botany', 'csiro', r'10\.1071/bt\d+|publish\.csiro\.au/(?:journals/)?ajb\b|^[ \t]*australian journal of botany(?=[ \t]*(?:,|\d|$))'),
    ('Global Change Biology', 'wiley', r'10\.1111/gcb\.|wileyonlinelibrary\.com/journal/gcb\b|^[ \t]*(?:glob change biol\.|global change biology)(?=[ \t]*(?:,|\d|$))'),
    ('Diversity and Distributions', 'wiley', r'10\.1111/ddi\.|^[ \t]*diversity and distributions(?=[ \t]*(?:,|\d|$))'),
    ('Global Change Biology Communications', 'wiley', r'10\.1002/gcb4\.|^[ \t]*(?:global change biology communications|global change bio\.[ \t]*comms\.)(?=[ \t]*(?:,|\d|$))'),
]
JOURNAL_RULES_VERSION = 2
DOI_LINE = re.compile(r'(?:doi\s*:\s*)?(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/\S+', re.I)
JOURNAL_SITE_LINE = re.compile(r'(?:journal homepage:\s*)?(?:https?://)?(?P<site>[a-z0-9][a-z0-9.-]*\.[a-z]{2,}/\S+)', re.I)


def journal_profiles(rules_version):
    if rules_version == 1:
        return PROFILES
    if rules_version == JOURNAL_RULES_VERSION:
        return [row for row in PROFILES if row[0] != 'Atmospheric Research'] + PUBLICATION_PROFILES
    raise ValueError(f'不支持的刊名规则版本：{rules_version}')


def _publication_matches(text, profiles):
    """仅接纳独立出版标记；正文叙述或引文中的 DOI 不是本刊证据。"""
    matches = {}
    for line in map(str.strip, text.splitlines()):
        doi = DOI_LINE.fullmatch(line)
        site = JOURNAL_SITE_LINE.fullmatch(line)
        for name, family, pattern in profiles:
            if name in matches or not (match := re.search(pattern, line, re.I)):
                continue
            token = match.group()
            tail = line[match.end():]
            masthead = (match.start() == 0 and not token.startswith('10.')
                        and (not tail.strip() or re.match(r'\s*[.,|]?\s*\d', tail)))
            own_site = (site and match.start() >= site.start('site')
                        and line[site.start('site'):match.start()].casefold() in {'', 'www.'})
            if masthead or doi and token.startswith('10.') or own_site:
                matches[name] = (family, token.strip())
    return matches


def identify_journal(source: Path, *, related=True, earths_future=True, rules_version=JOURNAL_RULES_VERSION) -> dict:
    """优先使用首页刊头和 DOI，再检查元数据及前部页证据。

    无刊名的 *_supplement 可核验同目录正文：正文元数据标题必须完整出现于
    补充材料首页，不能只凭相似文件名继承。单独移动的无标识附件返回 unknown。
    earths_future=False 仅供旧版面计划重算：旧计划尚无这项刊物适配，恢复时
    必须保持原来的刊物归属，不能使已有分类或计划身份因新增规则而变化。
    """
    profiles = [row for row in journal_profiles(rules_version) if earths_future or row[0] != "Earth's Future"]
    publication_names = {name for name, _, _ in PUBLICATION_PROFILES}
    with fitz.open(source) as doc:
        front = [p.get_text() for p in list(doc)[:3]]
        fields = [str(doc.metadata.get(k) or '') for k in ('subject', 'title')]
        metadata = (' ' if rules_version == 1 else '\n').join(fields)
        evidence = []
        if rules_version >= 2:
            # 首页上方 1/4 和页底 13% 包含已核对的刊头或出版链接；
            # 位置只用于排序证据，仍需独立刊头或链接，不能据此认定普通引文。
            first = doc[0]
            rect = first.rect
            header = first.get_text(clip=fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + .25 * rect.height))
            footer = first.get_text(clip=fitz.Rect(rect.x0, rect.y1 - .13 * rect.height, rect.x1, rect.y1))
            evidence.append((1, header + '\n' + footer, 'imprint'))
        evidence += [('metadata', metadata, 'metadata')] + [(i+1, t, 'page') for i, t in enumerate(front)]
    for page, text, location in evidence:
        matches = {}
        if rules_version >= 2 and location != 'metadata':
            direct = profiles if location == 'imprint' else [row for row in profiles if row[0] in publication_names]
            matches.update(_publication_matches(text, direct))
        if location != 'imprint':
            # 老规则在机构封面等场景需要拼接折行 DOI。新增规则的页面证据
            # 仍须是独立出版标记，不让正文 DOI 通过这一回退路径进入结果。
            text = re.sub(r'-\s*\n\s*', '', text)
            for name, family, pattern in profiles:
                if rules_version >= 2 and location == 'page' and name in publication_names:
                    continue
                if match := re.search(pattern, text, re.I | re.M):
                    matches[name] = (family, match.group().strip())
        # 一页中多刊物命中可能来自封面引用或正文讨论，保留冲突，不任意选第一项。
        if len(matches) == 1:
            name, (family, match) = next(iter(matches.items()))
            return {'name': name, 'family': family, 'evidence': match, 'page': page, 'rules_version': rules_version}
    if related and re.search(r'[_-]supplement$', source.stem, re.I):
        parent = source.with_name(re.sub(r'[_-]supplement$', '', source.stem, flags=re.I)+source.suffix)
        if parent.is_file():
            with fitz.open(parent) as doc:
                title = doc.metadata.get('title') or ''
            normalize = lambda v: re.sub(r'\W+', '', v).lower()
            if len(title) > 30 and normalize(title) in normalize(front[0]):
                match = identify_journal(parent, related=False, earths_future=earths_future, rules_version=rules_version)
                if match['family'] != 'generic':
                    return {**match, 'evidence': '补充材料标题与正文元数据一致：'+title,
                            'related_source': str(parent.resolve()), 'page': 1}
    return {'name': 'unknown', 'family': 'generic', 'evidence': '没有唯一刊名证据', 'rules_version': rules_version}


def auxiliary_page(text: str, number: int) -> str | None:
    """机构封面是出版附页，须同时命中来源标识及管理说明，且只限前三页。"""
    if number > 3:
        return None
    signatures = [('VU Research Portal', 'Take down policy'), ('HAL Id:', 'To cite this version:'),
                  ('centaur.reading.ac.uk', 'Reading'), ('Article 25fa', 'Dutch Copyright Act'),
                  ('To cite this article:', 'View the article online for updates')]
    if any(a.lower() in text.lower() and b.lower() in text.lower() for a, b in signatures):
        return '机构/下载封面原样保留'
    return None


def _same_rect(first, second, tolerance=.15):
    """表格检测会合并近重合边；允许亚点精度的坐标误差。"""
    return all(abs(a - b) <= tolerance for a, b in zip(first, second))


def _solid_rectangle(drawing):
    """出版社底色由纯填充矩形分块绘制，有时以三条线加隐式闭合表示。"""
    if drawing['type'] != 'f' or drawing['fill_opacity'] != 1:
        return False
    items = drawing['items']
    if len(items) == 1:
        return items[0][0] == 're'
    if len(items) not in {3, 4} or any(item[0] != 'l' for item in items):
        return False
    box = drawing['rect']
    corners = {(box.x0, box.y0), (box.x1, box.y0), (box.x1, box.y1), (box.x0, box.y1)}
    starts, ends = [item[1] for item in items], [item[2] for item in items]
    vertices = [*starts, ends[-1]] if len(items) == 3 else starts
    if (starts[1:] != ends[:-1] or len(items) == 4 and ends[-1] != starts[0]
            or {tuple(point) for point in vertices} != corners):
        return False
    return all(a != b and (a.x == b.x or a.y == b.y) for a, b in zip(vertices, [*vertices[1:], vertices[0]]))


def _abstract_panel(table, drawings, text_blocks, plain_boxes):
    """核对假表格恰由四个同色矩形组成，中间只有一个原生正文栏。"""
    box = fitz.Rect(table.bbox)
    if table.row_count != 2 or table.col_count != 3 or len(table.cells) != 4:
        return None
    # 向外扩 0.15 点读到零宽表格线。内部只允许四块原生底色，
    # 此证据不能用于排除任何带表格线、边框或其他向量内容的区域。
    involved = [d for d in drawings if box.intersects(d['rect'] + (-.15, -.15, .15, .15))]
    if len(involved) != 4 or not all(_solid_rectangle(d) for d in involved):
        return None
    color = involved[0]['fill']
    if not color or min(color) < .8 or any(d['fill'] != color for d in involved):
        return None
    if not all(sum(_same_rect(d['rect'], cell) for d in involved) == 1 for cell in table.cells):
        return None
    rows = table.extract()
    if [[bool(text and text.strip()) for text in row] for row in rows] != [[False, True, False], [False, False, False]]:
        return None

    cell = fitz.Rect(table.rows[0].cells[1]) + (-.15, -.15, .15, .15)
    runs = [block['lines'] for block in text_blocks if box.contains((block['bbox'].tl + block['bbox'].br) / 2)]
    if len(runs) != 1 or len(runs[0]) < 4:
        return None
    lines = runs[0]
    line_boxes = [fitz.Rect(line['bbox']) for line in lines]
    nearby = box + (-2, -2, 2, 2)
    plain_boxes = [region for region in plain_boxes if nearby.contains(region)]
    # 逐行以最左原生基线认定所属色块，用行中心核对视觉正文框。
    # 描述符可能将下缘画到基线下，最后一行基线仍属于上方正文格。
    if not all(line['dir'] == (1., 0.) and cell.contains(line['spans'][0]['origin'])
               and any(region.contains((line_box.tl + line_box.br) / 2) for region in plain_boxes)
               for line, line_box in zip(lines, line_boxes)):
        return None
    spans = [max(line['spans'], key=lambda span: len(span['text'])) for line in lines]
    style, count = Counter((s['font'].split('+', 1)[0], round(s['size'], 1), s['color'])
                           for s in spans).most_common(1)[0]
    if count < len(lines) * .8:
        return None
    return box, color, style, [''.join(s['text'] for s in line['spans']).strip() for line in lines]


def royal_society_abstract_tables(page, tables, detections):
    """返回新版计划可略过的原生表格框，保留 PDF 底色及其余图表证据。

    Phil. Trans. R. Soc. B 的续页把摘要置于左栏底部和右栏顶部。MuPDF
    将底色与留白各自的矩形边当作表格线。只接受同色、同文体的一对框，
    且右栏必须紧邻原生 Introduction 标题、语句从左至右连续。
    """
    if page.number != 1 or not tables:
        return set()
    regions = [r for r in detections if r['page'] == page.number + 1]
    plain_boxes = [fitz.Rect(r['rect']) for r in regions if r['kind'] == 'plain text']
    if not plain_boxes:
        return set()
    figures = [fitz.Rect(r['rect']) for r in regions if r['kind'] in {'table', 'figure'}]
    text_blocks = [{**block, 'bbox': fitz.Rect(block['bbox'])}
                   for block in page.get_text('dict', flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']
                   if block['type'] == 0]
    drawings = page.get_drawings()
    panels = [panel for table in tables if not any(box.intersects(table.bbox) for box in figures)
              if (panel := _abstract_panel(table, drawings, text_blocks, plain_boxes))]
    width, height = page.rect.width, page.rect.height
    left = [p for p in panels if p[0].x1 < width / 2 and p[0].y0 > .65 * height and p[0].y1 > .9 * height]
    right = [p for p in panels if p[0].x0 > width / 2 and p[0].y0 < .1 * height and p[0].y1 < .5 * height]
    if len(left) != 1 or len(right) != 1:
        return set()
    a, a_color, a_style, a_lines = left[0]
    b, b_color, b_style, b_lines = right[0]
    if (a_color != b_color or a_style != b_style or abs(a.width - b.width) > 1
            or not re.search(r'[A-Za-z]$', a_lines[-1]) or not re.match(r'[a-z]', b_lines[0])):
        return set()
    size = b_style[1]
    if not any(len(block['lines']) == 1 and abs(block['bbox'].x0 - b.x0) < 2
               and 0 < block['bbox'].y0 - b.y1 < 3 * size
               and re.fullmatch(r'1\s*[.)]\s*Introduction',
                                ''.join(s['text'] for s in block['lines'][0]['spans']).strip(), re.I)
               for block in text_blocks):
        return set()
    return {tuple(a), tuple(b)}


def csiro_column_gutter(width, lines):
    """以两边长行之间的实测间隙确定 CSIRO 的偏心双栏分界。"""
    # 期刊的内外边距随奇偶页换边。正文宽约 0.4 页，两栏各须有
    # 至少三条长行且中间留白，避免把通栏标题或页码当作正文栏。
    spans = [line[:4] for line in lines if len(line[4]) >= 40 and .32 * width <= line[2]-line[0] <= .46 * width]
    left = [x1 for x0, _, x1, _ in spans if x0 < .2 * width]
    right = [x0 for x0, _, _, _ in spans if .43 * width < x0 < .62 * width]
    if len(left) >= 3 and len(right) >= 3 and min(right) - max(left) >= .01 * width:
        return (max(left) + min(right)) / 2
    return None


def _native_text_blocks(page):
    """从横排 PDF 文本取实际文字流和字体样式，坐标用点。"""
    for block in page.get_text('dict', flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
        lines = block.get('lines', [])
        if lines and all(line['dir'] == (1., 0.) for line in lines):
            text = ' '.join(''.join(span['text'] for span in line['spans']).strip() for line in lines)
            yield fitz.Rect(block['bbox']), lines, text.strip()


def _csiro_affiliations(lines, text):
    """至少三项升起的小号字母编号和机构名称共同证明作者单位。"""
    letters = set()
    for line in lines:
        if len(line['spans']) < 2:
            continue
        reference, address = line['spans'][:2]
        if (re.fullmatch(r'[A-Z]', reference['text'].strip()) and reference['size'] < .85 * address['size']
                and address['origin'][1] - reference['origin'][1] > .2 * address['size']):
            letters.add(reference['text'].strip())
    return len(letters) >= 3 and len(re.findall(
        r'(?<![a-z])(?:Department|University|Centre|Center|School|Institute|Laboratory)\b', text)) >= 3


def csiro_front_matter(document, detections):
    """核实跨页摘要、紧邻的署名和单位，忽略与正文同坐标的较弱弃用框。

    适用于文章前两页（可有勘误扉页）的通栏摘要。英文显式 Abstract
    标题、未结束的句子、下一页同宽同字体续文和关键词必须同时存在。
    只有与正文视觉框完全重叠且置信度较低的 abandon 框被移除。返回首段
    页码、原生摘要框、弃用框集合，以及依据 PDF 证明的前置信息框。
    """
    from statistics import median

    def style(lines):
        spans = [span for line in lines for span in line['spans'] if span['text'].strip()]
        font = Counter(span['font'].split('+', 1)[0] for span in spans).most_common(1)[0][0]
        return font, round(median(span['size'] for span in spans), 1)

    for index in range(min(2, len(document)-1)):
        page = document[index]
        width, height = page.rect.width, page.rect.height
        current = list(_native_text_blocks(page))
        abstracts = [(box, lines, text) for box, lines, text in current
                     if box.width > .6 * width and box.y0 > .65 * height and len(lines) >= 3
                     and re.match(r'^Abstract[.:]\s+', text, re.I) and re.search(r'[A-Za-z]$', text)
                     and (lines[0]['spans'][0]['flags'] & 16
                          or re.search(r'(?:Bold|[.]B)(?:\+.*)?$', lines[0]['spans'][0]['font']))]
        if len(abstracts) != 1:
            continue
        abstract, lines, _ = abstracts[0]
        abstract_style = style(lines)
        following = sorted(_native_text_blocks(document[index+1]), key=lambda block: block[0].y0)
        candidates = [(box, spans) for box, spans, text in following if abs(box.width-abstract.width) < .06 * width
                      and box.y0 < .2 * height and len(spans) >= 3 and re.match(r'[a-z]', text)
                      and style(spans) == abstract_style
                      and any(0 < other.y0 - box.y1 < 3 * abstract_style[1]
                              and re.match(r'^Keywords?[:.]', value, re.I) for other, _, value in following)]
        if len(candidates) != 1:
            continue
        continuation = candidates[0][0]
        number = index + 1
        plain = [r for r in detections if r['page'] == number and r['kind'] == 'plain text'
                 and fitz.Rect(r['rect']).contains(abstract)]
        ignore = {tuple(r['rect']) for r in detections if r['page'] == number and r['kind'] == 'abandon'
                  and any(_same_rect(r['rect'], t['rect'], tolerance=.5)
                          and r.get('confidence', 1) < t.get('confidence', 1) for t in plain)}
        preserved = {}
        affiliations = [box for box, spans, text in current if 0 < abstract.y0-box.y1 < .1 * height
                        and abs(abstract.x0-box.x0) < 2 and _csiro_affiliations(spans, text)]
        if len(affiliations) == 1:
            address = affiliations[0]
            authors = [box for box, _, text in current
                       if 0 < address.y0-box.y1 < .06 * height and abs(address.x0-box.x0) < 2
                       and len(re.findall(r'\b(?:[A-Z]\.\s*)+[A-Z][a-z]+', text)) >= 3
                       and text.count(',') >= 3]
            if len(authors) == 1:
                preserved[number] = [(authors[0], 'CSIRO 作者原样保留'), (address, 'CSIRO 作者单位原样保留')]
        dates = [box for box, _, text in following if continuation.y1 < box.y0 < .28 * height
                 and re.fullmatch(r'Received \d{1,2} [A-Za-z]+ \d{4}, accepted \d{1,2} [A-Za-z]+ \d{4}, '
                                  r'published online \d{1,2} [A-Za-z]+ \d{4}', text, re.I)]
        if len(dates) == 1:
            preserved[number + 1] = [(dates[0], 'CSIRO 稿件时间原样保留')]
        return number, abstract, ignore, preserved
    return 0, None, set(), {}


def preserve_line(profile: dict, page, line: dict, cover_reason: str | None, *, publication_labels=False) -> str | None:
    """期刊专属规则只作用于明确辅助信息；轴标签/正文旋转文字不因旋转被跳过。"""
    if cover_reason:
        return cover_reason
    family = profile['family']
    text = ' '.join(''.join(c['c'] for c in s['chars']) for s in line['spans']).strip()
    box = fitz.Rect(line['bbox'])
    edge = box.x1 < 35 or box.x0 > page.rect.width-35
    marginal = box.y1 < 65 or box.y0 > page.rect.height-55
    if family in {'asce', 'agu', 'wiley', 'pnas', 'science', 'ams'} and (edge or marginal):
        if re.search(r'downloaded|terms (?:and|&) conditions|rights reserved|personal use|unauthenticated', text, re.I):
            return f'{family} 下载/版权边栏原样保留'
    if (publication_labels and family == 'science' and page.number == 0 and box.y1 < .2*page.rect.height
            and re.fullmatch(r'(?:[A-Z] ){4,}[A-Z]{1,3}', text)):
        return 'Science 字距展开的学科分类原样保留'
    if family == 'pnas' and edge and re.fullmatch(r'EARTH, ATMOSPHERIC,|AND PLANETARY SCIENCES', text):
        return 'PNAS 学科分类边栏原样保留'
    if family == 'science' and edge and re.match(r'^on [A-Z][a-z]+ \d{1,2}, \d{4}$', text):
        return 'Science 下载时间边栏原样保留'
    if family == 'nature' and re.fullmatch(r'[\d():,;\s]+', text) and '1234567890' in text:
        return 'Nature 排印校验字符原样保留'
    if marginal and family != 'generic':
        own_pattern = next((p for n, _, p in journal_profiles(profile.get('rules_version', 1)) if n == profile['name']), r'(?!)')
        if re.search(own_pattern, text, re.I) or re.match(r'^(?:©|Copyright|Published by Copernicus|Contents lists available|journal homepage:)', text, re.I):
            return f'{family} 刊名/出版页眉页脚原样保留'
    if family == 'elsevier' and re.match(r'^(?:Contents lists available at ScienceDirect|journal homepage:)', text, re.I):
        return 'Elsevier 出版信息原样保留'
    if family == 'frontiers' and re.match(r'^(?:TYPE |PUBLISHED |DOI 10\.3389/)', text):
        return 'Frontiers 稿件元数据原样保留'
    if (family == 'royal-society' and page.number == 0 and box.y1 < .25*page.rect.height
            and re.fullmatch(r'(?:https?://)?(?:www\.)?royalsocietypublishing\.org/journal/rstb/?', text, re.I)):
        return 'Royal Society 首页刊物官网原样保留'
    return None


def publisher_style_flags(family: str, font: str, flags: int, *, science_styles=False) -> int:
    """恢复 ASCE AdvOT 和启用的新 Science AdvTT 明示样式，不猜测无后缀字体。"""
    match = re.fullmatch(r'AdvOT[^.]+\.(BI|B|I)', font) if family == 'asce' else None
    if science_styles and family == 'science':
        match = re.fullmatch(r'AdvTT[^.]+\.(BI|B|I)', font)
    return flags | ((16 if 'B' in match[1] else 0) | (2 if 'I' in match[1] else 0)) if match else flags
