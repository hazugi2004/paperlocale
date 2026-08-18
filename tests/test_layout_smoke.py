"""版面冒烟脚本的确定性 Provider 不应破坏领域术语或公式标记。"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image

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

    def test_demo_gif_contains_source_translation_and_comparison_frames(self) -> None:
        """README 演示必须稳定包含原文、译文和完整 QA 三个阶段。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            comparison = root / "comparison.png"
            image = Image.new("RGB", (400, 240), "white")
            # 两半使用不同颜色，确保测试输入确实可以识别拆分边界。
            image.paste("steelblue", (0, 0, 200, 240))
            image.paste("seagreen", (200, 0, 400, 240))
            image.save(comparison)

            output = _load_smoke_module().build_demo_gif(
                comparison,
                root / "demo.gif",
            )
            with Image.open(output) as demo:
                self.assertTrue(demo.is_animated)
                self.assertEqual(demo.n_frames, 3)
                self.assertEqual(demo.size, (960, 720))


if __name__ == "__main__":
    unittest.main()
