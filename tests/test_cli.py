"""命令行首用与断点身份测试不调用模型或真实版面引擎。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paperlocale import __version__, cli
from paperlocale.cli import _initialize_or_load_run, _provider_from_args, build_parser


class CliTest(unittest.TestCase):
    def test_package_version_matches_v030_release_line(self) -> None:
        self.assertEqual(__version__, "0.3.1")

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
            ]
        )
        self.assertEqual(args.command, "run")
        self.assertEqual(args.provider, "codex-local")
        self.assertEqual(args.domain, "atmospheric-science")
        self.assertEqual(args.reasoning_effort, "high")
        self.assertEqual(args.reference_policy, "preserve")

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
                "--confirmed-by",
                "reviewer",
            ]
        )
        self.assertEqual(args.segment_id, ["first", "second"])
        self.assertEqual(args.confirmed_by, "reviewer")

    def test_codex_provider_requires_explicit_model_for_auditing(self) -> None:
        """忽略用户配置后不能把未知默认模型写成可审计运行。"""

        args = build_parser().parse_args(
            ["translate", "--run-dir", "run", "--provider", "codex-local"]
        )
        with self.assertRaisesRegex(ValueError, "显式提供 --model"):
            _provider_from_args(args)

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


if __name__ == "__main__":
    unittest.main()
