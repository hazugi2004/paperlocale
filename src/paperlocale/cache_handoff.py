"""显式跨模型续跑：只导入源段落及锚点均相同、通过当前门禁的译文。"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from .contracts import read_jsonl, validate_translation, write_jsonl_atomic
from .pipeline import restored_content_errors
from .source_layout import digest, save_json, units_from_plan
from .workflow import load_manifest, _verify_source_pdf


def import_cache(old_root: Path, root: Path, plan: dict, units: list, domain) -> None:
    """导入到新运行，保留旧 Provider 身份和拒收证据；绝不编辑旧运行。

    新版分类可增加待译章节，但每个复用段落的字符、坐标及源文必须仍一致。
    除源字符串 ID 外逐项比较锚点全文，避免相同占位符承载不同公式时误复用。
    输出 translations.jsonl 与 cache_handoff.json；恢复前核对完整导入身份。
    """
    old_root = old_root.expanduser().resolve()
    report_path = root / 'cache_handoff.json'
    if old_root == root.resolve():
        raise ValueError('缓存导入必须使用新的运行目录')
    if not report_path.exists() and (root / 'translations.jsonl').exists():
        raise ValueError('不能向已有译文的运行追加导入；请使用新目录')
    manifest = load_manifest(old_root)
    _verify_source_pdf(manifest)
    if manifest['source_sha256'] != plan['source_sha256'] or manifest.get('layout_mode') != 'paragraph':
        raise ValueError('导入仅支持同一源 PDF 的段落模式')
    if manifest.get('domain_sha256') != domain.content_sha256:
        raise ValueError('缓存领域包不一致')
    path = old_root / 'layout_plan.json'
    if digest(path) != manifest.get('layout_plan_sha256'):
        raise ValueError('旧版面计划哈希不一致')
    old_plan = json.loads(path.read_text())
    if old_plan.get('source_sha256') != plan['source_sha256']:
        raise ValueError('旧计划源 PDF 身份不一致')
    def characters(unit):
        parts = [p for slot in unit['slots'] for p in slot] + unit['anchors']
        return Counter(json.dumps([p['page'], c['text'], c['rect'], c['origin']], ensure_ascii=False)
                       for p in parts for c in p['chars'] if c['text'].strip())
    def content_digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()
    identity = {'source_sha256': plan['source_sha256'],
                'source_plan_sha256': manifest['layout_plan_sha256'],
                'plan_sha256': content_digest(plan),
                'units_sha256': content_digest([
                    {'id': u['id'], 'source': u['source'], 'characters': characters(u),
                     'anchors': [a['text'] for a in u['anchors']]} for u in units]),
                'domain_sha256': domain.content_sha256}
    if report_path.exists():
        report = json.loads(report_path.read_text())
        if report['from_run'] != str(old_root):
            raise ValueError('既有导入记录不属于指定运行')
        # 旧报告没有足够证据证明接受时的领域和计划，不能静默升级或恢复。
        if 'import_identity' not in report:
            raise ValueError('旧导入报告缺少身份哈希；请使用新目录重新导入')
        if report['import_identity'] != identity:
            raise ValueError('既有导入记录的源文件、计划或领域身份不一致')
        if report.get('source_translations_sha256') != digest(old_root/'translations.jsonl'):
            raise ValueError('旧译文哈希与导入报告不一致')
        if not (root / 'translations.jsonl').exists():
            write_jsonl_atomic(root/'translations.jsonl', report['accepted'])
        return
    # 提取修复可纠正别处公式的 ActualText 编号。按待复用段落核验字符
    # 身份，不因无关保护块变化丢掉全篇缓存；有变化的段落单独拒收。
    old_units = {u['id']: u for u in units_from_plan(old_plan, paragraph=True)}
    new_units = {u['id']: u for u in units}
    accepted, rejected, seen = [], [], set()
    for row in read_jsonl(old_root / 'translations.jsonl'):
        sid = row['id']
        if sid in seen:
            raise ValueError('旧译文有重复 ID')
        seen.add(sid)
        old, new = old_units.get(sid), new_units.get(sid)
        reason = []
        if not old or not new or row['source'] != old['source'] or row['source'] != new['source']:
            reason = ['源段落不在当前计划中']
        elif characters(old) != characters(new):
            reason = ['源段落字符或坐标已变化']
        elif [a['text'] for a in old['anchors']] != [a['text'] for a in new['anchors']]:
            reason = ['固定锚点内容不同']
        else:
            anchors = {'{v'+str(i)+'}': a['text'] for i,a in enumerate(new['anchors'])}
            reason = validate_translation(row['source'], row['target'], domain) + restored_content_errors(row['source'], row['target'], anchors)
        if reason:
            rejected.append({**row, 'errors': reason})
        else:
            # 旧混合来源缓存不能强行标为当前模型；缺乏逐条证据时保留原
            # 运行声明并明确身份未细分，后续新译文由流水线记录实际模型。
            accepted.append({**row, 'imported_from': str(old_root),
                             'source_run_provider': manifest.get('translation_provider'),
                             'provider_identity_verified': 'provider' in row})
    report = {'from_run': str(old_root), 'import_identity': identity,
              'source_translations_sha256': digest(old_root/'translations.jsonl'),
              'accepted_count': len(accepted), 'rejected': rejected}
    # 报告先写；若写译文失败，重试时可从报告中恢复，不重新调用模型。
    report['accepted'] = accepted
    report['source_provenance_files'] = [
        {'path': str(old_root/name), 'sha256': digest(old_root/name)}
        for name in ('cache_provenance.json', 'layout_refinements.json') if (old_root/name).is_file()]
    save_json(report_path, report)
    write_jsonl_atomic(root/'translations.jsonl', accepted)
