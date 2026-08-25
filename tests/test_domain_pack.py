"""领域包本身必须可加载，且自带案例必须全部通过同一门禁。"""

from __future__ import annotations

import unittest

from paperlocale.contracts import validate_translation
from paperlocale.domains import load_domain_pack


class DomainPackTest(unittest.TestCase):
    def test_builtin_atmospheric_science_pack(self) -> None:
        pack = load_domain_pack("atmospheric-science")
        self.assertEqual(pack.pack_id, "atmospheric-science")
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


if __name__ == "__main__":
    unittest.main()
