"""有限科学单位语法：绑定数值和单位，规范表示而不推断物理换算。

前缀是单位身份的一部分，hPa不等于Pa，摄氏度也不等于K。仅支持这里明确
登记的SI/大气科学单位及别名；不把任意英语单词猜成单位。乘除和整数幂
归一为因子指数，数值精度不改变，原文坐标供Provider的原位保护使用。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re

# core SI + 常用时间/角度单位；中文别名仅改变表示，不改变前缀或倍率。
_ALIASES: dict[str, tuple[str, int]] = {}
for symbol, aliases in {
    'm': ['米', 'metre', 'metres', 'meter', 'meters'],
    'km': ['千米', '公里', 'kilometre', 'kilometres', 'kilometer', 'kilometers'],
    'cm': ['厘米'], 'mm': ['毫米'], 'kg': ['千克', '公斤'], 'g': ['克'],
    's': ['秒', 'second', 'seconds'], 'min': ['分钟'], 'h': ['小时', 'hour', 'hours'],
    'd': ['天', '日', 'day', 'days'], 'yr': ['年', 'year', 'years'],
    'Pa': ['帕', '帕斯卡'], 'hPa': ['百帕'], 'kPa': ['千帕'],
    'K': ['开尔文'], '°C': ['摄氏度', '℃'], '°F': ['华氏度', '℉'],
    '°': ['度'], '%': ['百分比', 'percent'], 'W': ['瓦', '瓦特'], 'MW': ['兆瓦'],
    'J': ['焦耳'], 'mol': ['摩尔'], 'µmol': ['μmol', '微摩尔'],
    'ppm': ['百万分之一'], 'ppb': ['十亿分之一'],
    'gC': ['g C'], 'PgC': ['Pg C'], 'µmolCO2': ['µmol CO2', 'μmol CO2'],
}.items():
    for alias in [symbol, *aliases]:
        _ALIASES[alias] = (symbol, 1)
for alias, base, power in [('平方米', 'm', 2), ('平方千米', 'km', 2),
                           ('平方公里', 'km', 2), ('立方米', 'm', 3)]:
    _ALIASES[alias] = (base, power)

_ATOM = re.compile('|'.join(re.escape(k).replace(r'\ ', r'\s*')
                           for k in sorted(_ALIASES, key=len, reverse=True)))
_SUPERS = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺', '0123456789-+')
_EXP = re.compile(r'(?:\s*\^\s*[+−-]?\d+|\s*[−-]\s*\d+|[⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+|\d+)')
_POWER = r'10(?:\^[+−-]?\d+|−\d+|[⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)'
_NUM = rf'(?:{_POWER}|[+−-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+−-]?\d+)?(?:\s*[×x]\s*{_POWER})?)'
_VALUES = re.compile(rf'(?<![A-Za-z0-9_./])(?P<a>{_NUM})(?:\s*[–—~-]\s*(?P<b>{_NUM}))?')
_NON_CJK_LETTER = re.compile(r'[^\W\d_\u3400-\u9fff]')
_BARE = {'m', 'km', 'cm', 'mm', 'kg', 'Pa', 'hPa', 'kPa', 'K', '°C', '°F', '°', '%', 'ppm', 'ppb'}


def _unit_at(text: str, start: int):
    match = _ATOM.match(text, start)
    if not match:
        return None
    raw = match.group()
    alias = next((key for key in _ALIASES if re.sub(r'\s+', '', key) == re.sub(r'\s+', '', raw)), raw)
    base, power = _ALIASES[alias]
    end = match.end()
    exponent = _EXP.match(text, end)
    if exponent:
        power *= int(re.sub(r'[\s^]', '', exponent.group()).translate(_SUPERS).replace('−', '-'))
        end = exponent.end()
    if end < len(text) and _NON_CJK_LETTER.match(text[end]):
        return None
    return end, base, power


def _unit_expression(text: str, start: int):
    first = _unit_at(text, start)
    if first is None:
        return None
    end, base, power = first
    factors = Counter({base: power})
    count = 1
    while end < len(text):
        separator = re.match(r'(?:\s*(/|每|per\b|·|⋅)\s*|\s+)', text[end:])
        if not separator:
            break
        next_start = end + separator.end()
        following = _unit_at(text, next_start)
        if following is None:
            break
        next_end, name, exponent = following
        divide = separator.group(1) in ('/', '每', 'per')
        factors[name] += -exponent if divide else exponent
        end = next_end
        count += 1
    signature = tuple(sorted((name, exponent) for name, exponent in factors.items() if exponent))
    return end, signature, count


def unit_label(signature: tuple[tuple[str, int], ...]) -> str:
    return ' '.join(name if exponent == 1 else f'{name}^{exponent}' for name, exponent in signature)


def _value(raw: str) -> str:
    raw = re.sub(r'\s+', '', raw).replace('−', '-').replace('×', 'x')
    # 上标仅规范为显式幂；不把1.0折成1，不进行温度/前缀/科学计数换算。
    raw = re.sub(r'([⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)', lambda m: '^' + m.group().translate(_SUPERS), raw)
    return raw


@dataclass(frozen=True)
class Quantity:
    start: int
    end: int
    values: tuple[str, ...]
    unit: tuple[tuple[str, int], ...]

    @property
    def signature(self) -> str:
        return '–'.join(self.values) + ' ' + unit_label(self.unit)


def find_quantities(text: str, excluded: list[tuple[int, int]] = ()) -> list[Quantity]:
    """提取明确数值+单位（含范围和850-hPa写法）；不在URL/公式标记内猜测。"""
    result = []
    occupied_end = -1
    for match in _VALUES.finditer(text):
        if match.start() < occupied_end:
            continue
        separator = re.match(r'\s*(?:[-‑]\s*)?', text[match.end():])
        unit_start = match.end() + separator.end()
        parsed = _unit_expression(text, unit_start)
        if parsed is None:
            continue
        end, signature, _count = parsed
        # 英文1960s表示年代，不是1960秒；SI量值写成1960 s时仍可识别。
        if re.fullmatch(r"(?:18|19|20)\d{2}s", text[match.start():end]):
            continue
        if any(match.start() < b and end > a for a, b in excluded):
            continue
        values = tuple(_value(x) for x in (match.group('a'), match.group('b')) if x is not None)
        result.append(Quantity(match.start(), end, values, signature))
        occupied_end = end
    return result


def standalone_units(text: str) -> list[tuple[int, int, str]]:
    """识别剩余单位标记；不把day等普通单词或单字母s/d/W当作裸单位。

    有乘除/幂的明确单位表达式可独立出现；中文别名与既有裸单位使用同一身份。
    数值所在范围应由调用方先遮去，避免同时使用独立计数与配对校验。
    """
    result = []
    end_seen = -1
    for match in _ATOM.finditer(text):
        if match.start() < end_seen or (match.start() and _NON_CJK_LETTER.match(text[match.start() - 1])):
            continue
        parsed = _unit_expression(text, match.start())
        if parsed is None:
            continue
        end, signature, count = parsed
        if count > 1 or any(power != 1 for _, power in signature) or (signature and signature[0][0] in _BARE):
            result.append((match.start(), end, unit_label(signature)))
            end_seen = end
    return result


def hide_spans(text: str, spans) -> str:
    """保留字符串坐标，避免移除物理量后两侧单词粘连造成新标记。"""
    chars = list(text)
    for start, end in spans:
        chars[start:end] = ' ' * (end - start)
    return ''.join(chars)
