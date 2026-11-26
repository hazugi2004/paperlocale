"""翻译片段的稳定身份与科学信息完整性门禁。

PDF 版面引擎通常把公式和富文本替换为占位符后交给翻译器。如果模型删除
占位符而流水线继续渲染，最终 PDF 会直接缺公式。本模块把这类风险提升为
硬错误，并同时保护数字、单位、缩写、URL、DOI 和领域术语。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from .domains import DomainPack, GlossaryEntry
from .quantities import find_quantities, hide_spans, standalone_units

FORMULA_RE = re.compile(r"\{v\d+\}")
STYLE_RE = re.compile(r"<style\s+id=['\"]\d+['\"]>|</style>", re.IGNORECASE)
# 中文译文常把“获取”“下载”等词直接接在 URL 或 DOI 后。汉字必须作为
# 标识符边界，否则完整保留的网址会因中文后缀而被误判为篡改。
URL_RE = re.compile(
    r"https?://[^\s)\]}>,;，。；：！？）】》\u3400-\u9fff]+", re.IGNORECASE
)
DOI_RE = re.compile(
    r"\b10\.\d{4,9}/[^\s)\]}>,;，。；：！？）】》\u3400-\u9fff]+", re.IGNORECASE
)
# PDF 文本层常把引文号和产品版本紧贴在英文名后，如
# ``productivity3,17,18`` 和 ``NDVI3g``。第二个零宽分支允许数字紧跟
# ASCII 字母；正负号仍只能由第一分支接管，因此 ``mid-1960s``
# 中的连字符不会被误判为负号。
NUMBER_RE = re.compile(
    r"(?:(?<![\dA-Za-z])[-+−]?|(?<=[A-Za-z]))"
    r"\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?"
)
ABBREVIATION_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z][A-Z0-9-]{1,})(?![A-Za-z0-9])")
# 多段下划线字段是可直接用于数据查询的标识符，必须保留完整词元和次数。
DATA_FIELD_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z][A-Z0-9]*){3,}(?![A-Za-z0-9_])")
# 真实论文常把章节名、出版元数据和图件标签排成全大写。它们是应翻译的
# 普通英语词，而不是必须逐字保留的变量缩写。这里只列入批量运行中已经
# 观察到的明确类别；NDVI、MOE、DH、CO 等科学标记仍继续走硬门禁。
TRANSLATABLE_UPPERCASE_WORDS = frozenset(
    {
        "ABSTRACT",
        "ACCEPTED",
        "ACCESS",
        "ACKNOWLEDGMENTS",
        "AND",
        "APR",
        "APRIL",
        "ARTICLE",
        "AUG",
        "AUGUST",
        "AUTHOR",
        "AVAILABILITY",
        "BY",
        "CITATION",
        "CONFLICT",
        "CONNECTING",
        "CONCLUSION",
        "CONCLUSIONS",
        "CONTRIBUTIONS",
        "COPULA-BASED",
        "COPYRIGHT",
        "CORRESPONDENCE",
        "DATA",
        "DEC",
        "DECEMBER",
        "DISCUSSION",
        "EDITED",
        "ESTABLISHING",
        "EXTREMES",
        "FEB",
        "FEBRUARY",
        "FIGURE",
        "FOR",
        "FUNDING",
        "INFORMATION",
        "INTEREST",
        "INTRODUCTION",
        "JAN",
        "JANUARY",
        "JUL",
        "JULY",
        "JUN",
        "JUNE",
        "KEYWORDS",
        "LETTER",
        "MAR",
        "MARCH",
        "MATERIAL",
        "MATERIALS",
        "MAY",
        "METHODS",
        "MODELLED",
        "NOV",
        "NOVEMBER",
        "OCT",
        "OCTOBER",
        "OF",
        "OPEN",
        "ORIGINAL",
        "PATTERNS",
        "PERMISSIONS",
        "PRECIPITATION",
        "PUBLICATION",
        "PUBLISHED",
        "RECEIVED",
        "REFERENCES",
        "RESEARCH",
        "RESULTS",
        "REVISED",
        "REVIEWED",
        "SEP",
        "SEPT",
        "SEPTEMBER",
        "SILOS",
        "SPATIO-TEMPORAL",
        "STATEMENT",
        "STATEMENTS",
        "SUPPLEMENTARY",
        "SUPPORTING",
        "TOOLS",
        "TYPE",
        "TYPES",
        "VOL",
    }
)
# 国家缩写在机构地址和普通正文中可自然译为中文国名；继续要求保留其英文
# 表面形式会把正确的“美国”“英国”误判为信息丢失。
GEOGRAPHIC_ABBREVIATION_EXCLUSIONS = frozenset({"US", "USA", "UK"})
ADDRESS_REGION_RE = re.compile(r"\b([A-Z]{2})(?=,\s*USA\b)")
UNIT_RE = re.compile(
    # 边界拒绝与任意非中日韩文字母粘连，避免把 Météorologiques 的 M
    # 识别成 metre；同时允许中文译文直接写成“10 mm降水”。单位大小写
    # 具有科学含义，因此不能再对整条正则使用 IGNORECASE。
    r"(?<![^\W\d_\u3400-\u9fff])(?:%|°[CF]?|mm|cm|m|km|Pa|hPa|K|W\s*m-?2|"
    r"g\s*C\s*m-?2(?:\s*d-?1)?|µmol\s*m-?2\s*s-?1)(?![^\W\d_\u3400-\u9fff])",
)
CJK_RE = re.compile(r"[\u3400-\u9fff]")
ENGLISH_RE = re.compile(r"[A-Za-z]")
TRAILING_PUNCTUATION = ".,;:!?。，、；：！？"


def normalize_text(text: str) -> str:
    """统一换行和外围空白，避免平台差异改变片段身份。"""

    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def segment_id(text: str) -> str:
    """用规范化原文生成稳定 SHA-256；相同片段可安全命中断点译文。"""

    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _clean_identifiers(values: list[str]) -> Counter[str]:
    """去除 URL/DOI 末尾普通句末标点，允许中英文句号自然替换。"""

    return Counter(value.rstrip(TRAILING_PUNCTUATION) for value in values)


def protected_counts(text: str, *, domain: DomainPack | None = None) -> dict[str, Counter[str]]:
    """提取必须保留的标记及出现次数。"""

    # DOI 链接常在列宽边界被拆成 ``https://doi.org/ 10.x/...``；先只合并
    # 这一种确定性 URL 断行，使译文中的正常完整链接不被误报为篡改。
    identifier_text = re.sub(
        r"(https?://doi\.org/)\s+(10\.\d{4,9}/[^\s)\]}>,;，。；：！？）】》\u3400-\u9fff]+)",
        r"\1\2",
        text,
        flags=re.IGNORECASE,
    )
    # PDF 版面引擎可能在连字符后插入断行空格，例如 ``HDI- MSDI`` 或
    # ``SSP5- 8.5``。缩写门禁只消除这一种版面空格，使正确合并后的译文
    # 仍可通过；原始文字、数字和其他标点检查保持不变。
    # 只拆分从词首开始的 ``andHDI`` 版面粘连。旧规则会在期刊艺术字
    # ``ChaNGE`` 内部制造不存在的 ``NGE`` 缩写。
    abbreviation_text = re.sub(
        r"(?<![A-Za-z0-9])([a-z]+)(?=[A-Z]{2})",
        r"\1 ",
        glossary_abbreviation_text(text, domain),
    )
    abbreviation_text = re.sub(
        r"(?<=[A-Z0-9])-\s+(?=[A-Z0-9])",
        "-",
        abbreviation_text,
    )
    # SCOPE 也是真实模型名。只有处于普通并列标题的末项时，才按“范围”识别。
    abbreviation_text = re.sub(
        r"(\bDATA TYPES,\s+CONTRIBUTIONS,\s+AND )SCOPE(?=[.。]?\s*[)）]?\s*$)",
        r"\1scope",
        abbreviation_text,
    )
    address_regions = set(ADDRESS_REGION_RE.findall(abbreviation_text))
    abbreviation_exclusions = (
        TRANSLATABLE_UPPERCASE_WORDS
        | GEOGRAPHIC_ABBREVIATION_EXCLUSIONS
        | address_regions
    )
    abbreviations = (
        value
        for value in ABBREVIATION_RE.findall(abbreviation_text)
        if value not in abbreviation_exclusions
    )
    return {
        "formula": Counter(FORMULA_RE.findall(text)),
        "style": Counter(STYLE_RE.findall(text)),
        "url": _clean_identifiers(URL_RE.findall(identifier_text)),
        "doi": _clean_identifiers(DOI_RE.findall(identifier_text)),
        "data_field": Counter(DATA_FIELD_RE.findall(text)),
        "number": Counter(NUMBER_RE.findall(text)),
        "abbreviation": Counter(abbreviations),
        "unit": Counter(match.group(0) for match in UNIT_RE.finditer(text)),
    }


_TERM_SEPARATOR = r"[\s\u2010\u2011\u2012\u2013-]+"


@lru_cache(maxsize=4096)
def _term_pattern(term: str, legacy: bool) -> re.Pattern[str]:
    # 使用实际词边界排除 spatio-temporal 等复合词的后半截。保留原词形，
    # 允许 PDF 文本层的空白和连接号变体，以及明确可复核的末词复数。
    left_boundary = r"(?<![A-Za-z0-9_\u2010\u2011\u2012\u2013-])"
    if legacy:
        # 已记录 1.1.0 的旧运行沿用原先的命中条件，避免补跑时新增术语要求。
        return re.compile(r"(?<![A-Za-z0-9\-\u2010\u2011])" + re.escape(term), re.IGNORECASE)
    words = re.split(_TERM_SEPARATOR, term)
    last = words[-1]
    plural = {"analysis": "analyses", "index": "indices", "matrix": "matrices"}.get(last.lower())
    if plural:
        tail = rf"(?:{re.escape(last)}|{plural})"
    elif last.islower() and last.isalpha() and re.search(r"[^aeiou]y$", last):
        tail = re.escape(last[:-1]) + r"(?:y|ies)"
    elif last.islower() and last.isalpha() and not last.endswith("s"):
        suffix = "es" if last.endswith(("x", "ch", "sh")) else "s"
        tail = re.escape(last) + rf"(?:{suffix})?"
    else:
        tail = re.escape(last)
    pattern = _TERM_SEPARATOR.join([*(re.escape(word) for word in words[:-1]), tail])
    return re.compile(left_boundary + pattern + r"(?![A-Za-z0-9_])", re.IGNORECASE)


def source_term_is_present(text: str, term: str) -> bool:
    """判断原文是否出现给定词形或受支持的复数。"""

    return _term_pattern(term, False).search(text) is not None


def _legacy_glossary(domain: DomainPack) -> bool:
    return domain.pack_id == "atmospheric-science" and domain.version == "1.1.0"


def _glossary_hits(text: str, domain: DomainPack) -> list[tuple[re.Match[str], GlossaryEntry]]:
    legacy = _legacy_glossary(domain)
    if not legacy:
        # 屏蔽网址内部，并保留坐标，供保护原文缩写时定位大小写来源。
        def hide_address(match: re.Match[str]) -> str:
            return "|" * len(match.group())

        text = DOI_RE.sub(hide_address, _VALIDATION_URL_RE.sub(hide_address, text))
    matches = [
        (match, entry)
        for entry in domain.glossary
        for match in _term_pattern(entry.source, legacy).finditer(text)
    ]
    matched = []
    end = 0
    # 同一处「reference crop evapotranspiration」使用完整词条。短词在同段
    # 的其他位置独立出现时仍检查，词库顺序不决定长短词的适用范围。
    for match, entry in sorted(matches, key=lambda item: (item[0].start(), -item[0].end())):
        if legacy or match.start() >= end:
            matched.append((match, entry))
            end = match.end()
    return matched


def match_glossary(text: str, domain: DomainPack) -> list[tuple[str, GlossaryEntry]]:
    """按首次出现顺序返回不重复的实际词形和词条，供请求与验收共用。"""

    return list(dict.fromkeys((match.group(), entry) for match, entry in _glossary_hits(text, domain)))


def glossary_abbreviation_text(text: str, domain: DomainPack | None) -> str:
    """为缩写识别调整已知术语中的排版大写，保持原文字符坐标。"""

    if domain is None or _legacy_glossary(domain) or ABBREVIATION_RE.search(text) is None:
        return text
    pieces = []
    start = 0
    for match, entry in _glossary_hits(text, domain):
        # 全大写图题中的 SOIL MOISTURE 是术语。词条内显式登记的缩写、
        # 词条之外出现的缩写及所有数字仍按各自的原规则保护。
        known_abbreviations = set(ABBREVIATION_RE.findall(entry.source))

        def normalize_token(token: re.Match[str]) -> str:
            value = token.group()
            return value if value in known_abbreviations else value.lower()

        pieces.extend((text[start:match.start()], ABBREVIATION_RE.sub(normalize_token, match.group())))
        start = match.end()
    pieces.append(text[start:])
    return "".join(pieces)


def scientific_quantities(text: str):
    """在翻译链路统一读取数字、单位、完整统计分母及其原文位置。"""
    excluded = [match.span() for pattern in (URL_RE, DOI_RE, FORMULA_RE, STYLE_RE)
                for match in pattern.finditer(text)]
    return find_quantities(text, excluded, statistics=True)


def unresolved_statistical_denominator(source: str) -> str | None:
    """只有一处无法自动解释的分母时，才给出可在同一位置手工确认的源文短语。"""
    unresolved = [quantity.unknown_denominator for quantity in scientific_quantities(source)
                  if quantity.unknown_denominator is not None]
    return unresolved[0] if len(unresolved) == 1 else None


def _has_local_denominator(text: str, start: int, end: int) -> bool:
    """确认只能解决分母含义不确定；译文必须在同一处明确写出统计对象。"""
    before = text[max(0, start - 48):start]
    after = text[end:end + 48]
    return bool(re.search(r'每[\u3400-\u9fff \t]{2,32}$', before)
                or re.match(r'[ \t]*[/／][ \t]*[^\W\d_]', after))


def _anchor_sentence_break(text: str, match: re.Match[str], anchors: Mapping[str, str]) -> bool:
    # 只放行闭合的量纲后接普通句号、空格和新主语的形式。PDF 的空格可能
    # 在下一个固定锚点内。没有原字形证据或语法不明确时，仍保护小数点。
    left, _, right = match.group().partition('.')
    unit = anchors.get(left, '').strip()
    if not (unit.startswith('(') and unit.endswith(')')
            and re.fullmatch(r'\s+[^\W\d]\w*', anchors.get(right, ''))
            and re.match(r'\s+(?:is|are|was|were)\b', text[match.end():])):
        return False
    quantity = '1 ' + unit[1:-1]
    return any(q.start == 0 and q.end == len(quantity) for q in scientific_quantities(quantity))


def scientific_literal_spans(text: str, *, anchor_text: Mapping[str, str] | None = None) -> list[tuple[int, int]]:
    """保护版面占位符拆开的数值，以及语法明确的整行数学表达式。

    只处理有公式占位符的表达式；正文中的小写词不推断为变量。完整函数式
    必须由标识符、标记和数学分隔符构成，不能含相邻自然语言单词。
    """
    if not FORMULA_RE.search(text):
        return []
    token = r"(?:\d+|\{v\d+\})"
    decimals = list(re.finditer(token + r"(?:\." + token + r")+", text))
    math_only = (re.match(r"^[A-Za-z_]\w*\(", text.strip())
                 and re.fullmatch(r"(?:\{v\d+\}|[A-Za-z0-9_]|[\s,().+*/=<>≤≥−-])+", text)
                 and not re.search(r"[A-Za-z_]\w*\s+[A-Za-z_]\w*", FORMULA_RE.sub("@", text)))
    numeric_only = re.fullmatch(r"(?:\{v\d+\}|[0-9\s.,+~<>≤≥−-])+", text)
    if math_only or numeric_only:
        return [(0, len(text))]
    return [match.span() for match in decimals
            if not anchor_text or not _anchor_sentence_break(text, match, anchor_text)]


# 仅供门禁使用：不改变 Provider 的原文、保护范围、请求或重试策略。
# 无协议的 www 地址与带协议网址同样必须逐字保留；不把普通英文正文当网址。
_VALIDATION_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s)\]}>,;，。；：！？）】》\u3400-\u9fff]+", re.IGNORECASE
)


def _quantity_validation_text(text: str) -> str:
    """将面积/体积名称和明确倍率规范到既有量值语法，不做物理换算。

    此副本只用于校验，绝不写回译文；倍率仍绑定原来的数值和单位，避免
    million 遗漏或平方变长度被当成等价译法。单独的 million 普通用词不处理。
    """
    units = {"kilometer": "km", "kilometre": "km", "meter": "m", "metre": "m"}
    # 中文把频率分母放在数值之前；仅规范明确的“每年…N次”结构，
    # 比较词和数值原样保留，不把相隔任意句子的“年”“事件”拼成单位。
    text = re.sub(r'每(十年|年|天|日|小时)(平均)?(不到|少于|超过|约)?\s*(\d+(?:\.\d+)?)\s*(?:次事件|个事件|次|事件)(?![/每])',
                  lambda m: (m[2] or '') + (m[3] or '') + m[4] + ' event/' + m[1], text)
    text = re.sub(
        r"\b(square|cubic)\s+(kilometers?|kilometres?|meters?|metres?)\b",
        lambda m: units[m[2].removesuffix("s")] + ("²" if m[1] == "square" else "³"),
        text,
    )
    # 仅在数值后、明确面积/体积单位前识别倍率，保留范围两端与精度。
    scales = {"million": "6", "百万": "6", "billion": "9", "十亿": "9", "thousand": "3", "千": "3"}
    return re.sub(
        r"(?<=\d)\s*(million|billion|thousand|百万|十亿|千)\s*(?=(?:km|m)[²³]|平方(?:千米|公里|米)|立方米)",
        lambda m: "×10^" + scales[m[1]] + " ", text,
    )


def validate_translation(
    source: str,
    target: str,
    domain: DomainPack | None = None,
    *,
    require_cjk: bool = True,
    confirmed_denominator: str | None = None,
    anchor_text: Mapping[str, str] | None = None,
) -> list[str]:
    """返回全部合同错误；空列表才允许译文进入渲染阶段。"""

    errors: list[str] = []
    if not target.strip():
        return ["译文为空"]

    # C0/C1/DEL 是控制码，不是科学符号。拒绝而不删除：字符若来自源 PDF，
    # 需要先核对源字形；若模型新添，则由重试或人工修订纠正，避免到排版才报缺字。
    import unicodedata
    for index, character in enumerate(target):
        if unicodedata.category(character) == 'Cc' and character not in '\n\r\t':
            context = target[max(0, index-18):index+19]
            origin = '原文也包含此字符，须核对源字形' if character in source else '原文不包含此字符'
            errors.append(f"character 不可见控制字符 U+{ord(character):04X}，译文第 {index+1} 字符；{origin}；上下文 {context!r}")
    source_counts = protected_counts(source)
    target_counts = protected_counts(target)
    for category in ("formula", "style", "url", "doi", "data_field"):
        if source_counts[category] != target_counts[category]:
            errors.append(
                f"{category} 标记不一致：期望 {dict(source_counts[category])!r}，"
                f"实际 {dict(target_counts[category])!r}"
            )

    # 科学表达式内部的小数点和小写变量不能翻译成句号或人名。
    literal_spans = scientific_literal_spans(source, anchor_text=anchor_text)
    target_compact = re.sub(r"\s+", "", target)
    for start, end in literal_spans:
        literal = re.sub(r"\s+", "", source[start:end])
        if target_compact.count(literal) < re.sub(r"\s+", "", source).count(literal):
            errors.append(f"scientific_literal 科学表达式改变：{source[start:end]!r}")

    # 数值与单位必须成对一致，不能靠交换两处单位骗过独立计数。
    # 仅做别名/乘除幂表示归一；不换算hPa到Pa或摄氏度到K。
    quantity_source = _quantity_validation_text(source)
    quantity_target = _quantity_validation_text(target)
    source_quantities = scientific_quantities(quantity_source)
    target_quantities = scientific_quantities(quantity_target)
    required = Counter(q.signature for q in source_quantities)
    available = Counter(q.signature for q in target_quantities)
    missing_quantities = required - available
    if missing_quantities:
        errors.append(f"quantity 数值/单位配对缺失或改变：{dict(missing_quantities)!r}；译文配对：{dict(available)!r}")
    # 未掌握的分母可原样引用；若需中文改写，确认须绑定唯一的源文对象和该数值。
    # 底层数值/单位配对、已识别的分母和其他内容检查始终照常执行。
    unknown_source = [q for q in source_quantities if q.unknown_denominator is not None]
    unknown_target = Counter((q.signature, q.unknown_denominator.casefold())
                             for q in target_quantities if q.unknown_denominator is not None)
    for quantity in unknown_source:
        key = (quantity.signature, quantity.unknown_denominator.casefold())
        if unknown_target[key]:
            unknown_target[key] -= 1
            continue
        matches = [target for target in target_quantities if target.values == quantity.values]
        if (len(unknown_source) == 1 and confirmed_denominator == quantity.unknown_denominator
                and len(matches) == 1 and matches[0].signature == quantity.signature
                and matches[0].unknown_denominator is None
                and _has_local_denominator(quantity_target, matches[0].start, matches[0].end)):
            continue
        errors.append(f"quantity_review 统计分母待核对：原文“{quantity.unknown_denominator}”；"
                      f"请核对对应的 {quantity.signature} 按什么对象统计。")
    source_free = hide_spans(quantity_source, [(q.start, q.end) for q in source_quantities])
    # 未匹配的目标物理量不能吞掉普通原文数字，例如15°N译成北纬15度。
    matched = required.copy()
    target_spans = []
    for quantity in target_quantities:
        if matched[quantity.signature] > 0:
            target_spans.append((quantity.start, quantity.end))
            matched[quantity.signature] -= 1
    target_free = hide_spans(quantity_target, target_spans)
    source_remaining = protected_counts(source_free, domain=domain)
    target_remaining = protected_counts(target_free)
    # “每年的秒数”与“seconds per year”是同一单位关系。只在独立单位计数
    # 时规范分母前置写法，避免把相邻数字拼成新的数值/单位对，也不改译文。
    for remaining, text in ((source_remaining, source_free), (target_remaining, target_free)):
        text = re.sub(r"每([年天日])的?秒数", r"秒/\g<1>", text)
        remaining["unit"] = Counter(label for _a, _b, label in standalone_units(text))
    # 非物理量区域仍使用既有科学标记门禁，不放宽数字、变量及公式要求。
    for category in ("number", "abbreviation", "unit"):
        missing = source_remaining[category] - target_remaining[category]
        if missing:
            # 用同一校验计数给出缺口证据；只列失败标记，不改变门禁判定。
            expected = {token: source_remaining[category][token] for token in missing}
            actual = {token: target_remaining[category][token] for token in missing}
            errors.append(f"{category} 标记缺失：{dict(missing)!r}；原文要求 {expected!r}；译文实际 {actual!r}")

    if STYLE_RE.findall(source) != STYLE_RE.findall(target):
        errors.append(f"style 标签顺序改变：原文 {STYLE_RE.findall(source)!r}；译文 {STYLE_RE.findall(target)!r}")

    if literal_spans == [(0, len(source))]:
        require_cjk = False
    # 先核对所有网址，再从中文要求中排除网址字符。含真实长正文的片段
    # 仍需中文；网址删改不能借此绕过门禁。既有 URL/DOI 检查仍保留。
    bare_source = [m.group() for m in _VALIDATION_URL_RE.finditer(source) if m.group().lower().startswith("www.")]
    bare_target = [m.group() for m in _VALIDATION_URL_RE.finditer(target) if m.group().lower().startswith("www.")]
    if _clean_identifiers(bare_source) != _clean_identifiers(bare_target):
        errors.append(f"url 网址标记不一致：原文 {_clean_identifiers(bare_source)!r}；译文 {_clean_identifiers(bare_target)!r}")
    prose = _VALIDATION_URL_RE.sub("", source)
    if require_cjk and len(ENGLISH_RE.findall(prose)) >= 40 and not CJK_RE.search(target):
        errors.append("长正文片段缺少中文译文")

    if domain is not None:
        target_folded = target.casefold()
        checked_terms = set()
        for _, entry in match_glossary(source, domain):
            if entry.required and entry.source not in checked_terms:
                checked_terms.add(entry.source)
                if entry.target.casefold() not in target_folded:
                    errors.append(
                        f"领域术语缺失：{entry.source!r} 必须译为包含 {entry.target!r}"
                    )
    return errors


def read_jsonl(path: Path) -> list[dict[str, object]]:
    """严格读取 JSONL；坏行必须阻止后续阶段。"""

    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是合法 JSONL：{exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number} 每行必须是 JSON 对象")
            rows.append(row)
    return rows


def write_jsonl_atomic(path: Path, rows: list[dict[str, object]]) -> None:
    """同目录临时写入后原子替换，避免中断留下半份译文。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def validate_translation_files(
    segments_path: Path,
    translations_path: Path,
    domain: DomainPack | None = None,
    *,
    reference_segment_ids: set[str] | frozenset[str] = frozenset(),
    reference_policy: str = "preserve",
    passthrough_segment_ids: set[str] | frozenset[str] = frozenset(),
) -> None:
    """核对两个 JSONL 的身份闭合和逐片段内容合同。"""

    if reference_policy not in {"preserve", "translate-titles"}:
        raise ValueError(f"参考文献策略非法：{reference_policy}")
    segments = read_jsonl(segments_path)
    translations = read_jsonl(translations_path)
    expected = {str(row.get("id")): row for row in segments}
    actual = {str(row.get("id")): row for row in translations}
    if len(expected) != len(segments) or len(actual) != len(translations):
        raise ValueError("片段或译文包含重复 ID")
    if set(expected) != set(actual):
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        raise ValueError(f"ID 集合不闭合：missing={missing}, unexpected={unexpected}")
    unknown_passthrough_ids = set(passthrough_segment_ids) - set(expected)
    if unknown_passthrough_ids:
        raise ValueError(f"透传映射包含未知片段 ID：{sorted(unknown_passthrough_ids)}")
    overlap = set(reference_segment_ids) & set(passthrough_segment_ids)
    if overlap:
        raise ValueError(f"参考文献与透传映射不能包含相同片段：{sorted(overlap)}")

    failures: dict[str, list[str]] = {}
    for sid, source_row in expected.items():
        source = str(source_row.get("source", ""))
        if sid != segment_id(source):
            raise ValueError(f"片段 ID 与规范化原文不一致：{sid}")
        target_row = actual[sid]
        if str(target_row.get("source", "")) != source:
            raise ValueError(f"译文记录的 source 与片段原文不一致：{sid}")
        target = str(target_row.get("target", ""))
        if sid in passthrough_segment_ids:
            errors = [] if target == source else ["人工透传片段必须与原文完全相同"]
        elif sid in reference_segment_ids and reference_policy == "preserve":
            errors = [] if target == source else ["preserve 策略要求参考文献原样保留"]
        elif sid in reference_segment_ids:
            errors = validate_translation(source, target, None)
        else:
            errors = validate_translation(source, target, domain)
        if errors:
            failures[sid] = errors
    if failures:
        raise ValueError(f"存在 {len(failures)} 个未通过门禁的片段：{failures!r}")
