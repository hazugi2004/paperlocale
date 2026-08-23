"""Provider 测试全部使用模拟响应，不联网、不读取本机登录态。"""

from __future__ import annotations

import http.client
import json
import unittest
from unittest.mock import patch

from paperlocale.domains import load_domain_pack
from paperlocale.providers import (
    CodexLocalProvider,
    OpenAICompatibleProvider,
    QwenMTProvider,
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
            self.assertIn("--model", command)
            self.assertIn("gpt-5.6-sol", command)
            self.assertIn("model_reasoning_effort=\"high\"", command)
            self.assertNotIn("auth.json", " ".join(command))
            self.assertIn("待翻译 JSON", str(kwargs["input"]))
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        provider = CodexLocalProvider(
            codex_bin="/fake/codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        with patch("paperlocale.providers.codex_local.subprocess.run", side_effect=fake_run):
            result = provider.translate([self.segment], self.context)
        self.assertEqual(result[0].target, "土壤湿度为10 mm。")

    def test_codex_provenance_records_cli_model_and_effort(self) -> None:
        """运行清单需要足以复核本机会员额度调用的非敏感身份。"""

        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "codex-cli 0.148.0\n", "stderr": ""},
        )()
        provider = CodexLocalProvider(
            codex_bin="/fake/codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        with patch(
            "paperlocale.providers.codex_local.subprocess.run",
            return_value=completed,
        ):
            self.assertEqual(
                provider.provenance(),
                {
                    "provider": "codex-local",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                    "codex_cli_version": "codex-cli 0.148.0",
                },
            )

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

    def test_openai_compatible_rejects_remote_plaintext_http(self) -> None:
        """API Key 不能通过明文网络发送到远程主机。"""

        with self.assertRaisesRegex(ValueError, "必须使用 HTTPS"):
            OpenAICompatibleProvider(
                base_url="http://api.example.test/v1",
                api_key="secret-key",
                model="example-model",
            )

    def test_qwen_mt_translates_each_segment_with_official_options(self) -> None:
        """专用翻译模型必须收到单条正文和明确的英译简中语言代码。"""

        def fake_urlopen(request, timeout: int):
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(
                body["messages"],
                [{"role": "user", "content": self.segment.source}],
            )
            self.assertEqual(body["translation_options"]["source_lang"], "en")
            self.assertEqual(body["translation_options"]["target_lang"], "zh")
            self.assertEqual(
                body["translation_options"]["domains"],
                self.context.domain.prompt,
            )
            self.assertIn(
                {"source": "soil moisture", "target": "土壤湿度"},
                body["translation_options"]["terms"],
            )
            self.assertIn(
                {"source": "mm", "target": "mm"},
                body["translation_options"]["terms"],
            )
            self.assertNotIn("secret-key", json.dumps(body))
            self.assertEqual(timeout, 300)
            return _Response(
                {"choices": [{"message": {"content": "土壤湿度为10 mm。"}}]}
            )

        provider = QwenMTProvider(
            base_url="https://example.test/compatible-mode/v1",
            api_key="secret-key",
            model="qwen-mt-plus",
        )
        with patch(
            "paperlocale.providers.qwen_mt.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = provider.translate([self.segment], self.context)
        self.assertEqual(result, [type(result[0])("id-1", "土壤湿度为10 mm。")])

    def test_qwen_mt_restores_formula_placeholders(self) -> None:
        """模型只看到稳定哨兵，Provider 必须在返回前恢复原公式占位符。"""

        segment = Segment("id-formula", "HDI {v1} was below -0.8.")

        def fake_urlopen(request, timeout: int):
            body = json.loads(request.data.decode("utf-8"))
            content = body["messages"][0]["content"]
            self.assertNotIn("{v1}", content)
            self.assertIn("[PLPROTECTED0001]", content)
            return _Response(
                {
                    "choices": [
                        {"message": {"content": "HDI [PLPROTECTED0001]低于-0.8。"}}
                    ]
                }
            )

        provider = QwenMTProvider(
            base_url="https://example.test/compatible-mode/v1",
            api_key="secret-key",
            model="qwen-mt-plus",
        )
        with patch(
            "paperlocale.providers.qwen_mt.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = provider.translate([segment], self.context)
        self.assertEqual(result[0].target, "HDI {v1}低于-0.8。")

    def test_qwen_mt_retries_temporary_disconnect(self) -> None:
        """临时断连可短重试，但同一片段最终只返回一条译文。"""

        provider = QwenMTProvider(
            base_url="https://example.test/compatible-mode/v1",
            api_key="secret-key",
            model="qwen-mt-plus",
        )
        response = _Response(
            {"choices": [{"message": {"content": "土壤湿度为10 mm。"}}]}
        )
        with (
            patch(
                "paperlocale.providers.qwen_mt.urllib.request.urlopen",
                side_effect=[http.client.RemoteDisconnected(), response],
            ) as urlopen,
            patch("paperlocale.providers.qwen_mt.time.sleep") as sleep,
        ):
            result = provider.translate([self.segment], self.context)
        self.assertEqual(result[0].target, "土壤湿度为10 mm。")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_qwen_mt_restores_ascii_hyphen_in_abbreviation(self) -> None:
        """模型排版用不换行连字符时，科学缩写仍恢复为原始 ASCII 标记。"""

        provider = QwenMTProvider(
            base_url="https://example.test/compatible-mode/v1",
            api_key="secret-key",
            model="qwen-mt-plus",
        )
        response = _Response({"choices": [{"message": {"content": "采用HDI‑P指数。"}}]})
        with patch(
            "paperlocale.providers.qwen_mt.urllib.request.urlopen",
            return_value=response,
        ):
            result = provider.translate(
                [Segment("id-hyphen", "The HDI-P index was used.")],
                self.context,
            )
        self.assertEqual(result[0].target, "采用HDI-P指数。")

    def test_qwen_mt_rejects_a_deleted_protection_marker(self) -> None:
        """模型删除哨兵时必须失败，不能根据缩写位置猜测补回。"""

        provider = QwenMTProvider(
            base_url="https://example.test/compatible-mode/v1",
            api_key="secret-key",
            model="qwen-mt-plus",
        )
        response = _Response(
            {"choices": [{"message": {"content": "标准化降水指数（SPI）。"}}]}
        )
        with (
            patch(
                "paperlocale.providers.qwen_mt.urllib.request.urlopen",
                return_value=response,
            ),
            self.assertRaisesRegex(ValueError, "删除了保护标记"),
        ):
            provider.translate(
                [Segment("id-style", "Standardized Precipitation Index (SPI{v1}).")],
                self.context,
            )

    def test_openai_compatible_allows_loopback_http(self) -> None:
        """本机 Ollama 等兼容服务可以继续使用明确的 loopback HTTP。"""

        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:11434/v1",
            api_key="local-placeholder",
            model="local-model",
        )
        self.assertEqual(provider.endpoint, "http://127.0.0.1:11434/v1/chat/completions")

    def test_openai_compatible_rejects_credentials_and_query_in_url(self) -> None:
        """密钥只允许走 Authorization header，端点身份必须无歧义。"""

        invalid_urls = (
            "https://user:password@example.test/v1",
            "https://example.test/v1?tenant=secret",
            "https://example.test/v1#fragment",
            "https://example.test/v1/chat/completions",
        )
        for base_url in invalid_urls:
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                OpenAICompatibleProvider(
                    base_url=base_url,
                    api_key="secret-key",
                    model="example-model",
                )


if __name__ == "__main__":
    unittest.main()
