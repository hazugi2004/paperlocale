"""按物理区块采用用户译文，并保留原运行的保版及验收约束。"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import time

from .contracts import read_jsonl, unresolved_statistical_denominator, validate_translation
from .domains import load_domain_pack
from .error_categories import LAYOUT_CAPACITY_GUIDANCE, rule_detail
from .font_geometry import open_source_pdf
from .pipeline import restored_content_errors
from .repair_choices import read_choices, snapshot_run
from .source_layout import digest
from .workflow import _verify_rendered_pdf, _verify_source_pdf, load_manifest, save_manifest


def occurrence_id(location: dict) -> str:
    """同一段的自动译文可共享，人工修订绑定版面上的一次出现。"""
    data = json.dumps([location['id'], location['regions']], sort_keys=True).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def revision_errors(source: str, target: str, domain, anchors: dict, confirmation: str | None) -> list[str]:
    """保存和续跑共用同一份内容检查，手工确认仅能对应一个明确的待核对分母。"""
    if any(char in '\r\n\t\v\f\x85\u2028\u2029' for char in target):
        return ['请将修订写为一个段落。页面会自动折行，请移除手工换行或制表符。']
    if confirmation is not None and confirmation != unresolved_statistical_denominator(source):
        return ['确认的统计分母与当前原文待核对项不一致。']
    errors = validate_translation(source, target, domain, confirmed_denominator=confirmation, anchor_text=anchors)
    errors += restored_content_errors(source, target, anchors)
    for marker, anchor in anchors.items():
        # 旧计划可能只将「(Fig. 3」做成固定字形。能从源句确认的紧邻
        # 子图字母也随引用一起保护，避免绕过前端锁定后静默改为另一组图。
        if re.search(r'\b(?:Fig(?:ure)?\.?|图)\s*\d+\s*$', anchor, re.IGNORECASE):
            tail = re.search(re.escape(marker) + r'([a-z](?:[-–—][a-z])?)(?=[)）])', source)
            if tail and not re.search(re.escape(marker + tail[1]) + r'(?=[)）])', target):
                errors.append(f'固定引用的子图编号须保留 {anchor}{tail[1]}。')
    return errors


def _legacy_protected_spans(source: str, target: str, anchors: dict, urls: set[str]) -> list[dict]:
    """按旧占位符提交原字串，但在编辑器中合并已证实为同一实体的内容。"""
    source_markers = list(re.finditer(r'\{v\d+\}', source))
    target_markers = list(re.finditer(r'\{v\d+\}', target))
    if [m[0] for m in source_markers] != [m[0] for m in target_markers]:
        return []
    spans = []
    covered = set()
    for index, marker in enumerate(source_markers):
        if index in covered:
            continue
        first = target_markers[index]
        label = anchors[marker[0]]
        if urls and re.match(r'^[（(]?https?://', label):
            # 必须与 PDF 中的点击目标逐字相同。只跨过源文和译文中的空白，
            # 不能把后来加在网址碎片之间的正文藏进锁定控件。
            joined = label
            for end in range(index, len(source_markers)):
                if end > index:
                    following = source_markers[end]
                    if (source[source_markers[end-1].end():following.start()].strip()
                            or target[target_markers[end-1].end():target_markers[end].start()].strip()):
                        break
                    joined += anchors[following[0]]
                if joined in urls or (joined[0] in '(（' and joined[-1] in ')）' and joined[1:-1] in urls):
                    if end > index:
                        spans.append({'markers': [m[0] for m in source_markers[index:end+1]],
                                      'encoded': target[first.start():target_markers[end].end()], 'display': joined})
                        covered.update(range(index + 1, end + 1))
                    break
        if re.fullmatch(r'[-‐‑−][0-9][a-zA-Z0-9]*', label):
            prefix = re.search(r'(?<![A-Za-z0-9_])[A-Z][A-Z0-9]+$', source[:marker.start()])
            target_prefix = re.search(r'(?<![A-Za-z0-9_])[A-Z][A-Z0-9]+$', target[:first.start()])
            if prefix and target_prefix and prefix[0] == target_prefix[0]:
                spans.append({'markers': [marker[0]], 'encoded': prefix[0] + first[0],
                              'display': prefix[0] + label})
    return spans


def manual_revision(root: Path, *, page: int, error_id: str | None,
                    expected_hash: str | None, edit: dict | None = None,
                    check_only: bool = False) -> dict:
    """列出当前页可修订区块，或检查/保存一处已选的完整译文区块。"""
    if page < 1 or bool(error_id) == bool(expected_hash):
        raise ValueError('修订须指定有效页码和当前诊断或候选版本。')
    root = root.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest.get('layout_mode') not in {'paragraph', 'preserved'}:
        raise ValueError('采用我的修订仅支持保版运行。')
    _verify_source_pdf(manifest)
    context = json.loads((root / 'repair_context.json').read_text(encoding='utf-8'))
    if (context['source_sha256'] != manifest['source_sha256'] or
            context['layout_plan_sha256'] != manifest.get('layout_plan_sha256') or
            digest(Path(manifest['layout_plan'])) != context['layout_plan_sha256']):
        raise ValueError('源文或版面计划已变化，请重新打开当前运行。')
    domain = load_domain_pack(context['domain'], recorded_sha256=context['domain_sha256'])
    if domain.content_sha256 != context['domain_sha256'] or domain.content_sha256 != manifest.get('domain_sha256'):
        raise ValueError('领域包已变化，请恢复当前断点记录的领域包。')

    revisions = manifest.get('manual_revisions', {})
    if edit is not None:
        if (not isinstance(edit, dict) or any(not isinstance(edit.get(key), str) for key in ('id', 'before', 'target')) or
                (edit.get('confirm_denominator') is not None and not isinstance(edit['confirm_denominator'], str))):
            raise ValueError('修订请求缺少区块、修改前内容或修订译文。')
        earlier = revisions.get(edit['id'])
        # 应用后如果调用中断，同版本同文本再次提交只返回已保存结果。
        if (not check_only and earlier and earlier['page'] == page and
                earlier['before'] == edit['before'] and earlier['target'] == edit['target'] and
                earlier.get('confirmed_denominator') == edit.get('confirm_denominator') and
                earlier.get('previous_rendered_sha256') == expected_hash and earlier.get('error_id') == error_id):
            return {'valid': True, 'errors': [], 'item': None}

    report_items = None
    layout_capacity = False
    if error_id is not None:
        report = json.loads((root / 'error_report.json').read_text(encoding='utf-8'))
        if (manifest['status'] != 'collected' or report['error_id'] != error_id or
                report['source_sha256'] != manifest['source_sha256'] or
                'e' not in {action['key'] for action in report['actions']}):
            raise ValueError('当前错误或断点已更新，请重新打开问题。')
        layout_capacity = report.get('classification', {}).get('code') == 'layout.capacity'
        report_items = {}
        for diagnostic in report['items']:
            if isinstance(diagnostic.get('target'), str) and diagnostic.get('regions'):
                report_items[occurrence_id(diagnostic)] = diagnostic['target']
    else:
        if manifest['status'] not in {'rendered', 'qa_generated', 'accepted'}:
            raise ValueError('候选已变化，请重新打开当前运行。')
        _verify_rendered_pdf(manifest, expected_sha256=expected_hash)

    locations = json.loads((root / 'source_locations.json').read_text(encoding='utf-8'))
    counts = Counter(occurrence_id(location) for location in locations)
    segments = {row['id']: row['source'] for row in read_jsonl(root / 'segments.jsonl')}
    translations = root / 'translations.jsonl'
    targets = {row['id']: row['target'] for row in read_jsonl(translations)} if translations.exists() else {}
    skipped = set(read_choices(root, manifest['source_sha256'])['skip'])
    items = []
    for location in locations:
        sid = location['id']
        if page not in location['pages'] or sid in skipped or sid not in segments:
            continue
        identifier = occurrence_id(location)
        if counts[identifier] != 1 or report_items is not None and identifier not in report_items:
            continue
        previous_target = report_items.get(identifier) if report_items is not None else targets.get(sid)
        target = revisions.get(identifier, {}).get('target', previous_target)
        if target is None:
            continue
        source = segments[sid]
        anchors = location.get('anchors', context['anchors'].get(sid, {}))
        for marker in re.findall(r'\{v\d+\}', source):
            # 老断点的映射以文本 ID 保存。若同 ID 的当前原句对不上，
            # 不把另一处图号的映射借给此位置，等待重新生成定位记录。
            if marker not in anchors:
                break
        else:
            expanded = re.sub(r'\{v\d+\}', lambda m: anchors[m[0]], source)
            if expanded != location['source']:
                continue
            confirmation = revisions.get(identifier, {}).get('confirmed_denominator')
            errors = revision_errors(source, target, domain, anchors, confirmation)
            items.append({**location, 'segment_id': sid, 'id': identifier, 'validation_source': source,
                          'target': target, 'anchors': anchors, 'rule_details': [rule_detail(e) for e in errors],
                          'unresolved_denominator': unresolved_statistical_denominator(source)})
    if edit is None:
        # 旧断点没有逻辑网址标记。仅在有旧式网址起始锚点时读取真实 PDF 链接。
        url_pages = {p for item in items for p in item['pages']
                     if any(re.match(r'^[（(]?https?://', value) for value in item['anchors'].values())}
        links_by_page = {}
        if url_pages:
            with open_source_pdf(Path(manifest['source_pdf'])) as document:
                for p in url_pages:
                    links_by_page[p] = {link['uri'] for link in document[p-1].get_links() if link.get('uri')}
        for item in items:
            urls = set().union(*(links_by_page.get(p, set()) for p in item['pages']))
            spans = _legacy_protected_spans(item['validation_source'], item['target'], item['anchors'], urls)
            if spans:
                item['protected_spans'] = spans
        return {'items': items}

    item = next((item for item in items if item['id'] == edit['id']), None)
    if item is None or item['target'] != edit['before']:
        raise ValueError('所选区块或译文已变化，请重新定位后再修订。')
    target = edit['target']
    confirmation = edit.get('confirm_denominator')
    errors = revision_errors(item['validation_source'], target, domain, item['anchors'], confirmation)
    # 实际内容问题优先。正文和确认状态都未改动时，按当前操作解释为何无法继续。
    if (not errors and (layout_capacity or expected_hash or item['id'] in revisions) and item['target'] == target and
            confirmation == revisions.get(item['id'], {}).get('confirmed_denominator')):
        if layout_capacity:
            errors.append('译文没有改动，当前段落仍排不下。' + LAYOUT_CAPACITY_GUIDANCE)
        else:
            errors.append('译文没有改动。请修改后再提交。')
    result = {'valid': not errors, 'errors': errors, 'item': {**item, 'target': target}}
    if errors or check_only:
        return result
    snapshot_run(root)
    if load_manifest(root) != manifest:
        raise ValueError('保存期间运行已变化，请重新定位所选区块。')
    revisions[item['id']] = {'segment_id': item['segment_id'], 'page': page, 'before': item['target'], 'target': target,
                             'confirmed_denominator': confirmation, 'error_id': error_id,
                             'previous_rendered_sha256': expected_hash}
    # 同一次原子写入同时登记人工正文和撤销旧候选的验收身份。旧 PDF
    # 和导出可追溯，只有新版经过机器检查和明确验收才可重新正式导出。
    manifest.update(manual_revisions=revisions, status='collected', rendered_pdf=None, rendered_sha256=None)
    for key in ('qa_report', 'preservation_report', 'accepted_by'):
        manifest.pop(key, None)
    save_manifest(root, manifest)
    with (root / 'repair_audit.jsonl').open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'action': 'manual_revision', 'time_ns': time.time_ns(), 'id': item['id'],
                                 **revisions[item['id']]}, ensure_ascii=False) + '\n')
    return result
