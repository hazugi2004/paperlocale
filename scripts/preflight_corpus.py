"""对真实 PDF 语料执行无模型版面预检；每篇保存结果，可从断点续跑。

输入为 PDF 目录，输出为独立检查目录，不修改原 PDF 或已有翻译运行。
检查包含真实正文删除和固定字符保留，不代表译文、最终像素或人工验收通过。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil

import pymupdf
import paperlocale
from paperlocale.domains import load_domain_pack
from paperlocale.preserved_workflow import run_preserved
from paperlocale.source_layout import digest, save_json
from paperlocale.workflow import initialize_run


def inspect(args):
    source, output, detection_cache, font, revision = args
    # 同名源文件内容改变、代码改变或 MuPDF 升级均使用新断点，不能将
    # 旧版通过记录冒充当前实现的验证。已有翻译目录从不参与写入。
    source_hash = digest(source)
    root = output / revision / (source.stem + '-' + source_hash[:12])
    result_path = root / 'preflight_result.json'
    if result_path.exists():
        return json.loads(result_path.read_text())
    if not (root / 'run_manifest.json').exists():
        initialize_run(source_pdf=source, run_dir=root, source_language='en', target_language='zh-CN')
    if detection_cache:
        cached = detection_cache / source.stem / 'layout_detection.json'
        if cached.exists() and not (root / 'layout_detection.json').exists():
            # detect_regions 本身核对缓存的源 SHA256 和检测器身份。
            shutil.copy2(cached, root / 'layout_detection.json')
    result = {'file': str(source), 'source_sha256': source_hash, 'implementation': revision}
    try:
        report = run_preserved(root, provider=None, domain=load_domain_pack('atmospheric-science'),
                               plan_path=None, font_file=font, paragraph=True, preflight_only=True)
        result.update(passed=True, report=report)
    except Exception as error:
        result.update(passed=False, error=str(error), category=getattr(error, 'category', 'operation'),
                      items=getattr(error, 'items', []))
    save_json(result_path, result)
    print(source.name, 'PASS' if result['passed'] else 'FAIL: '+result['error'][:200], flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_dir', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--detection-cache', type=Path)
    parser.add_argument('--font-file', type=Path)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--failed-from', type=Path, help='仅复检指定 summary.json 中失败的原文件')
    args = parser.parse_args()
    sources = sorted(args.source_dir.expanduser().resolve().glob('*.pdf'))
    if args.failed_from:
        failed = {Path(r['file']).resolve() for r in json.loads(args.failed_from.read_text())['results'] if not r['passed']}
        if failed - set(sources):
            parser.error('失败清单包含当前目录中没有的原文件')
        sources = [s for s in sources if s in failed]
    if not sources or args.workers < 1:
        parser.error('需要至少一个 PDF，且 workers 必须大于零')
    package = Path(paperlocale.__file__).parent
    identity = hashlib.sha256(pymupdf.VersionBind.encode())
    for path in sorted(package.rglob('*.py')):
        identity.update(str(path.relative_to(package)).encode())
        identity.update(path.read_bytes())
    identity.update(Path(__file__).read_bytes())
    revision = identity.hexdigest()[:16]
    output = args.output.expanduser().resolve()
    jobs = [(source, output, args.detection_cache, args.font_file, revision) for source in sources]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(inspect, jobs))
    report = {'implementation': revision, 'documents': len(results),
              'passed': sum(r['passed'] for r in results), 'results': results,
              'translation_checked': False, 'final_pixels_checked': False}
    save_json(output / revision / 'summary.json', report)
    print(f"预检完成：{report['passed']}/{len(results)}；报告 {output / revision / 'summary.json'}")
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
