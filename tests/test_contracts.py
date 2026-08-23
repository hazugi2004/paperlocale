"""核心门禁测试不调用任何外部模型或 PDF 服务。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paperlocale.contracts import (
    protected_counts,
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

    def test_section_headings_are_not_treated_as_abbreviations(self) -> None:
        """论文结构标题不是必须原样保留的科学缩写。"""

        for heading in ("ABSTRACT", "KEYWORDS", "REFERENCES"):
            with self.subTest(heading=heading):
                self.assertEqual(protected_counts(heading)["abbreviation"], {})

    def test_hyphenated_decade_keeps_year_without_false_negative_sign(self) -> None:
        """mid-1960s 应保护年代 1960，但连字符不是负号。"""

        source = "Compound events increased in the mid-1960s."
        target = "复合事件在20世纪60年代（1960年代中期）有所增加。"
        self.assertEqual(protected_counts(source)["number"], {"1960": 1})
        self.assertEqual(validate_translation(source, target), [])

    def test_citation_numbers_attached_to_english_and_chinese_are_equivalent(self) -> None:
        """英文论文引文号常紧贴单词，中文译文也可紧贴汉字。"""

        source = "Vegetation productivity3,17,18 follows earlier studies24,25."
        target = "植被生产力3,17,18与早期研究24,25的结果一致。"
        self.assertEqual(
            protected_counts(source)["number"],
            protected_counts(target)["number"],
        )
        self.assertEqual(validate_translation(source, target), [])

    def test_chinese_text_after_url_and_doi_is_not_part_of_identifier(self) -> None:
        """中文可直接承接网址，但不能因此把中文后缀误识别为 URL 或 DOI。"""

        source = "Data are available at https://doi.org/10.5281/zenodo.10878698."
        target = "数据可从https://doi.org/10.5281/zenodo.10878698获取。"
        self.assertEqual(validate_translation(source, target), [])

    def test_pdf_line_break_inside_doi_url_is_ignored(self) -> None:
        """doi.org 与 DOI 之间的版面空格不改变链接身份。"""

        source = "Available at https://doi.org/ 10.5281/zenodo.10878698."
        target = "可在https://doi.org/10.5281/zenodo.10878698获取。"
        self.assertEqual(validate_translation(source, target), [])

    def test_pdf_line_break_after_hyphen_does_not_change_abbreviation(self) -> None:
        """版面断行空格可消除，但缩写本身仍必须完整保留。"""

        source = "HDI- MSDI and SSP5- 8.5"
        target = "HDI-MSDI和SSP5-8.5"
        self.assertEqual(validate_translation(source, target), [])

    def test_pdf_word_fusion_before_abbreviation_is_ignored(self) -> None:
        """正文词与大写缩写粘连时，缩写身份仍按可见排版含义核对。"""

        self.assertEqual(
            validate_translation("andHDI- MSDI", "以及HDI-MSDI"),
            [],
        )

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
