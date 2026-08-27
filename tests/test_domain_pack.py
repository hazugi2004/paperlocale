"""领域包本身必须可加载，且自带案例必须全部通过同一门禁。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paperlocale.contracts import validate_translation
from paperlocale.domains import load_domain_pack


class DomainPackTest(unittest.TestCase):
    def test_builtin_atmospheric_science_pack(self) -> None:
        pack = load_domain_pack("atmospheric-science")
        self.assertEqual(pack.pack_id, "atmospheric-science")
        self.assertEqual(len(pack.content_sha256), 64)
        self.assertGreaterEqual(len(pack.glossary), 15)
        self.assertGreaterEqual(len(pack.eval_cases), 5)
        for case in pack.eval_cases:
            self.assertEqual(
                validate_translation(case["source"], case["target"], pack),
                [],
                msg=case["source"],
            )

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
