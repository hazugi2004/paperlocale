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
from .error_categories import rule_detail
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
    if error_id is not None:
        report = json.loads((root / 'error_report.json').read_text(encoding='utf-8'))
        if (manifest['status'] != 'collected' or report['error_id'] != error_id or
                report['source_sha256'] != manifest['source_sha256'] or
                'e' not in {action['key'] for action in report['actions']}):
            raise ValueError('当前错误或断点已更新，请重新打开问题。')
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
        return {'items': items}

    item = next((item for item in items if item['id'] == edit['id']), None)
    if item is None or item['target'] != edit['before']:
        raise ValueError('所选区块或译文已变化，请重新定位后再修订。')
    target = edit['target']
    confirmation = edit.get('confirm_denominator')
    errors = revision_errors(item['validation_source'], target, domain, item['anchors'], confirmation)
    if ((expected_hash or item['id'] in revisions) and item['target'] == target and
            confirmation == revisions.get(item['id'], {}).get('confirmed_denominator')):
        errors.append('请修改译文或确认待核对的统计分母。')
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
