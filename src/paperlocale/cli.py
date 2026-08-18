"""PaperLocale 可验证学术 PDF 翻译命令行入口。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .contracts import validate_translation, validate_translation_files
from .domains import load_domain_pack
from .pipeline import translate_segment_file
from .providers import CodexLocalProvider, OpenAICompatibleProvider
from .workflow import (
    accept_run,
    collect_run,
    initialize_run,
    load_manifest,
    qa_run,
    render_run,
    run_to_qa,
    translate_run,
    validate_run,
)


def _domain_check(identifier: str) -> None:
    """加载领域包并用其自带案例验证术语门禁。"""

    pack = load_domain_pack(identifier)
    failures: list[str] = []
    for index, case in enumerate(pack.eval_cases, 1):
        errors = validate_translation(case["source"], case["target"], pack)
        if errors:
            failures.append(f"case {index}: {errors}")
    if failures:
        raise ValueError("领域包回归失败：" + "；".join(failures))
    print(
        f"领域包通过：{pack.pack_id} {pack.version}，"
        f"术语 {len(pack.glossary)} 条，案例 {len(pack.eval_cases)} 条"
    )


def _initialize_or_load_run(
    *,
    source_pdf: Path,
    run_dir: Path,
    source_language: str,
    target_language: str,
    pages: str | None,
) -> dict[str, object]:
    """新建运行或核对既有运行仍绑定同一源 PDF，禁止误续跑其他论文。"""

    source = source_pdf.expanduser().resolve()
    root = run_dir.expanduser().resolve()
    if (root / "run_manifest.json").is_file():
        manifest = load_manifest(root)
        if Path(str(manifest["source_pdf"])).resolve() != source:
            raise ValueError("--run-dir 已绑定另一份源 PDF；请使用新的运行目录")
        return manifest
    return initialize_run(
        source_pdf=source,
        run_dir=root,
        source_language=source_language,
        target_language=target_language,
        pages=pages,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paperlocale", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    domain = subparsers.add_parser("domain-check", help="验证内置或外部领域包")
    domain.add_argument("domain")

    validate = subparsers.add_parser("validate-segments", help="验证片段与译文 JSONL")
    validate.add_argument("--segments", type=Path, required=True)
    validate.add_argument("--translations", type=Path, required=True)
    validate.add_argument("--domain")

    translate = subparsers.add_parser("translate-segments", help="调用一个明确 Provider 翻译片段")
    translate.add_argument("--segments", type=Path, required=True)
    translate.add_argument("--translations", type=Path, required=True)
    translate.add_argument("--domain", default="atmospheric-science")
    translate.add_argument(
        "--provider",
        choices=("codex-local", "openai-compatible"),
        required=True,
    )
    translate.add_argument("--model")
    translate.add_argument("--codex-bin")
    translate.add_argument("--base-url")
    translate.add_argument("--api-key-env", default="PAPERLOCALE_API_KEY")
    translate.add_argument("--max-segments", type=int, default=200)
    translate.add_argument("--max-characters", type=int, default=30000)

    initialize = subparsers.add_parser("init-run", help="创建绑定源 PDF 的运行目录")
    initialize.add_argument("source_pdf", type=Path)
    initialize.add_argument("--run-dir", type=Path, required=True)
    initialize.add_argument("--source-language", default="en")
    initialize.add_argument("--target-language", default="zh-CN")
    initialize.add_argument("--pages")

    run = subparsers.add_parser("run", help="从当前断点一键推进到机器 QA")
    run.add_argument("source_pdf", type=Path)
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--source-language", default="en")
    run.add_argument("--target-language", default="zh-CN")
    run.add_argument("--pages")
    run.add_argument("--domain", default="atmospheric-science")
    run.add_argument("--provider", choices=("codex-local", "openai-compatible"))
    run.add_argument("--model")
    run.add_argument("--codex-bin")
    run.add_argument("--base-url")
    run.add_argument("--api-key-env", default="PAPERLOCALE_API_KEY")
    run.add_argument("--max-segments", type=int, default=200)
    run.add_argument("--max-characters", type=int, default=30000)
    run.add_argument("--pdf2zh-bin")
    run.add_argument("--dpi", type=int, default=144)
    run.add_argument("--pdftoppm-bin")

    collect = subparsers.add_parser("collect", help="收集 PDF 待译片段")
    collect.add_argument("--run-dir", type=Path, required=True)
    collect.add_argument("--pdf2zh-bin")

    run_translate = subparsers.add_parser("translate", help="翻译一个已初始化运行")
    run_translate.add_argument("--run-dir", type=Path, required=True)
    run_translate.add_argument("--domain", default="atmospheric-science")
    run_translate.add_argument(
        "--provider",
        choices=("codex-local", "openai-compatible"),
        required=True,
    )
    run_translate.add_argument("--model")
    run_translate.add_argument("--codex-bin")
    run_translate.add_argument("--base-url")
    run_translate.add_argument("--api-key-env", default="PAPERLOCALE_API_KEY")
    run_translate.add_argument("--max-segments", type=int, default=200)
    run_translate.add_argument("--max-characters", type=int, default=30000)

    run_validate = subparsers.add_parser("validate", help="全量验证一个运行的译文")
    run_validate.add_argument("--run-dir", type=Path, required=True)
    run_validate.add_argument("--domain", default="atmospheric-science")

    render = subparsers.add_parser("render", help="用已验证译文重建 PDF")
    render.add_argument("--run-dir", type=Path, required=True)
    render.add_argument("--pdf2zh-bin")

    status = subparsers.add_parser("status", help="显示运行清单")
    status.add_argument("--run-dir", type=Path, required=True)

    qa = subparsers.add_parser("qa", help="执行 PDF 结构检查并生成逐页对照图")
    qa.add_argument("--run-dir", type=Path, required=True)
    qa.add_argument("--dpi", type=int, default=144)
    qa.add_argument("--pdftoppm-bin")

    accept = subparsers.add_parser("accept", help="记录人工逐页视觉验收")
    accept.add_argument("--run-dir", type=Path, required=True)
    accept.add_argument("--reviewed-by", required=True)
    return parser


def _provider_from_args(args: argparse.Namespace):
    """根据明确命令行选择构造唯一 Provider，不做自动回退。"""

    if args.provider == "codex-local":
        return CodexLocalProvider(model=args.model, codex_bin=args.codex_bin)
    if not args.base_url or not args.model:
        raise ValueError("openai-compatible 必须提供 --base-url 和 --model")
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise ValueError(f"环境变量 {args.api_key_env} 为空")
    return OpenAICompatibleProvider(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
    )


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "domain-check":
        _domain_check(args.domain)
        return 0
    if args.command == "translate-segments":
        pack = load_domain_pack(args.domain)
        provider = _provider_from_args(args)
        reused, translated = translate_segment_file(
            segments_path=args.segments.expanduser().resolve(),
            translations_path=args.translations.expanduser().resolve(),
            provider=provider,
            domain=pack,
            max_segments=args.max_segments,
            max_characters=args.max_characters,
        )
        print(f"片段翻译完成：复用 {reused}，新译 {translated}")
        return 0
    if args.command == "init-run":
        manifest = initialize_run(
            source_pdf=args.source_pdf,
            run_dir=args.run_dir,
            source_language=args.source_language,
            target_language=args.target_language,
            pages=args.pages,
        )
        print(f"运行已初始化：{manifest['source_sha256']}")
        return 0
    if args.command == "run":
        root = args.run_dir.expanduser().resolve()
        is_new = not (root / "run_manifest.json").is_file()
        if is_new and args.provider is None:
            raise ValueError("新运行必须提供 --provider")
        manifest = _initialize_or_load_run(
            source_pdf=args.source_pdf,
            run_dir=root,
            source_language=args.source_language,
            target_language=args.target_language,
            pages=args.pages,
        )
        needs_provider = manifest["status"] in {"initialized", "collected"}
        if needs_provider and args.provider is None:
            raise ValueError("运行尚未翻译；请提供 --provider 后重试")
        final_manifest = run_to_qa(
            run_dir=root,
            provider=_provider_from_args(args) if needs_provider else None,
            domain=load_domain_pack(args.domain),
            pdf2zh_bin=args.pdf2zh_bin,
            dpi=args.dpi,
            pdftoppm_bin=args.pdftoppm_bin,
        )
        if final_manifest["status"] == "qa_generated":
            comparisons = Path(str(final_manifest["qa_output_dir"])) / "comparisons"
            print(f"运行已推进到机器 QA；请逐页检查：{comparisons}")
            print("确认无误后执行 paperlocale accept，人工验收不会自动完成")
        else:
            print(f"运行当前状态：{final_manifest['status']}")
        return 0
    if args.command == "collect":
        collect_run(args.run_dir, args.pdf2zh_bin)
        print("PDF 片段收集完成")
        return 0
    if args.command == "translate":
        pack = load_domain_pack(args.domain)
        reused, translated = translate_run(
            run_dir=args.run_dir,
            provider=_provider_from_args(args),
            domain=pack,
            max_segments=args.max_segments,
            max_characters=args.max_characters,
        )
        print(f"运行翻译完成：复用 {reused}，新译 {translated}")
        return 0
    if args.command == "validate":
        validate_run(args.run_dir, load_domain_pack(args.domain))
        print("运行译文全量门禁通过")
        return 0
    if args.command == "render":
        print(f"PDF 已重建：{render_run(args.run_dir, args.pdf2zh_bin)}")
        return 0
    if args.command == "status":
        import json

        print(json.dumps(load_manifest(args.run_dir.resolve()), ensure_ascii=False, indent=2))
        return 0
    if args.command == "qa":
        report = qa_run(
            args.run_dir,
            dpi=args.dpi,
            pdftoppm_bin=args.pdftoppm_bin,
        )
        print(
            f"机器 QA 通过：{report['translated_pages']} 页；"
            "请检查 comparisons/ 后再执行 accept"
        )
        return 0
    if args.command == "accept":
        accept_run(args.run_dir, reviewed_by=args.reviewed_by)
        print("人工视觉验收已记录")
        return 0
    pack = load_domain_pack(args.domain) if args.domain else None
    validate_translation_files(
        args.segments.expanduser().resolve(),
        args.translations.expanduser().resolve(),
        pack,
    )
    print("片段与译文合同通过")
    return 0
