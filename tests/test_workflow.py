"""PDF 工作流测试使用模拟版面命令，不需要真实 PDF 引擎。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paperlocale.contracts import segment_id, write_jsonl_atomic
from paperlocale.domains import load_domain_pack
from paperlocale.providers import Segment, Translation, TranslationContext, TranslationProvider
from paperlocale.workflow import (
    collect_run,
    initialize_run,
    load_manifest,
    render_run,
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
            self.assertEqual(load_manifest(run_dir)["status"], "rendered")

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
            from paperlocale.workflow import save_manifest

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
            from paperlocale.workflow import save_manifest

            save_manifest(run_dir, manifest)
            with self.assertRaisesRegex(ValueError, "领域包语言"):
                translate_run(run_dir=run_dir, provider=_Provider(), domain=domain)


if __name__ == "__main__":
    unittest.main()
