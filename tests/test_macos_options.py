"""验证真实 Swift 参数生成：非 Codex 不传推理档位，路径保持单一参数。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("xcrun"), "需要 macOS Swift 编译器")
class MacOSOptionsTest(unittest.TestCase):
    def test_provider_and_output_arguments(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main.swift"
            main.write_text('''
import Foundation
for provider in ["codex-local", "openai-compatible", "qwen-mt"] {
    for qa in [true, false] {
        let options = TranslationOptions(pdf: "/源 文.pdf", runDirectory: "/断点",
            provider: provider, model: "test-model", effort: "medium",
            baseURL: "https://example.com/v1", outputPDF: "/译文 目录/结果.pdf", runQA: qa)
        let args = options.arguments
        precondition(args.contains("--reasoning-effort") == (provider == "codex-local"))
        precondition(args.contains("--base-url") == (provider != "codex-local"))
        precondition(args.contains("--no-qa") == !qa)
        precondition(args[args.firstIndex(of: "--output-pdf")! + 1] == "/译文 目录/结果.pdf")
        precondition(args[1] == "/源 文.pdf")
    }
}
print("Swift provider and output options passed")
''')
            binary = Path(directory) / "options-test"
            subprocess.run(["xcrun", "swiftc", str(root / "macos/TranslationOptions.swift"),
                            str(main), "-o", str(binary)], check=True, capture_output=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn("passed", result.stdout)
