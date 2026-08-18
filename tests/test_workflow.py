"""PDF 工作流测试使用模拟版面命令，不需要真实 PDF 引擎。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paperlocale.contracts import segment_id, write_jsonl_atomic
from paperlocale.domains import load_domain_pack
from paperlocale.providers import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
)
from paperlocale.workflow import (
    _resolve_pdf2zh,
    accept_run,
    collect_run,
    initialize_run,
    load_manifest,
    qa_run,
    render_run,
    save_manifest,
    translate_run,
    validate_run,
)


class _Provider(TranslationProvider):
    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        return [Translation(segment.id, "土壤湿度为10 mm。") for segment in segments]


class WorkflowTest(unittest.TestCase):
    def _make_qa_ready_run(self, root: Path) -> tuple[Path, Path]:
        """建立一份哈希、路径和 QA 报告完全闭合的最小运行。"""

        source = root / "source.pdf"
        source.write_bytes(b"source")
        translated = root / "translated.pdf"
        translated.write_bytes(b"translated-before-qa")
        run_dir = root / "run"
        manifest = initialize_run(
            source_pdf=source,
            run_dir=run_dir,
            source_language="en",
            target_language="zh-CN",
        )
        translated_hash = hashlib.sha256(translated.read_bytes()).hexdigest()
        report_path = run_dir / "qa" / "qa_report.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_text(
            json.dumps(
                {
                    "source_pdf": str(source.resolve()),
                    "translated_pdf": str(translated.resolve()),
                    "source_sha256": manifest["source_sha256"],
                    "translated_sha256": translated_hash,
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )
        manifest["status"] = "qa_generated"
        manifest["rendered_pdf"] = str(translated)
        manifest["rendered_sha256"] = translated_hash
        manifest["qa_report"] = str(report_path)
        save_manifest(run_dir, manifest)
        return run_dir, translated

    def test_state_machine_reaches_rendered(self) -> None:
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"synthetic-pdf-placeholder")
            run_dir = root / "run"
            initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            self.assertEqual(load_manifest(run_dir)["status"], "initialized")

            def fake_collect(command: list[str], log_path: Path, timeout_seconds: int = 7200):
                manifest = load_manifest(run_dir)
                text = "Soil moisture was 10 mm."
                write_jsonl_atomic(
                    Path(str(manifest["segments_path"])),
                    [{"id": segment_id(text), "source": text}],
                )

            with patch("paperlocale.workflow._invoke", side_effect=fake_collect):
                collect_run(run_dir, "/fake/pdf2zh_next")
            self.assertEqual(load_manifest(run_dir)["status"], "collected")

            self.assertEqual(
                translate_run(run_dir=run_dir, provider=_Provider(), domain=domain),
                (0, 1),
            )
            validate_run(run_dir, domain)
            self.assertEqual(load_manifest(run_dir)["status"], "validated")

            def fake_render(command: list[str], log_path: Path, timeout_seconds: int = 7200):
                manifest = load_manifest(run_dir)
                output = Path(str(manifest["render_output_dir"]))
                output.mkdir(parents=True, exist_ok=True)
                (output / "translated.pdf").write_bytes(b"translated-pdf-placeholder")

            with patch("paperlocale.workflow._invoke", side_effect=fake_render):
                result = render_run(run_dir, "/fake/pdf2zh_next")
            self.assertEqual(result.name, "translated.pdf")
            rendered_manifest = load_manifest(run_dir)
            self.assertEqual(rendered_manifest["status"], "rendered")
            self.assertEqual(
                rendered_manifest["rendered_sha256"],
                hashlib.sha256(result.read_bytes()).hexdigest(),
            )

    def test_collect_cannot_run_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"placeholder")
            run_dir = root / "run"
            initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            manifest = load_manifest(run_dir)
            manifest["status"] = "collected"
            save_manifest(run_dir, manifest)
            with self.assertRaisesRegex(ValueError, "initialized"):
                collect_run(run_dir, "/fake/pdf2zh_next")

    def test_source_change_is_rejected_before_collect(self) -> None:
        """源文件身份发生变化时，旧运行不得继续污染片段和译文。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"original")
            run_dir = root / "run"
            initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "发生变化"):
                collect_run(run_dir, "/fake/pdf2zh_next")

    def test_layout_cli_is_found_next_to_current_interpreter(self) -> None:
        """未激活 venv 时仍应找到由同一解释器安装的版面命令。"""

        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            interpreter = bin_dir / "python"
            interpreter.touch()
            layout_cli = bin_dir / "pdf2zh_next"
            layout_cli.touch()
            with (
                patch("paperlocale.workflow.shutil.which", return_value=None),
                patch("paperlocale.workflow.sys.executable", str(interpreter)),
            ):
                self.assertEqual(_resolve_pdf2zh(None), str(layout_cli))

    def test_domain_language_mismatch_is_rejected(self) -> None:
        """运行语言必须和领域包声明一致，不能静默套用错误提示。"""

        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"placeholder")
            run_dir = root / "run"
            initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="fr",
                target_language="zh-CN",
            )
            manifest = load_manifest(run_dir)
            manifest["status"] = "collected"
            save_manifest(run_dir, manifest)
            with self.assertRaisesRegex(ValueError, "领域包语言"):
                translate_run(run_dir=run_dir, provider=_Provider(), domain=domain)

    def test_legacy_rendered_run_binds_hash_when_qa_is_regenerated(self) -> None:
        """v0.1.0 清单可以重新 QA，但不能跳过重新检查直接验收。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"source")
            translated = root / "translated.pdf"
            translated.write_bytes(b"translated")
            run_dir = root / "run"
            manifest = initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            manifest["schema_version"] = 1
            manifest["status"] = "rendered"
            manifest["rendered_pdf"] = str(translated)
            manifest.pop("rendered_sha256", None)
            save_manifest(run_dir, manifest)
            report = {
                "source_pdf": str(source.resolve()),
                "translated_pdf": str(translated.resolve()),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "translated_sha256": hashlib.sha256(translated.read_bytes()).hexdigest(),
                "errors": [],
            }
            with patch("paperlocale.workflow.inspect_pdf_pair", return_value=report):
                qa_run(run_dir)
            upgraded = load_manifest(run_dir)
            self.assertEqual(upgraded["schema_version"], 2)
            self.assertEqual(upgraded["status"], "qa_generated")
            self.assertEqual(upgraded["rendered_sha256"], report["translated_sha256"])

    def test_accept_rejects_rendered_pdf_changed_after_qa(self) -> None:
        """人工验收只能批准生成 QA 对照图时的确切候选 PDF。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir, translated = self._make_qa_ready_run(root)
            translated.write_bytes(b"translated-after-qa")
            with self.assertRaisesRegex(ValueError, "render 后发生变化"):
                accept_run(run_dir, reviewed_by="reviewer")

    def test_accept_records_review_for_unchanged_artifacts(self) -> None:
        """源文件、候选和报告一致时，人工验收应正常闭合。"""

        with tempfile.TemporaryDirectory() as directory:
            run_dir, _ = self._make_qa_ready_run(Path(directory))
            accept_run(run_dir, reviewed_by="reviewer")
            manifest = load_manifest(run_dir)
            report = json.loads(
                Path(str(manifest["qa_report"])).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "accepted")
            self.assertEqual(manifest["accepted_by"], "reviewer")
            self.assertTrue(report["visual_accepted"])
            self.assertEqual(report["visual_reviewed_by"], "reviewer")


if __name__ == "__main__":
    unittest.main()
