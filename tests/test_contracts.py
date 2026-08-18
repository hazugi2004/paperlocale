"""核心门禁测试不调用任何外部模型或 PDF 服务。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paperlocale.contracts import (
    segment_id,
    validate_translation,
    validate_translation_files,
    write_jsonl_atomic,
)
from paperlocale.domains import load_domain_pack


class TranslationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain = load_domain_pack("atmospheric-science")

    def test_segment_id_normalizes_line_endings(self) -> None:
        self.assertEqual(segment_id(" A\r\nB "), segment_id("A\nB"))

    def test_formula_loss_is_rejected(self) -> None:
        errors = validate_translation("Index {v1} equals 2.5 mm.", "指数等于2.5 mm。")
        self.assertTrue(any("formula" in error for error in errors))

    def test_style_reordering_is_rejected(self) -> None:
        source = "<style id='1'>A</style> and <style id='2'>B</style>"
        target = "<style id='2'>乙</style>和<style id='1'>甲</style>"
        self.assertTrue(any("style" in error for error in validate_translation(source, target)))

    def test_number_and_unit_loss_is_rejected(self) -> None:
        errors = validate_translation("Resolution is 0.25° and rainfall is 10 mm.", "分辨率和降水量如文中所示。")
        self.assertTrue(any("number" in error for error in errors))
        self.assertTrue(any("unit" in error for error in errors))

    def test_required_domain_term_is_enforced(self) -> None:
        source = "Gross primary productivity (GPP) was measured."
        bad = "测量了生态系统生产力（GPP）。"
        self.assertTrue(any("领域术语" in error for error in validate_translation(source, bad, self.domain)))

    def test_valid_scientific_translation_passes(self) -> None:
        source = "Gross primary productivity (GPP) changed by 2.5% under {v1}."
        target = "总初级生产力（GPP）在{v1}条件下变化2.5%。"
        self.assertEqual(validate_translation(source, target, self.domain), [])

    def test_atomic_jsonl_and_file_closure(self) -> None:
        source = "Soil moisture was 10 mm."
        sid = segment_id(source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            write_jsonl_atomic(segments, [{"id": sid, "source": source}])
            write_jsonl_atomic(
                translations,
                [{"id": sid, "source": source, "target": "土壤湿度为10 mm。"}],
            )
            validate_translation_files(segments, translations, self.domain)
            self.assertFalse((root / "translations.jsonl.tmp").exists())

    def test_duplicate_translation_id_is_rejected(self) -> None:
        source = "Soil moisture was 10 mm."
        sid = segment_id(source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            segments.write_text(json.dumps({"id": sid, "source": source}) + "\n", encoding="utf-8")
            row = json.dumps({"id": sid, "source": source, "target": "土壤湿度为10 mm。"}, ensure_ascii=False)
            translations.write_text(row + "\n" + row + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复 ID"):
                validate_translation_files(segments, translations, self.domain)


if __name__ == "__main__":
    unittest.main()
