"""面向使用者的错误证据：原文、PDF 页码和坐标与异常一起传播。

不从 traceback 局部变量猜原文；内容/排版调用点明确附上正在处理的对象。
外部服务和文件错误可能与某句无关，此时明确报告没有句级定位。
"""
from __future__ import annotations
from pathlib import Path
import json
import time
from statistics import median
from .recovery import _save, _message, error_details


class LocatedError(ValueError):
    def __init__(self, message, items, category):
        super().__init__(message)
        self.items = items
        self.category = category


def unit_location(unit: dict) -> dict:
    text = unit['source']
    for i, anchor in enumerate(unit.get('anchors', [])):
        text = text.replace('{v'+str(i)+'}', anchor['text'])
    regions = unit.get('frames') or [p for slot in unit['slots'] for p in slot]
    sizes = [p['size'] for p in regions]
    default_floor = median(sizes) * (.8 if unit.get('frames') else 1)
    return {'id': unit['id'], 'source': text, 'pages': sorted({p['page'] for p in regions}),
            'regions': [{'page': p['page'], 'rect': p['rect']} for p in regions],
            'font_size': unit.get('_repair_font_floor', default_floor)}


def record_error(error: Exception, root: Path) -> dict:
    """诊断固定绑定源文件及本次错误，防止 App 用上一次弹窗修改新断点。"""
    root = root.expanduser().resolve()
    items = getattr(error, 'items', [])
    category = getattr(error, 'category', 'operation')
    warnings = []
    def evidence_file(name, empty):
        path = root/name
        if not path.exists():
            return empty
        # 诊断本身要能报告损坏的断点；此处不修复数据、不授权跳过身份检查。
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, type(empty)):
                raise ValueError('诊断文件结构不符')
            return value
        except (OSError, ValueError) as failure:
            warnings.append(f'{name} 无法读取：{_message(failure)}')
            return empty
    manifest = evidence_file('run_manifest.json', {})
    locations = evidence_file('source_locations.json', [])
    resolved = []
    for item in items:
        matches = [loc for loc in locations if loc['id'] == item.get('id')]
        resolved.extend([{**item, **loc} for loc in matches] or [item])
    page_context = []
    if not resolved and manifest.get('source_pdf'):
        import re
        import pymupdf as fitz
        pages = sorted({int(n) for n in re.findall(r'第(\d+)页', _message(error)) if n})
        if pages:
            # 源文件损坏/被移动也可能正是首错，辅助取证不可掩盖该错误。
            try:
                with fitz.open(manifest['source_pdf']) as doc:
                    page_context = [{'page': n, 'source': doc[n-1].get_text()} for n in pages if 1 <= n <= len(doc)]
            except (OSError, ValueError, RuntimeError):
                pass
    actions = [{'key': 'r', 'label': '从断点重试一次（模型请求可能计费）'}]
    # 只有唯一定位到待译单元才能保留原文；错误期间不改版面计划或科学合同。
    if category in {'translation', 'layout'} and resolved and all(i.get('pages') for i in resolved):
        actions.append({'key': 't', 'label': '仅清除失败片段缓存并重新翻译（可能计费）'})
        if category == 'layout' and all(i.get('font_size', 0) > 6 for i in resolved):
            floors = sorted({round(float(i['font_size']), 2) for i in resolved})
            actions.append({'key': 'f', 'label': f'仅降低失败段落字号下限 10%（当前 {floors} pt，最低 6 pt）'})
        actions.append({'key': 's', 'label': '跳过这些片段，保留原文并记录未翻译项'})
    if category == 'extraction' and resolved and not manifest.get('translation_provider'):
        actions.append({'key': 's', 'label': '保留这些异常区域的原文，不猜测或改写字形'})
    if (root / 'recovery_undo.json').exists():
        actions.append({'key': 'b', 'label': '回退上一次修复及其后续尝试，恢复修复前断点'})
    actions.append({'key': 'q', 'label': '暂不处理，保存断点并退出'})
    location, solution = error_details(error)
    record = {'error_id': str(time.time_ns()), 'category': category, 'message': _message(error),
              'location': location, 'solution': solution, 'diagnostic_warnings': warnings, 'items': resolved, 'page_context': page_context, 'actions': actions,
              'source_pdf': manifest.get('source_pdf'), 'source_sha256': manifest.get('source_sha256')}
    _save(root / 'error_report.json', record)
    return record


def print_context(record: dict) -> None:
    import sys
    for warning in record.get('diagnostic_warnings', []):
        print(warning, file=sys.stderr)
    if not record['items']:
        print('原文定位：此错误没有可靠的句级定位，请查看上述文件/服务原因。', file=sys.stderr)
    for context in record.get('page_context', []):
        print(f"PDF 第 {context['page']} 页原文上下文（尚未定位到句）：\n{context['source']}", file=sys.stderr)
    for item in record['items']:
        pages = ', '.join(map(str, item.get('pages', []))) or '未知'
        print(f"PDF 页码：{pages}；片段：{item.get('id', '未分段')}\n原文：{item.get('source', '')}", file=sys.stderr)
        if item.get('regions'):
            print('原文坐标：'+json.dumps(item['regions'], ensure_ascii=False), file=sys.stderr)
