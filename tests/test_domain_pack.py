"""领域包本身必须可加载，且自带案例必须全部通过同一门禁。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paperlocale.contracts import match_glossary, validate_translation
from paperlocale.domains import load_domain_pack


class DomainPackTest(unittest.TestCase):
    def test_builtin_atmospheric_science_pack(self) -> None:
        pack = load_domain_pack("atmospheric-science")
        self.assertEqual(pack.pack_id, "atmospheric-science")
        self.assertEqual(pack.version, "2.0.0")
        self.assertEqual(len(pack.content_sha256), 64)
        self.assertGreaterEqual(len(pack.eval_cases), 5)
        for case in pack.eval_cases:
            self.assertEqual(
                validate_translation(case["source"], case["target"], pack),
                [],
                msg=case["source"],
            )

    def test_atmospheric_runs_can_reload_the_original_pack(self) -> None:
        # 本机 0.8.6 旧断点实际记录的哈希，避免归档内容被误改。
        fingerprint = "c671514759de3829d1b180c165eae73e42e366e40c65cff1f797ef52e1514006"
        old = load_domain_pack("atmospheric-science", recorded_sha256=fingerprint)
        current = load_domain_pack("atmospheric-science", recorded_sha256="unknown")
        self.assertEqual((old.version, old.content_sha256), ("1.1.0", fingerprint))
        self.assertEqual(len(old.glossary), 18)
        self.assertEqual(current.version, "2.0.0")
        source, target = "Potential evapotranspiration was estimated.", "估算了潜在蒸散量。"
        self.assertTrue(validate_translation(source, target, old))
        self.assertEqual(validate_translation(source, target, current), [])

    def test_matching_uses_surface_variants_and_full_terms(self) -> None:
        pack = load_domain_pack("atmospheric-science")
        source = (
            "Compound hot–dry events, conditional probabilities, and\n"
            "wet‑bulb temperature; spatiotemporal resolution; "
            "https://example.test/soil-moisture"
        )
        matches = {surface: entry.target for surface, entry in match_glossary(source, pack)}
        self.assertEqual(matches, {
            "Compound hot–dry events": "复合干热事件",
            "conditional probabilities": "条件概率",
            "wet‑bulb temperature": "湿球温度",
            "spatiotemporal resolution": "时空分辨率",
        })
        source = "Virtual potential temperature and potential temperature were estimated."
        target = "估算了虚温位温。"
        # 长词中的“位温”不改变匹配范围，独立出现的英文短词也须生效。
        self.assertEqual([entry.source for _, entry in match_glossary(source, pack)],
                         ["virtual potential temperature", "potential temperature"])
        self.assertTrue(validate_translation(source, "估算了虚温。", pack))
        self.assertEqual(validate_translation(source, target, pack), [])
        errors = validate_translation("Compound hot–dry events occurred.", "发生了高温事件。", pack)
        self.assertEqual(len(errors), 1)
        self.assertIn("复合干热事件", errors[0])

    def test_uppercase_heading_terms_do_not_become_abbreviations(self) -> None:
        pack = load_domain_pack("atmospheric-science")
        source = "GROSS PRIMARY PRODUCTIVITY (GPP) AND SOIL MOISTURE"
        self.assertEqual(validate_translation(source, "总初级生产力（GPP）与土壤湿度", pack), [])
        errors = validate_translation(source, "总初级生产力与土壤湿度", pack)
        self.assertEqual(len(errors), 1)
        self.assertIn("GPP", errors[0])

    def test_builtin_ecology_pack(self) -> None:
        pack = load_domain_pack("ecology")
        self.assertEqual(pack.pack_id, "ecology")
        self.assertEqual((pack.source_language, pack.target_language), ("en", "zh-CN"))
        self.assertGreaterEqual(len(pack.glossary), 10)
        self.assertGreaterEqual(len(pack.eval_cases), 3)
        for case in pack.eval_cases:
            self.assertEqual(
                validate_translation(case["source"], case["target"], pack),
                [],
                msg=case["source"],
            )

    def test_content_hash_changes_when_pack_content_changes(self) -> None:
        """同一 id/version 下改写提示词也必须产生不同内容身份。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "id": "test-pack",
                        "version": "1.0.0",
                        "source_language": "en",
                        "target_language": "zh-CN",
                    }
                ),
                encoding="utf-8",
            )
            (root / "glossary.tsv").write_text(
                "source\ttarget\trequired\tnote\nsoil moisture\t土壤湿度\ttrue\t\n",
                encoding="utf-8",
            )
            (root / "eval_cases.jsonl").write_text(
                '{"source":"soil moisture","target":"土壤湿度"}\n',
                encoding="utf-8",
            )
            (root / "prompt.txt").write_text("第一版提示", encoding="utf-8")
            first = load_domain_pack(root).content_sha256
            (root / "prompt.txt").write_text("第二版提示", encoding="utf-8")
            second = load_domain_pack(root).content_sha256
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
