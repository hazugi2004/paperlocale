"""Provider 测试全部使用模拟响应，不联网、不读取本机登录态。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from paperlocale.domains import load_domain_pack
from paperlocale.providers import (
    CodexLocalProvider,
    OpenAICompatibleProvider,
    Segment,
    TranslationContext,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class ProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.domain = load_domain_pack("atmospheric-science")
        cls.context = TranslationContext("en", "zh-CN", cls.domain)
        cls.segment = Segment("id-1", "Soil moisture was 10 mm.")
        cls.translation_payload = {
            "translations": [{"id": "id-1", "target": "土壤湿度为10 mm。"}]
        }

    def test_codex_provider_uses_read_only_structured_mode(self) -> None:
        def fake_run(command: list[str], **kwargs: object):
            output = command[command.index("--output-last-message") + 1]
            with open(output, "w", encoding="utf-8") as handle:
                json.dump(self.translation_payload, handle, ensure_ascii=False)
            self.assertIn("read-only", command)
            self.assertIn("--ephemeral", command)
            self.assertIn("--ignore-user-config", command)
            self.assertNotIn("auth.json", " ".join(command))
            self.assertIn("待翻译 JSON", str(kwargs["input"]))
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        provider = CodexLocalProvider(codex_bin="/fake/codex")
        with patch("paperlocale.providers.codex_local.subprocess.run", side_effect=fake_run):
            result = provider.translate([self.segment], self.context)
        self.assertEqual(result[0].target, "土壤湿度为10 mm。")

    def test_openai_compatible_provider_keeps_key_out_of_body(self) -> None:
        api_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(self.translation_payload, ensure_ascii=False)
                    }
                }
            ]
        }

        def fake_urlopen(request, timeout: int):
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
            self.assertNotIn("secret-key", json.dumps(body))
            self.assertEqual(timeout, 300)
            return _Response(api_payload)

        provider = OpenAICompatibleProvider(
            base_url="https://example.test/v1",
            api_key="secret-key",
            model="example-model",
        )
        with patch(
            "paperlocale.providers.openai_compatible.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = provider.translate([self.segment], self.context)
        self.assertEqual(result[0].id, "id-1")


if __name__ == "__main__":
    unittest.main()
