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
for provider in ["codex-local", "openai-api", "openai-compatible", "qwen-mt"] {
    for qa in [true, false] {
        let options = TranslationOptions(pdf: "/源 文.pdf", runDirectory: "/断点",
            provider: provider, model: "test-model", effort: "medium",
            baseURL: "https://example.com/v1", outputPDF: "/译文 目录/结果.pdf", runQA: qa)
        let args = options.arguments
        precondition(args.contains("--reasoning-effort") == (provider == "codex-local"))
        precondition(args.contains("--base-url") == (provider == "openai-compatible" || provider == "qwen-mt"))
        if let key = args.firstIndex(of: "--api-key-env") {
            precondition(provider != "codex-local")
            precondition(args[key + 1] == (provider == "openai-api" ? "OPENAI_API_KEY" : "PAPERLOCALE_API_KEY"))
        } else { precondition(provider == "codex-local") }
        precondition(args.contains("--no-qa") == !qa)
        precondition(args[args.firstIndex(of: "--output-pdf")! + 1] == "/译文 目录/结果.pdf")
        precondition(args[1] == "/源 文.pdf")
    }
}
let inherited = ["OPENAI_API_KEY": "official-test-key", "PAPERLOCALE_API_KEY": "compatible-test-key", "PATH": "/test"]
precondition(TranslationOptions.keyEnvironment(inherited, provider: "openai-api", key: "window-test-key", includeKey: true)
    == ["OPENAI_API_KEY": "window-test-key", "PATH": "/test"])
precondition(TranslationOptions.keyEnvironment(inherited, provider: "openai-api", key: "", includeKey: true)
    == ["OPENAI_API_KEY": "official-test-key", "PATH": "/test"])
precondition(TranslationOptions.keyEnvironment(inherited, provider: "qwen-mt", key: "", includeKey: true)
    == ["PAPERLOCALE_API_KEY": "compatible-test-key", "PATH": "/test"])
precondition(TranslationOptions.keyEnvironment(inherited, provider: "codex-local", key: "", includeKey: true)
    == ["PATH": "/test"])
precondition(TranslationOptions.keyEnvironment(inherited, provider: "openai-api", key: "window-test-key", includeKey: false)
    == ["PATH": "/test"])
let legacy = TranslationOptions(pdf: "/源 文.pdf", runDirectory: "/旧断点", provider: "codex-local",
    model: "original-model", effort: "high", baseURL: "", outputPDF: "", runQA: false,
    layoutMode: "legacy", domain: "general", referencePolicy: "translate-titles").arguments
precondition(!legacy.contains("--no-qa"))
precondition(legacy[legacy.firstIndex(of: "--layout-mode")! + 1] == "legacy")
precondition(legacy[legacy.firstIndex(of: "--domain")! + 1] == "general")
precondition(legacy[legacy.firstIndex(of: "--reference-policy")! + 1] == "translate-titles")
print("Swift provider and output options passed")
''')
            binary = Path(directory) / "options-test"
            subprocess.run(["xcrun", "swiftc", str(root / "macos/TranslationOptions.swift"),
                            str(main), "-o", str(binary)], check=True, capture_output=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn("passed", result.stdout)
