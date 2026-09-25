"""可执行路径选择不依赖测试机安装，不读取任何登录材料。"""
import unittest
from unittest.mock import patch
from paperlocale.providers.codex_local import resolve_codex


class CodexDiscoveryTests(unittest.TestCase):
    def test_explicit_and_path_take_precedence(self):
        with patch("shutil.which", return_value="/path/codex"):
            self.assertEqual(resolve_codex("/chosen/codex"), "/chosen/codex")
            self.assertEqual(resolve_codex(None), "/path/codex")

    def test_broken_link_can_find_installed_desktop_binary(self):
        with patch("shutil.which", return_value=None), \
             patch("sys.platform", "darwin"), \
             patch("pathlib.Path.is_file", autospec=True,
                   side_effect=lambda p: str(p) == "/Applications/ChatGPT.app/Contents/Resources/codex"), \
             patch("os.access", return_value=True):
            self.assertEqual(resolve_codex(None), "/Applications/ChatGPT.app/Contents/Resources/codex")

    def test_missing_binary_does_not_invent_a_path(self):
        with patch("shutil.which", return_value=None), patch("sys.platform", "linux"):
            with self.assertRaisesRegex(FileNotFoundError, "软链接"):
                resolve_codex(None)
