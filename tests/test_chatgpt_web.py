"""ChatGPT 网页人工桥接只交换文件，不联网也不读取浏览器登录态。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reportlab.pdfgen import canvas

from paperlocale.chatgpt_web import (
    export_chatgpt_web_batches,
    import_chatgpt_web_responses,
)
from paperlocale.contracts import read_jsonl, segment_id, write_jsonl_atomic
from paperlocale.domains import load_domain_pack
from paperlocale.workflow import (
    confirm_reference_run,
    initialize_run,
    load_manifest,
    save_manifest,
)


class ChatGPTWebBridgeTest(unittest.TestCase):
    def _make_collected_run(self, root: Path, sources: list[str]) -> Path:
        """建立真实可读 PDF 与手工收集片段，保留安全审查所需的可见文本。"""

        source_pdf = root / "source.pdf"
        document = canvas.Canvas(str(source_pdf))
        for index, source in enumerate(sources):
            document.drawString(40, 760 - index * 24, source)
        document.save()

        run_dir = root / "run"
        initialize_run(
            source_pdf=source_pdf,
            run_dir=run_dir,
            source_language="en",
            target_language="zh-CN",
        )
        manifest = load_manifest(run_dir)
        write_jsonl_atomic(
            Path(str(manifest["segments_path"])),
            [{"id": segment_id(source), "source": source} for source in sources],
        )
        manifest["segment_count"] = len(sources)
        manifest["status"] = "collected"
        save_manifest(run_dir, manifest)
        confirm_reference_run(
            run_dir,
            additional_segment_ids=[],
            confirmed_by="test reviewer",
        )
        return run_dir

    @staticmethod
    def _write_response(
        batch: dict[str, object],
        targets: dict[str, str],
    ) -> None:
        """按导出清单写入网页回复，测试与真实人工保存使用同一格式。"""

        response = {
            "batch_id": batch["batch_id"],
            "batch_sha256": batch["batch_sha256"],
            "translations": [
                {"id": sid, "target": targets[str(sid)]}
                for sid in batch["segment_ids"]
            ],
        }
        Path(str(batch["response"])).write_text(
            json.dumps(response, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_export_binds_prompts_and_does_not_touch_browser(self) -> None:
        """导出必须生成稳定批次和人工说明，不能包含任何网页登录实现。"""

        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._make_collected_run(
                Path(directory),
                ["Soil moisture was 10 mm."],
            )
            exported = export_chatgpt_web_batches(
                run_dir=run_dir,
                domain=domain,
            )
            self.assertEqual(exported["provider"], "chatgpt-web-manual")
            self.assertEqual(exported["batch_count"], 1)
            prompt = Path(str(exported["batches"][0]["prompt"])).read_text(
                encoding="utf-8"
            )
            self.assertIn("普通 **Chat** 模式", prompt)
            self.assertIn(str(exported["batches"][0]["batch_sha256"]), prompt)
            manifest = load_manifest(run_dir)
            self.assertTrue(manifest["chatgpt_web_batch_manifest_sha256"])
            self.assertEqual(manifest["status"], "collected")

    def test_import_resumes_after_missing_later_batch(self) -> None:
        """缺少后续网页回复时保留已通过译文，补齐后只处理剩余片段。"""

        sources = [
            "Soil moisture was 10 mm.",
            "Air temperature was 20 °C.",
        ]
        targets = {
            segment_id(sources[0]): "土壤湿度为10 mm。",
            segment_id(sources[1]): "气温为20 °C。",
        }
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._make_collected_run(Path(directory), sources)
            exported = export_chatgpt_web_batches(
                run_dir=run_dir,
                domain=domain,
                max_segments=1,
            )
            first, second = exported["batches"]
            self._write_response(first, targets)

            with self.assertRaisesRegex(ValueError, "缺少覆盖当前片段"):
                import_chatgpt_web_responses(
                    run_dir=run_dir,
                    domain=domain,
                    model_label="ChatGPT test model",
                )
            self.assertEqual(len(read_jsonl(run_dir / "translations.jsonl")), 1)
            self.assertEqual(load_manifest(run_dir)["status"], "collected")

            self._write_response(second, targets)
            self.assertEqual(
                import_chatgpt_web_responses(
                    run_dir=run_dir,
                    domain=domain,
                    model_label="ChatGPT test model",
                ),
                (1, 1),
            )
            manifest = load_manifest(run_dir)
            self.assertEqual(manifest["status"], "translated")
            self.assertEqual(
                manifest["translation_provider"],
                {
                    "provider": "chatgpt-web-manual",
                    "model": "ChatGPT test model",
                    "interface_mode": "manual-copy-paste",
                    "browser_automation": False,
                    "batch_manifest_sha256": manifest[
                        "chatgpt_web_batch_manifest_sha256"
                    ],
                },
            )
            history = read_jsonl(run_dir / "chatgpt_web" / "import_history.jsonl")
            self.assertEqual([row["outcome"] for row in history], ["failed", "completed"])
            self.assertEqual(len(list((run_dir / "chatgpt_web" / "imports").glob("*.json"))), 2)

    def test_import_rejects_wrong_batch_hash_before_writing_translations(self) -> None:
        """回复若不是针对当前提示批次，不能靠正确片段 ID 绕过身份校验。"""

        source = "Soil moisture was 10 mm."
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._make_collected_run(Path(directory), [source])
            exported = export_chatgpt_web_batches(run_dir=run_dir, domain=domain)
            batch = exported["batches"][0]
            response = {
                "batch_id": batch["batch_id"],
                "batch_sha256": "0" * 64,
                "translations": [
                    {"id": segment_id(source), "target": "土壤湿度为10 mm。"}
                ],
            }
            Path(str(batch["response"])).write_text(
                json.dumps(response, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "batch_sha256 不匹配"):
                import_chatgpt_web_responses(
                    run_dir=run_dir,
                    domain=domain,
                    model_label="ChatGPT test model",
                )
            self.assertFalse((run_dir / "translations.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
