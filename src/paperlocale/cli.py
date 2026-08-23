"""PaperLocale 可验证学术 PDF 翻译命令行入口。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .chatgpt_web import (
    export_chatgpt_web_batches,
    import_chatgpt_web_responses,
)
from .contracts import validate_translation, validate_translation_files
from .domains import load_domain_pack
from .evaluation import evaluate_provider, write_evaluation_report
from .pipeline import translate_segment_file
from .providers import REASONING_EFFORTS, CodexLocalProvider, OpenAICompatibleProvider
from .references import REFERENCE_POLICIES
from .workflow import (
    accept_run,
    apply_text_repair,
    apply_vector_repair,
    collect_run,
    confirm_passthrough_run,
    confirm_reference_run,
    initialize_run,
    load_manifest,
    prepare_reference_review_run,
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

    provider_eval = subparsers.add_parser(
        "provider-eval",
        help="用领域案例生成 Provider 合同与人工语义复核报告",
    )
    provider_eval.add_argument("--domain", default="atmospheric-science")
    provider_eval.add_argument(
        "--provider",
        choices=("codex-local", "openai-compatible"),
        required=True,
    )
    provider_eval.add_argument("--model")
    provider_eval.add_argument("--reasoning-effort", choices=REASONING_EFFORTS)
    provider_eval.add_argument("--codex-bin")
    provider_eval.add_argument("--base-url")
    provider_eval.add_argument("--api-key-env", default="PAPERLOCALE_API_KEY")
    provider_eval.add_argument("--output", type=Path, required=True)

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
    translate.add_argument("--reasoning-effort", choices=REASONING_EFFORTS)
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
    run.add_argument(
        "--reference-policy",
        choices=REFERENCE_POLICIES,
        default="preserve",
    )
    run.add_argument("--provider", choices=("codex-local", "openai-compatible"))
    run.add_argument("--model")
    run.add_argument("--reasoning-effort", choices=REASONING_EFFORTS)
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

    reference_review = subparsers.add_parser(
        "reference-review",
        help="生成参考文献片段人工复核清单",
    )
    reference_review.add_argument("--run-dir", type=Path, required=True)

    confirm_references = subparsers.add_parser(
        "confirm-references",
        help="确认自动匹配结果并补充参考文献片段 ID",
    )
    confirm_references.add_argument("--run-dir", type=Path, required=True)
    confirm_references.add_argument(
        "--segment-id",
        action="append",
        default=[],
        help="补充一个未自动匹配的参考文献片段 ID；可重复使用",
    )
    confirm_references.add_argument("--confirmed-by", required=True)

    confirm_passthrough = subparsers.add_parser(
        "confirm-passthrough",
        help="人工确认无需翻译且必须原样保留的片段 ID",
    )
    confirm_passthrough.add_argument("--run-dir", type=Path, required=True)
    confirm_passthrough.add_argument(
        "--segment-id",
        action="append",
        required=True,
        help="确认一个无需翻译的片段 ID；同一原因可重复使用",
    )
    confirm_passthrough.add_argument("--reason", required=True)
    confirm_passthrough.add_argument("--confirmed-by", required=True)

    chatgpt_export = subparsers.add_parser(
        "chatgpt-web-export",
        help="导出供 ChatGPT 网页端普通 Chat 人工翻译的哈希绑定批次",
    )
    chatgpt_export.add_argument("--run-dir", type=Path, required=True)
    chatgpt_export.add_argument("--domain", default="atmospheric-science")
    chatgpt_export.add_argument(
        "--reference-policy",
        choices=REFERENCE_POLICIES,
        default="preserve",
    )
    chatgpt_export.add_argument("--max-segments", type=int, default=20)
    chatgpt_export.add_argument("--max-characters", type=int, default=12000)

    chatgpt_import = subparsers.add_parser(
        "chatgpt-web-import",
        help="导入并验证从 ChatGPT 网页端普通 Chat 保存的 JSON 回复",
    )
    chatgpt_import.add_argument("--run-dir", type=Path, required=True)
    chatgpt_import.add_argument("--domain", default="atmospheric-science")
    chatgpt_import.add_argument(
        "--reference-policy",
        choices=REFERENCE_POLICIES,
        default="preserve",
    )
    chatgpt_import.add_argument(
        "--model-label",
        required=True,
        help="按网页模型选择器原样记录的人工可见模型标签",
    )

    run_translate = subparsers.add_parser("translate", help="翻译一个已初始化运行")
    run_translate.add_argument("--run-dir", type=Path, required=True)
    run_translate.add_argument("--domain", default="atmospheric-science")
    run_translate.add_argument(
        "--reference-policy",
        choices=REFERENCE_POLICIES,
        default="preserve",
    )
    run_translate.add_argument(
        "--provider",
        choices=("codex-local", "openai-compatible"),
        required=True,
    )
    run_translate.add_argument("--model")
    run_translate.add_argument("--reasoning-effort", choices=REASONING_EFFORTS)
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

    repair = subparsers.add_parser(
        "apply-vector-repair",
        help="导入只增加矢量绘图的候选 PDF，并记录修复历史",
    )
    repair.add_argument("--run-dir", type=Path, required=True)
    repair.add_argument("--repaired-pdf", type=Path, required=True)
    repair.add_argument("--description", required=True)

    text_repair = subparsers.add_parser(
        "apply-text-repair",
        help="在明确页面矩形内替换或删除文字，并记录修复历史",
    )
    text_repair.add_argument("--run-dir", type=Path, required=True)
    text_repair.add_argument(
        "--page",
        type=int,
        required=True,
        help="从 1 开始的页码",
    )
    text_repair.add_argument(
        "--rect",
        type=float,
        nargs=4,
        required=True,
        metavar=("X0", "Y0", "X1", "Y1"),
        help="PyMuPDF PDF 点坐标：左、上、右、下",
    )
    text_repair.add_argument(
        "--replacement",
        required=True,
        help="替换文字；显式传入空字符串时只删除 rect 内文字",
    )
    text_repair.add_argument(
        "--font-file",
        type=Path,
        help="非空 replacement 必需；只删除模式不使用",
    )
    text_repair.add_argument(
        "--font-size",
        type=float,
        help="非空 replacement 必需；只删除模式不使用",
    )
    text_repair.add_argument("--description", required=True)

    accept = subparsers.add_parser("accept", help="记录人工逐页视觉验收")
    accept.add_argument("--run-dir", type=Path, required=True)
    accept.add_argument("--reviewed-by", required=True)
    return parser


def _provider_from_args(args: argparse.Namespace):
    """根据明确命令行选择构造唯一 Provider，不做自动回退。"""

    if args.provider == "codex-local":
        if not args.model:
            raise ValueError("codex-local 必须显式提供 --model，才能审计实际模型")
        return CodexLocalProvider(
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            codex_bin=args.codex_bin,
        )
    if args.reasoning_effort:
        raise ValueError("--reasoning-effort 当前只适用于 codex-local")
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
    if args.command == "provider-eval":
        pack = load_domain_pack(args.domain)
        report = evaluate_provider(
            provider=_provider_from_args(args),
            provider_name=args.provider,
            model=args.model,
            domain=pack,
        )
        output = write_evaluation_report(args.output, report)
        print(
            f"Provider 评估完成：合同通过 {report['contract_passed_count']}/"
            f"{report['case_count']}，参考译文完全匹配 "
            f"{report['exact_reference_match_count']}/{report['case_count']}"
        )
        print(f"报告：{output}；语义准确性仍需逐条人工复核")
        return 1 if report["contract_failed_count"] else 0
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
            reference_policy=args.reference_policy,
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
    if args.command == "reference-review":
        summary = prepare_reference_review_run(args.run_dir)
        print(
            f"参考文献复核清单：{summary['review_jsonl']}；"
            f"确定性自动匹配 {len(summary['automatic_reference_segment_ids'])} 个片段"
        )
        return 0
    if args.command == "confirm-references":
        mapping = confirm_reference_run(
            args.run_dir,
            additional_segment_ids=args.segment_id,
            confirmed_by=args.confirmed_by,
        )
        print(
            "参考文献映射已确认："
            f"{len(mapping['reference_segment_ids'])} 个片段"
        )
        return 0
    if args.command == "confirm-passthrough":
        mapping = confirm_passthrough_run(
            args.run_dir,
            segment_ids=args.segment_id,
            reason=args.reason,
            confirmed_by=args.confirmed_by,
        )
        print(f"人工透传映射已确认：{len(mapping['entries'])} 个片段")
        return 0
    if args.command == "chatgpt-web-export":
        batch_manifest = export_chatgpt_web_batches(
            run_dir=args.run_dir,
            domain=load_domain_pack(args.domain),
            max_segments=args.max_segments,
            max_characters=args.max_characters,
            reference_policy=args.reference_policy,
        )
        print(
            f"ChatGPT Web 批次已导出：{batch_manifest['batch_count']} 批，"
            f"{batch_manifest['segment_count']} 个片段；请逐个保存网页 JSON 回复"
        )
        return 0
    if args.command == "chatgpt-web-import":
        reused, translated = import_chatgpt_web_responses(
            run_dir=args.run_dir,
            domain=load_domain_pack(args.domain),
            model_label=args.model_label,
            reference_policy=args.reference_policy,
        )
        print(f"ChatGPT Web 回复导入完成：复用 {reused}，新译 {translated}")
        return 0
    if args.command == "translate":
        pack = load_domain_pack(args.domain)
        reused, translated = translate_run(
            run_dir=args.run_dir,
            provider=_provider_from_args(args),
            domain=pack,
            max_segments=args.max_segments,
            max_characters=args.max_characters,
            reference_policy=args.reference_policy,
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
    if args.command == "apply-vector-repair":
        repaired = apply_vector_repair(
            args.run_dir,
            repaired_pdf=args.repaired_pdf,
            description=args.description,
        )
        print(f"矢量修复已导入并记录历史：{repaired}；请重新执行 qa 和 accept")
        return 0
    if args.command == "apply-text-repair":
        repaired = apply_text_repair(
            args.run_dir,
            page_number=args.page,
            rectangle=tuple(args.rect),
            replacement=args.replacement,
            font_file=args.font_file,
            font_size=args.font_size,
            description=args.description,
        )
        print(
            f"文字修复已应用并记录历史：{repaired}；"
            "请重新执行 qa 和 accept"
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
