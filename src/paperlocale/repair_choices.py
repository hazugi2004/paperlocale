"""CLI/App 共用的可回退修复选择，独立于自动科学内容门禁。

输入必须是当前诊断的 error_id 和所列字母。备份只复制本运行内部产物，
不修改源 PDF。失败尝试可恢复整个修复前断点；尚未完成 QA 时不导出结果。
"""
from pathlib import Path
import json
import shutil
import time
from .recovery import _save
from .source_layout import digest


def read_choices(root: Path, source_hash: str) -> dict:
    path = root / 'repair_choices.json'
    choices = json.loads(path.read_text()) if path.exists() else {'source_sha256': source_hash, 'skip': [], 'skip_blocks': [], 'font_sizes': {}}
    if choices['source_sha256'] != source_hash:
        raise ValueError('修复选项不属于当前源 PDF')
    return choices


def apply_choice(root: Path, key: str, error_id: str) -> None:
    """一次选择只改变明确失败片段；重试仍走原流程，绝不把失败设为成功。"""
    root = root.expanduser().resolve()
    report = json.loads((root / 'error_report.json').read_text())
    if report['error_id'] != error_id or key not in {a['key'] for a in report['actions']}:
        raise ValueError('诊断已更新或选项不适用于当前错误，请重新打开诊断')
    if key == 'q':
        return
    if report.get('source_pdf') and digest(Path(report['source_pdf'])) != report['source_sha256']:
        raise ValueError('源 PDF 已变化，不能应用旧诊断修复')
    if key == 'b':
        record = json.loads((root / 'recovery_undo.json').read_text())
        backup = root / 'recovery_backups' / record['backup']
        if not backup.is_dir():
            raise ValueError('修复备份缺失，不能回退')
        expected = record['files']
        if {str(p.relative_to(backup)) for p in backup.rglob('*') if p.is_file()} != set(expected):
            raise ValueError('修复备份文件集合不完整，拒绝回退')
        if any(digest(backup/name) != sha for name, sha in expected.items()):
            raise ValueError('修复备份内容已变化，拒绝回退')
        # 先逐文件原子恢复备份，再删除本次尝试新增的产物。回退过程意外
        # 中断时备份与撤销指针仍存在，可再次执行同一步，不先清空断点。
        current = [p for p in root.rglob('*') if p.is_file() and
                   'recovery_backups' not in p.relative_to(root).parts and p.name != 'repair_audit.jsonl']
        for name in expected:
            if name == 'recovery_undo.json':
                continue
            target = root/name
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name+'.rollback.tmp')
            shutil.copyfile(backup/name, temporary)
            temporary.replace(target)
        for path in current:
            if str(path.relative_to(root)) not in expected and path.name != 'recovery_undo.json':
                path.unlink()
        if 'recovery_undo.json' in expected:
            _save(root/'recovery_undo.json', json.loads((backup/'recovery_undo.json').read_text()))
        else:
            (root/'recovery_undo.json').unlink()
    else:
        # 外部计划/外部导出不属于本操作可回退范围；已生成 PDF 的流程仅支持
        # 原有专用 PDF 修复命令，避免重新翻译破坏已验收文件。
        if not (root / 'run_manifest.json').exists():
            return
        manifest = json.loads((root / 'run_manifest.json').read_text())
        if manifest['status'] in {'rendered', 'qa_generated', 'accepted'}:
            if key != 'r':
                raise ValueError('已生成 PDF 的断点不能改变翻译选项')
            return
        stamp = str(time.time_ns())
        backup = root / 'recovery_backups' / stamp
        backup.mkdir(parents=True)
        for path in root.iterdir():
            if path.name in {'recovery_backups', 'repair_audit.jsonl'}:
                continue
            if path.is_symlink():
                raise ValueError('运行目录包含符号链接，不能建立可靠的修复快照')
            if path.is_dir():
                shutil.copytree(path, backup/path.name)
            else:
                shutil.copy2(path, backup/path.name)
        snapshot_files = {str(p.relative_to(backup)): digest(p) for p in backup.rglob('*') if p.is_file()}
        _save(root / 'recovery_undo.json', {'backup': stamp, 'files': snapshot_files})
        ids = {item['id'] for item in report['items'] if item.get('id')}
        choices = read_choices(root, report['source_sha256'])
        if key == 's':
            field = 'skip_blocks' if report['category'] == 'extraction' else 'skip'
            choices[field] = sorted(set(choices.get(field, [])) | ids)
        elif key == 'f':
            # 相同原句可能出现于多页并共用译文 ID，一次选择只缩小一次。
            # 多种原字号共用同一译文时取最小下限，避免超过较小原字号。
            for sid in ids:
                floors = [float(item['font_size']) for item in report['items'] if item['id'] == sid]
                size = choices['font_sizes'].get(sid, min(floors))
                choices['font_sizes'][sid] = max(6., round(size*.9, 2))
        elif key == 't':
            from .contracts import read_jsonl, write_jsonl_atomic
            path = root / 'translations.jsonl'
            if path.exists():
                write_jsonl_atomic(path, [r for r in read_jsonl(path) if r['id'] not in ids])
            path = root / 'layout_refinements.json'
            if path.exists():
                _save(path, {k:v for k,v in json.loads(path.read_text()).items() if k not in ids})
        if report['category'] == 'extraction' and key == 's':
            # 提取发生在模型调用前；若其他步骤已开始，不允许改变逻辑分段身份。
            if manifest.get('translation_provider'):
                raise ValueError('已有翻译的断点不能改变提取分组；请为新提取方案创建新运行')
        _save(root / 'repair_choices.json', choices)
    with (root / 'repair_audit.jsonl').open('a') as handle:
        handle.write(json.dumps({'error_id': error_id, 'action': key, 'time_ns': time.time_ns()}, ensure_ascii=False)+'\n')


def interactive_run(operation, root: Path):
    """终端字母菜单：修复失败重新展示真实错误，回退后不自动再调用模型。"""
    from .diagnostics import record_error, print_context
    from .recovery import print_error
    while True:
        try:
            return operation()
        except Exception as error:
            print_error(error, root)
            report = record_error(error, root)
            print_context(report)
        while True:
            for action in report['actions']:
                print(f"[{action['key']}] {action['label']}", flush=True)
            try:
                key = input('请选择字母：').strip().lower()
            except EOFError:
                return 1
            if key == 'q':
                return 1
            try:
                apply_choice(root, key, report['error_id'])
            except Exception as error:
                print_error(error, root)
                continue
            if key == 'b':
                print('已回退；断点保持修复前状态。再次运行即可继续。', flush=True)
                return 1
            break
