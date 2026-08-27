"""命令行首用与断点身份测试不调用模型或真实版面引擎。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paperlocale import __version__, cli
from paperlocale.cli import _initialize_or_load_run, _provider_from_args, build_parser


class CliTest(unittest.TestCase):
    def test_package_version_matches_v042_release_line(self) -> None:
        self.assertEqual(__version__, "0.4.2")

    def test_cli_reports_package_version(self) -> None:
        """发布包必须能直接报告可核对的版本。"""

        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)

    def test_run_parser_accepts_single_command_workflow_options(self) -> None:
        """一键命令必须同时接收源 PDF、运行目录和明确 Provider。"""

        args = build_parser().parse_args(
            [
                "run",
                "paper.pdf",
                "--run-dir",
                "runs/paper",
                "--provider",
                "codex-local",
                "--model",
                "gpt-5.6-sol",
                "--reasoning-effort",
                "high",
                "--domain",
                "atmospheric-science",
                "--unattended",
            ]
        )
        self.assertEqual(args.command, "run")
        self.assertEqual(args.provider, "codex-local")
        self.assertEqual(args.domain, "atmospheric-science")
        self.assertEqual(args.reasoning_effort, "high")
        self.assertEqual(args.reference_policy, "preserve")
        self.assertTrue(args.unattended)

    def test_confirm_references_accepts_repeated_manual_ids(self) -> None:
        """人工复核命令应把多个乱序参考文献片段明确传给工作流。"""

        args = build_parser().parse_args(
            [
                "confirm-references",
                "--run-dir",
                "run",
                "--segment-id",
                "first",
                "--segment-id",
                "second",
                "--exclude-segment-id",
                "false-positive",
                "--confirmed-by",
                "reviewer",
            ]
        )
        self.assertEqual(args.segment_id, ["first", "second"])
        self.assertEqual(args.exclude_segment_id, ["false-positive"])
        self.assertEqual(args.confirmed_by, "reviewer")

    def test_confirm_passthrough_requires_reason_and_reviewer(self) -> None:
        """透传命令必须留下片段、原因和确认人三类审计信息。"""

        args = build_parser().parse_args(
            [
                "confirm-passthrough",
                "--run-dir",
                "run",
                "--segment-id",
                "formula-id",
                "--reason",
                "pure formula",
                "--confirmed-by",
                "reviewer",
            ]
        )
        self.assertEqual(args.segment_id, ["formula-id"])
        self.assertEqual(args.reason, "pure formula")
        self.assertEqual(args.confirmed_by, "reviewer")

    def test_chatgpt_web_commands_require_explicit_model_label_on_import(self) -> None:
        """网页桥接使用独立导出/导入命令，不能冒充自动 API Provider。"""

        exported = build_parser().parse_args(
            ["chatgpt-web-export", "--run-dir", "run"]
        )
        self.assertEqual(exported.command, "chatgpt-web-export")
        self.assertEqual(exported.max_segments, 20)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["chatgpt-web-import", "--run-dir", "run"]
            )

    def test_apply_text_repair_requires_explicit_geometry_and_font(self) -> None:
        """文字覆盖不得猜测页码、矩形、字体或字号。"""

        args = build_parser().parse_args(
            [
                "apply-text-repair",
                "--run-dir",
                "run",
                "--page",
                "27",
                "--rect",
                "40",
                "120",
                "500",
                "160",
                "--replacement",
                "图5 农业干旱评估",
                "--font-file",
                "NotoSansCJKsc-Regular.otf",
                "--font-size",
                "9.5",
                "--single-line",
                "--description",
                "修复跨对象碎裂图注",
            ]
        )
        self.assertEqual(args.command, "apply-text-repair")
        self.assertEqual(args.page, 27)
        self.assertEqual(args.rect, [40.0, 120.0, 500.0, 160.0])
        self.assertEqual(args.font_size, 9.5)
        self.assertTrue(args.single_line)

    def test_restore_source_vectors_requires_run_and_description(self) -> None:
        """源矢量重放必须通过显式、可审计的 PaperLocale 命令执行。"""

        args = build_parser().parse_args(
            [
                "restore-source-vectors",
                "--run-dir",
                "run",
                "--description",
                "restore machine-QA missing source vectors",
            ]
        )
        self.assertEqual(args.command, "restore-source-vectors")
        self.assertEqual(args.run_dir, Path("run"))

    def test_rollback_last_repair_passes_reason_and_reports_result(self) -> None:
        """CLI 必须把显式回滚原因传给工作流，并打印恢复后的 PDF 路径。"""

        restored = Path("/tmp/restored.pdf")
        with (
            patch(
                "sys.argv",
                [
                    "paperlocale",
                    "rollback-last-repair",
                    "--run-dir",
                    "run",
                    "--reason",
                    "remove audited repair",
                ],
            ),
            patch(
                "paperlocale.cli.rollback_last_repair",
                return_value=restored,
            ) as rollback,
            patch("builtins.print") as output,
        ):
            self.assertEqual(cli.main(), 0)

        rollback.assert_called_once_with(
            Path("run"),
            reason="remove audited repair",
        )
        output.assert_called_once_with(
            f"最后一次修复已回滚并记录：{restored}；请重新执行 qa"
        )

    def test_codex_provider_requires_explicit_model_for_auditing(self) -> None:
        """忽略用户配置后不能把未知默认模型写成可审计运行。"""

        args = build_parser().parse_args(
            ["translate", "--run-dir", "run", "--provider", "codex-local"]
        )
        with self.assertRaisesRegex(ValueError, "显式提供 --model"):
            _provider_from_args(args)

    def test_run_parser_accepts_qwen_mt_provider(self) -> None:
        """Qwen-MT 使用独立 Provider，不能冒充通用聊天模型接口。"""

        args = build_parser().parse_args(
            [
                "run",
                "paper.pdf",
                "--run-dir",
                "runs/paper",
                "--provider",
                "qwen-mt",
                "--base-url",
                "https://example.test/compatible-mode/v1",
                "--model",
                "qwen-mt-plus",
            ]
        )
        self.assertEqual(args.provider, "qwen-mt")

    def test_existing_run_rejects_a_different_source_pdf(self) -> None:
        """复用运行目录时不能把新论文误接到旧片段、译文和 QA 证据上。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.pdf"
            second = root / "second.pdf"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            run_dir = root / "run"
            _initialize_or_load_run(
                source_pdf=first,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
                pages=None,
            )
            with self.assertRaisesRegex(ValueError, "另一份源 PDF"):
                _initialize_or_load_run(
                    source_pdf=second,
                    run_dir=run_dir,
                    source_language="en",
                    target_language="zh-CN",
                    pages=None,
                )

    def test_run_resumes_after_translation_without_provider(self) -> None:
        """译文已写入清单后，续跑不应再次要求模型登录态或 API Key。"""

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            # ``main`` 先用清单是否存在区分新运行；内容由下方补丁提供。
            (run_dir / "run_manifest.json").write_text("{}", encoding="utf-8")
            translated = {
                "status": "translated",
                "source_pdf": str(Path(directory) / "paper.pdf"),
            }
            qa_generated = {
                "status": "qa_generated",
                "qa_output_dir": str(run_dir / "qa"),
                "rendered_pdf": str(run_dir / "render_output" / "translated.pdf"),
            }
            with (
                patch(
                    "sys.argv",
                    [
                        "paperlocale",
                        "run",
                        str(Path(directory) / "paper.pdf"),
                        "--run-dir",
                        str(run_dir),
                    ],
                ),
                patch(
                    "paperlocale.cli._initialize_or_load_run",
                    return_value=translated,
                ),
                patch("paperlocale.cli.load_domain_pack"),
                patch(
                    "paperlocale.cli.run_to_qa",
                    return_value=qa_generated,
                ) as run,
                patch("paperlocale.cli._provider_from_args") as build_provider,
                patch("builtins.print"),
            ):
                self.assertEqual(cli.main(), 0)
            build_provider.assert_not_called()
            self.assertIsNone(run.call_args.kwargs["provider"])
            self.assertFalse(run.call_args.kwargs["unattended"])


if __name__ == "__main__":
    unittest.main()
