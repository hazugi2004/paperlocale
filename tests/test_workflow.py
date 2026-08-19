"""PDF 工作流测试使用模拟版面命令，不需要真实 PDF 引擎。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reportlab.pdfgen import canvas

from paperlocale.contracts import segment_id, write_jsonl_atomic
from paperlocale.domains import load_domain_pack
from paperlocale.providers import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
)
from paperlocale.workflow import (
    _layout_provenance,
    _resolve_pdf2zh,
    accept_run,
    apply_vector_repair,
    collect_run,
    confirm_passthrough_run,
    confirm_reference_run,
    initialize_run,
    load_manifest,
    qa_run,
    render_run,
    run_to_qa,
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


class _IdentityProvider(TranslationProvider):
    """模拟模型正确原样保留非正文，但因此触发全局中文门禁。"""

    def __init__(self) -> None:
        self.calls = 0

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        self.calls += 1
        return [Translation(segment.id, segment.source) for segment in segments]


class WorkflowTest(unittest.TestCase):
    layout_provenance = {
        "executable": "/fake/pdf2zh_next",
        "pdf2zh_next_version": "2.9.0",
        "babeldoc_version": "0.6.2",
    }

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
            document = canvas.Canvas(str(source))
            document.drawString(40, 780, "Synthetic PDF without references")
            document.save()
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

            with (
                patch("paperlocale.workflow._invoke", side_effect=fake_collect),
                patch(
                    "paperlocale.workflow._layout_provenance",
                    return_value=self.layout_provenance,
                ),
            ):
                collect_run(run_dir, "/fake/pdf2zh_next")
            collected_manifest = load_manifest(run_dir)
            self.assertEqual(collected_manifest["status"], "collected")
            self.assertEqual(collected_manifest["layout_engine"], self.layout_provenance)
            confirm_reference_run(
                run_dir,
                additional_segment_ids=[],
                confirmed_by="test reviewer",
            )

            self.assertEqual(
                translate_run(run_dir=run_dir, provider=_Provider(), domain=domain),
                (0, 1),
            )
            translated_manifest = load_manifest(run_dir)
            self.assertEqual(
                translated_manifest["domain_pack"]["content_sha256"],
                domain.content_sha256,
            )
            self.assertEqual(translated_manifest["reference_policy"], "preserve")
            self.assertEqual(translated_manifest["reference_segment_count"], 0)
            self.assertTrue(translated_manifest["reference_map_sha256"])
            self.assertTrue(
                translated_manifest["translation_provider"]["provider"].endswith(
                    "test_workflow._Provider"
                )
            )
            validate_run(run_dir, domain)
            self.assertEqual(load_manifest(run_dir)["status"], "validated")

            def fake_render(command: list[str], log_path: Path, timeout_seconds: int = 7200):
                manifest = load_manifest(run_dir)
                output = Path(str(manifest["render_output_dir"]))
                output.mkdir(parents=True, exist_ok=True)
                (output / "translated.pdf").write_bytes(b"translated-pdf-placeholder")

            with (
                patch("paperlocale.workflow._invoke", side_effect=fake_render),
                patch(
                    "paperlocale.workflow._layout_provenance",
                    return_value=self.layout_provenance,
                ),
            ):
                result = render_run(run_dir, "/fake/pdf2zh_next")
            self.assertEqual(result.name, "translated.pdf")
            rendered_manifest = load_manifest(run_dir)
            self.assertEqual(rendered_manifest["status"], "rendered")
            self.assertEqual(
                rendered_manifest["rendered_sha256"],
                hashlib.sha256(result.read_bytes()).hexdigest(),
            )

    def test_run_to_qa_advances_initialized_run_without_accepting(self) -> None:
        """一键运行复用全部阶段，但必须停在需要人工复核的 qa_generated。"""

        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            document = canvas.Canvas(str(source))
            # 真实 QA 会拒绝疑似空白页，因此夹具提供足够正文，而不是降低门禁。
            for row in range(24):
                document.drawString(
                    40,
                    780 - row * 24,
                    f"Synthetic paragraph {row + 1}: Soil moisture was 10 mm.",
                )
            document.save()
            run_dir = root / "run"
            initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )

            def fake_layout(
                command: list[str],
                log_path: Path,
                timeout_seconds: int = 7200,
            ) -> None:
                manifest = load_manifest(run_dir)
                bridge = command[command.index("--clitranslator-command") + 1]
                if " collect " in f" {bridge} ":
                    text = "Soil moisture was 10 mm."
                    write_jsonl_atomic(
                        Path(str(manifest["segments_path"])),
                        [{"id": segment_id(text), "source": text}],
                    )
                    return
                output = Path(str(manifest["render_output_dir"]))
                output.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output / "translated.pdf")

            with (
                patch("paperlocale.workflow._invoke", side_effect=fake_layout),
                patch(
                    "paperlocale.workflow._layout_provenance",
                    return_value=self.layout_provenance,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "参考文献映射尚未人工确认"):
                    run_to_qa(
                        run_dir=run_dir,
                        provider=_Provider(),
                        domain=domain,
                        pdf2zh_bin="/fake/pdf2zh_next",
                        dpi=72,
                    )
                confirm_reference_run(
                    run_dir,
                    additional_segment_ids=[],
                    confirmed_by="test reviewer",
                )
                manifest = run_to_qa(
                    run_dir=run_dir,
                    provider=_Provider(),
                    domain=domain,
                    pdf2zh_bin="/fake/pdf2zh_next",
                    dpi=72,
                )
            self.assertEqual(manifest["status"], "qa_generated")
            self.assertTrue(Path(str(manifest["qa_report"])).is_file())
            self.assertTrue(
                (Path(str(manifest["qa_output_dir"])) / "comparisons/page-001.png").is_file()
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

    def test_layout_version_probe_retries_one_cold_start_timeout(self) -> None:
        """干净安装的首次版本探测超时后，只重试一次并保留实际版本。"""

        successful_probe = subprocess.CompletedProcess(
            args=["/fake/pdf2zh_next", "--version"],
            returncode=0,
            stdout="pdf2zh-next version: 2.9.0\n",
            stderr="",
        )
        with (
            patch(
                "paperlocale.workflow.subprocess.run",
                side_effect=[
                    subprocess.TimeoutExpired(
                        cmd=["/fake/pdf2zh_next", "--version"],
                        timeout=60,
                    ),
                    successful_probe,
                ],
            ) as run_probe,
            patch(
                "paperlocale.workflow.importlib.metadata.version",
                return_value="0.6.2",
            ),
            patch(
                "paperlocale.workflow.shutil.which",
                return_value="/fake/pdf2zh_next",
            ),
        ):
            provenance = _layout_provenance("/fake/pdf2zh_next")

        self.assertEqual(run_probe.call_count, 2)
        self.assertEqual(provenance, self.layout_provenance)

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

    def test_partial_translation_keeps_run_in_collected_state(self) -> None:
        """批内部分成功只更新断点证据，不得推进为 translated。"""

        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "source.pdf"
            document = canvas.Canvas(str(source_pdf))
            document.drawString(40, 780, "Synthetic PDF without references")
            document.save()
            run_dir = root / "run"
            manifest = initialize_run(
                source_pdf=source_pdf,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            sources = ("Soil moisture was 10 mm.", "Air temperature was 20 °C.")
            write_jsonl_atomic(
                Path(str(manifest["segments_path"])),
                [{"id": segment_id(text), "source": text} for text in sources],
            )
            manifest["status"] = "collected"
            save_manifest(run_dir, manifest)
            confirm_reference_run(
                run_dir,
                additional_segment_ids=[],
                confirmed_by="test reviewer",
            )

            with self.assertRaisesRegex(ValueError, "合格译文已保存"):
                translate_run(run_dir=run_dir, provider=_Provider(), domain=domain)

            failed_manifest = load_manifest(run_dir)
            self.assertEqual(failed_manifest["status"], "collected")
            self.assertEqual(failed_manifest["translation_count"], 1)
            self.assertTrue(Path(str(failed_manifest["rejected_translations"])).is_file())
            self.assertEqual(
                failed_manifest["domain_pack"]["content_sha256"],
                domain.content_sha256,
            )

    def test_validate_rejects_domain_changed_after_translation(self) -> None:
        """断点续跑不得把翻译时记录的领域包换成另一个同语言包。"""

        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"placeholder")
            run_dir = root / "run"
            manifest = initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            manifest["status"] = "translated"
            manifest["domain_pack"] = {"id": domain.pack_id, "version": domain.version}
            save_manifest(run_dir, manifest)

            changed_domain = replace(domain, pack_id="different-domain")
            with self.assertRaisesRegex(ValueError, "与翻译记录不一致"):
                validate_run(run_dir, changed_domain)

    def test_validate_rejects_reference_map_changed_after_translation(self) -> None:
        """人工确认映射一旦产生译文就必须由清单哈希锁定。"""

        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            document = canvas.Canvas(str(source))
            document.drawString(40, 780, "Synthetic PDF without references")
            document.save()
            run_dir = root / "run"
            manifest = initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            text = "Soil moisture was 10 mm."
            write_jsonl_atomic(
                Path(str(manifest["segments_path"])),
                [{"id": segment_id(text), "source": text}],
            )
            manifest["status"] = "collected"
            save_manifest(run_dir, manifest)
            confirm_reference_run(
                run_dir,
                additional_segment_ids=[],
                confirmed_by="test reviewer",
            )
            translate_run(run_dir=run_dir, provider=_Provider(), domain=domain)

            map_path = run_dir / "reference_map.json"
            map_path.write_text(
                map_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "映射在翻译后发生变化"):
                validate_run(run_dir, domain)

    def test_partial_failure_can_be_confirmed_as_hash_bound_passthrough(self) -> None:
        """失败片段可经人工确认后透传，且不能绕过清单哈希改写映射。"""

        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "source.pdf"
            document = canvas.Canvas(str(source_pdf))
            document.drawString(40, 780, "Synthetic PDF without references")
            document.save()
            run_dir = root / "run"
            manifest = initialize_run(
                source_pdf=source_pdf,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            source = (
                "Alice Smith, Bob Jones, Carol White, David Brown, "
                "Edward Green, and Frances Black"
            )
            sid = segment_id(source)
            write_jsonl_atomic(
                Path(str(manifest["segments_path"])),
                [{"id": sid, "source": source}],
            )
            manifest["status"] = "collected"
            save_manifest(run_dir, manifest)
            confirm_reference_run(
                run_dir,
                additional_segment_ids=[],
                confirmed_by="reviewer",
            )

            provider = _IdentityProvider()
            with self.assertRaisesRegex(ValueError, "未通过门禁"):
                translate_run(run_dir=run_dir, provider=provider, domain=domain)
            confirm_passthrough_run(
                run_dir,
                segment_ids=[sid],
                reason="作者姓名串没有可翻译正文",
                confirmed_by="reviewer",
            )
            self.assertEqual(
                translate_run(run_dir=run_dir, provider=provider, domain=domain),
                (0, 1),
            )
            self.assertEqual(provider.calls, 1)
            translated_manifest = load_manifest(run_dir)
            self.assertEqual(translated_manifest["schema_version"], 4)
            self.assertEqual(translated_manifest["passthrough_segment_count"], 1)
            self.assertTrue(translated_manifest["passthrough_map_sha256"])
            validate_run(run_dir, domain)

            map_path = run_dir / "passthrough_map.json"
            map_path.write_text(
                map_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "透传映射在翻译后发生变化"):
                validate_run(run_dir, domain)

    def test_validate_rejects_domain_content_changed_without_version_bump(self) -> None:
        """相同 id/version 不能掩盖提示词或术语表内容变化。"""

        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(b"placeholder")
            run_dir = root / "run"
            manifest = initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            manifest["status"] = "translated"
            manifest["domain_pack"] = {
                "id": domain.pack_id,
                "version": domain.version,
                "content_sha256": domain.content_sha256,
            }
            save_manifest(run_dir, manifest)

            changed_domain = replace(domain, content_sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "领域包内容"):
                validate_run(run_dir, changed_domain)

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
            self.assertEqual(upgraded["schema_version"], 4)
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

    def test_apply_vector_repair_records_history_and_resets_qa(self) -> None:
        """修复导入必须保留旧 PDF，并强制重新执行机器和人工 QA。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            rendered = root / "rendered.pdf"
            repaired = root / "repaired.pdf"

            for path, with_icon in ((source, False), (rendered, False), (repaired, True)):
                document = canvas.Canvas(str(path))
                document.drawString(40, 780, "Stable scientific text")
                if with_icon:
                    document.ellipse(420, 740, 432, 748, stroke=1, fill=0)
                document.save()

            run_dir = root / "run"
            manifest = initialize_run(
                source_pdf=source,
                run_dir=run_dir,
                source_language="en",
                target_language="zh-CN",
            )
            manifest["status"] = "accepted"
            manifest["rendered_pdf"] = str(rendered.resolve())
            manifest["rendered_sha256"] = hashlib.sha256(rendered.read_bytes()).hexdigest()
            manifest["qa_report"] = str(root / "old-qa.json")
            manifest["accepted_by"] = "reviewer"
            save_manifest(run_dir, manifest)

            result = apply_vector_repair(
                run_dir,
                repaired_pdf=repaired,
                description="恢复首页链接矢量图标",
            )
            updated = load_manifest(run_dir)
            history = updated["repair_history"][0]
            self.assertEqual(result, rendered.resolve())
            self.assertEqual(updated["status"], "rendered")
            self.assertNotIn("qa_report", updated)
            self.assertNotIn("accepted_by", updated)
            self.assertEqual(history["type"], "vector")
            self.assertEqual(history["vector_changes"][0]["page"], 1)
            self.assertTrue(Path(history["backup_pdf"]).is_file())
            self.assertEqual(
                updated["rendered_sha256"],
                hashlib.sha256(rendered.read_bytes()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
