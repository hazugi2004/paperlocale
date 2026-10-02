"""修复台的人工译文校验：不调用模型，不把校验通过等同于语义/版面验收。"""
import json
from pathlib import Path
from .contracts import read_jsonl, validate_translation, write_jsonl_atomic
from .domains import load_domain_pack
from .pipeline import restored_content_errors
from .source_layout import digest


def check_edits(root: Path, error_id: str, edits: dict) -> list[dict]:
    """只接受当前诊断明确定位的片段，使用同一源文件、计划和领域合同。"""
    report = json.loads((root/'error_report.json').read_text())
    if report['error_id'] != error_id or 'e' not in {a['key'] for a in report['actions']}:
        raise ValueError('诊断已更新或不能编辑，请重新打开本次错误')
    if not isinstance(edits, dict) or not edits or any(not isinstance(k, str) or not isinstance(v, str) for k,v in edits.items()):
        raise ValueError('修订内容必须是片段编号到译文文本的非空映射')
    allowed = {i['id'] for i in report['items'] if i.get('id')}
    if set(edits) - allowed:
        raise ValueError('修订包含本次诊断以外的片段')
    manifest = json.loads((root/'run_manifest.json').read_text())
    context = json.loads((root/'repair_context.json').read_text())
    source_hash = digest(Path(manifest['source_pdf']))
    if source_hash != manifest['source_sha256'] or source_hash != context['source_sha256']:
        raise ValueError('源 PDF 身份变化，拒绝把修订写入旧断点')
    if manifest['status'] in {'rendered', 'qa_generated', 'accepted'}:
        raise ValueError('PDF 已生成；请先回到生成前断点，不能直接改动已验收缓存')
    if digest(Path(manifest['layout_plan'])) != context['layout_plan_sha256']:
        raise ValueError('版面计划已经变化，请重新分析后校验')
    domain = load_domain_pack(context['domain'])
    if domain.content_sha256 != context['domain_sha256']:
        raise ValueError('领域包已变化，不能用另一套规则放行修订')
    segments = {r['id']: r['source'] for r in read_jsonl(root/'segments.jsonl')}
    result = []
    for sid, target in edits.items():
        if sid not in segments:
            raise ValueError('片段已不属于当前翻译集合')
        source = segments[sid]
        errors = validate_translation(source, target, domain)
        errors += restored_content_errors(source, target, context['anchors'].get(sid, {}))
        result.append({'id': sid, 'source': source, 'target': target, 'errors': errors})
    return result


def save_edits(root: Path, checked: list[dict], error_id: str) -> None:
    """由已建快照的 apply_choice 调用；只替换校验通过的选择项，保留其他缓存。"""
    rows = read_jsonl(root/'translations.jsonl') if (root/'translations.jsonl').exists() else []
    values = {r['id']: r for r in rows}
    provider = json.loads((root/'run_manifest.json').read_text()).get('translation_provider')
    for row in checked:
        values[row['id']] = {k: row[k] for k in ('id', 'source', 'target')}
        values[row['id']].update(provider=provider, manual_revision={'error_id': error_id})
    write_jsonl_atomic(root/'translations.jsonl', list(values.values()))
    edited = {r['id'] for r in checked}
    refinements = root/'layout_refinements.json'
    if refinements.exists():
        from .recovery import _save
        _save(refinements, {k:v for k,v in json.loads(refinements.read_text()).items() if k not in edited})
    rejected = root/'rejected_translations.jsonl'
    if rejected.exists():
        write_jsonl_atomic(rejected, [r for r in read_jsonl(rejected) if r['id'] not in edited])
