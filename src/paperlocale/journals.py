"""以源 PDF 的刊名/DOI 证据选择版式规则，不按文件名猜期刊。

识别与版面分类分开：同刊的作者稿、补充材料不强套正式双栏模板；只保留
有几何和文字证据的出版辅助信息。未知/冲突来源使用通用规则，不删除正文。
所有坐标为 PyMuPDF 点坐标，页码从 1 开始；原对象只保护、不擦除。
"""
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


def identify_journal(source: Path, *, related=True) -> dict:
    """优先元数据与前部页证据；不扫描末尾书目来判所属期刊。

    无刊名的 *_supplement 可核验同目录正文：正文元数据标题必须完整出现于
    补充材料首页，不能只凭相似文件名继承。单独移动的无标识附件返回 unknown。
    """
    with fitz.open(source) as doc:
        front = [p.get_text() for p in list(doc)[:3]]
        metadata = ' '.join(str(doc.metadata.get(k) or '') for k in ('subject', 'title'))
    evidence = [('metadata', metadata)] + [(i+1, t) for i, t in enumerate(front)]
    for page, text in evidence:
        text = re.sub(r'-\s*\n\s*', '', text)
        matches = [(name, family, re.search(pattern, text, re.I)) for name, family, pattern in PROFILES]
        matches = [x for x in matches if x[2]]
        # 一页中多刊物命中可能来自封面引用或正文讨论，保留冲突，不任意选第一项。
        if len(matches) == 1:
            name, family, match = matches[0]
            return {'name': name, 'family': family, 'evidence': match.group(), 'page': page, 'rules_version': 1}
    if related and re.search(r'[_-]supplement$', source.stem, re.I):
        parent = source.with_name(re.sub(r'[_-]supplement$', '', source.stem, flags=re.I)+source.suffix)
        if parent.is_file():
            with fitz.open(parent) as doc:
                title = doc.metadata.get('title') or ''
            normalize = lambda v: re.sub(r'\W+', '', v).lower()
            if len(title) > 30 and normalize(title) in normalize(front[0]):
                match = identify_journal(parent, related=False)
                if match['family'] != 'generic':
                    return {**match, 'evidence': '补充材料标题与正文元数据一致：'+title,
                            'related_source': str(parent.resolve()), 'page': 1}
    return {'name': 'unknown', 'family': 'generic', 'evidence': '没有唯一刊名证据', 'rules_version': 1}


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
        own_pattern = next((p for n, _, p in PROFILES if n == profile['name']), r'(?!)')
        if re.search(own_pattern, text, re.I) or re.match(r'^(?:©|Copyright|Published by Copernicus|Contents lists available|journal homepage:)', text, re.I):
            return f'{family} 刊名/出版页眉页脚原样保留'
    if family == 'elsevier' and re.match(r'^(?:Contents lists available at ScienceDirect|journal homepage:)', text, re.I):
        return 'Elsevier 出版信息原样保留'
    if family == 'frontiers' and re.match(r'^(?:TYPE |PUBLISHED |DOI 10\.3389/)', text):
        return 'Frontiers 稿件元数据原样保留'
    return None


def publisher_style_flags(family: str, font: str, flags: int, *, science_styles=False) -> int:
    """恢复 ASCE AdvOT 和启用的新 Science AdvTT 明示样式，不猜测无后缀字体。"""
    match = re.fullmatch(r'AdvOT[^.]+\.(BI|B|I)', font) if family == 'asce' else None
    if science_styles and family == 'science':
        match = re.fullmatch(r'AdvTT[^.]+\.(BI|B|I)', font)
    return flags | ((16 if 'B' in match[1] else 0) | (2 if 'I' in match[1] else 0)) if match else flags
