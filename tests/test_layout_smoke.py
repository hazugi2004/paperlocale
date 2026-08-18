"""版面冒烟脚本的确定性 Provider 不应破坏领域术语或公式标记。"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from paperlocale.contracts import validate_translation
from paperlocale.domains import load_domain_pack
from paperlocale.providers import Segment, TranslationContext


def _load_smoke_module():
    """脚本不属于安装包，测试通过文件位置加载其公开测试 Provider。"""

    path = Path(__file__).parents[1] / "scripts" / "layout_smoke.py"
    specification = importlib.util.spec_from_file_location("layout_smoke", path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"无法加载版面冒烟脚本：{path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class LayoutSmokeTest(unittest.TestCase):
    def test_deterministic_translation_preserves_contract(self) -> None:
        domain = load_domain_pack("atmospheric-science")
        context = TranslationContext("en", "zh-CN", domain)
        source = "Compound dry-hot event; soil moisture 10 mm; E {v1}mc{v2}."
        segment = Segment("synthetic-id", source)
        provider = _load_smoke_module().SmokeProvider()
        target = provider.translate([segment], context)[0].target
        self.assertEqual(validate_translation(source, target, domain), [])


if __name__ == "__main__":
    unittest.main()
