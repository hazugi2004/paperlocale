"""Provider 测试全部使用模拟响应，不联网、不读取本机登录态。"""

from __future__ import annotations

import http.client
import json
import re
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
                json.dump({"translations": {"s1": "土壤湿度为10 mm。"}}, handle, ensure_ascii=False)
            self.assertIn("read-only", command)
            self.assertIn("--ephemeral", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--model", command)
            self.assertIn("gpt-5.6-sol", command)
            self.assertIn("model_reasoning_effort=\"high\"", command)
            self.assertNotIn("auth.json", " ".join(command))
            self.assertIn("待翻译 JSON", str(kwargs["input"]))
            self.assertIn('"must_preserve"', str(kwargs["input"]))
            self.assertIn('"mm": 1', str(kwargs["input"]))
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        provider = CodexLocalProvider(
            codex_bin="/fake/codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        with patch("paperlocale.providers.codex_local.subprocess.run", side_effect=fake_run):
            result = provider.translate([self.segment], self.context)
        self.assertEqual(result[0].target, "土壤湿度为10 mm。")
        self.assertEqual(result[0].id, self.segment.id)

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
                [{"role": "user", "content": "Soil moisture was 10 mm."}],
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
            model="qwen-mt-plus", min_request_interval_seconds=0,
        )
        with patch(
            "paperlocale.providers.qwen_mt.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = provider.translate([self.segment], self.context)
        self.assertEqual(result, [type(result[0])("id-1", "土壤湿度为10 mm。")])

    def test_qwen_mt_does_not_send_substring_glossary_term(self) -> None:
        """spatiotemporal 不能让 Qwen 收到并不存在的 temporal 术语干预。"""

        segment = Segment("id-spatiotemporal", "High spatiotemporal resolution data.")

        def fake_urlopen(request, timeout: int):
            body = json.loads(request.data.decode("utf-8"))
            sources = {entry["source"] for entry in body["translation_options"]["terms"]}
            self.assertNotIn("temporal resolution", sources)
            return _Response(
                {"choices": [{"message": {"content": "高时空分辨率资料。"}}]}
            )

        provider = QwenMTProvider(
            base_url="https://example.test/compatible-mode/v1",
            api_key="secret-key",
            model="qwen-mt-plus", min_request_interval_seconds=0,
        )
        with patch(
            "paperlocale.providers.qwen_mt.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = provider.translate([segment], self.context)
        self.assertEqual(result[0].target, "高时空分辨率资料。")

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
                        {"message": {"content": "[PLPROTECTED0001] [PLPROTECTED0002]低于[PLPROTECTED0003]。"}}
                    ]
                }
            )

        provider = QwenMTProvider(
            base_url="https://example.test/compatible-mode/v1",
            api_key="secret-key",
            model="qwen-mt-plus", min_request_interval_seconds=0,
        )
        with patch(
            "paperlocale.providers.qwen_mt.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = provider.translate([segment], self.context)
        self.assertEqual(result[0].target, "HDI {v1}低于-0.8。")

    def test_qwen_mt_shields_wrapped_urls_and_repeated_abbreviations(self) -> None:
        """网址断行和内嵌公式保持原样，SST 每次出现对应不同的可验证标记。"""
        source = ("SST measured SST at http://www.cru.uea. ac.uk/data and "
                  "https://example.org/gcos{v1}wgsp/ Timeseries/Nino34/.")
        sent = {}
        def respond(request, timeout):
            body = json.loads(request.data)
            wire = body["messages"][0]["content"]
            sent["wire"] = wire
            self.assertNotIn("http", wire)
            self.assertNotIn("{v1}", wire)
            self.assertNotIn("SST", wire)
            markers = re.findall(r"\[PLPROTECTED\d+\]", wire)
            self.assertEqual(len(markers), 4)
            self.assertEqual(len(set(markers)), 4)
            return _Response({"choices": [{"message": {"content": "译文 " + wire}}]})
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret-key", model="qwen-mt-plus", min_request_interval_seconds=0)
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=respond):
            target = provider.translate([Segment("id-url", source)], self.context)[0].target
        self.assertEqual(target.count("SST"), 2)
        self.assertIn("http://www.cru.uea. ac.uk/data", target)
        self.assertIn("https://example.org/gcos{v1}wgsp/ Timeseries/Nino34/.", target)

    def test_qwen_mt_discards_unknown_or_duplicate_full_response(self) -> None:
        """未知/重复标记不做模糊匹配，只从原文位置恢复两次SST，丢弃坏译文。"""
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret-key", model="qwen-mt-plus", min_request_interval_seconds=0)
        for extra in ["[PLPROTECTED0001]", "[PLPROTECTED9999]",
                      "[PLPROTECTEDX9999]", "[PLPROTECTED 9999]", "[PLPROTECTED0001oops]"]:
            calls = []
            def respond(request, timeout):
                wire = json.loads(request.data)["messages"][0]["content"]
                calls.append(wire)
                if len(calls) == 1:
                    target = "BAD CANDIDATE " + wire + extra
                else:
                    self.assertEqual(wire, "and")
                    target = "和"
                return _Response({"choices": [{"message": {"content": target}}]})
            with self.subTest(extra=extra), patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=respond):
                target = provider.translate([Segment("id-repeat", "SST and SST.")], self.context)[0].target
            self.assertEqual(target.count("SST"), 2)
            self.assertNotIn("BAD", target)
            self.assertNotIn("PLPROTECTED", target)
            self.assertEqual(len(calls), 2)

    def test_qwen_mt_rejects_markers_from_recovery_without_recursing(self) -> None:
        """恢复请求再次幻化标记时必须停止；不能吞掉异常标记或无限分段。"""
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret-key", model="qwen-mt-plus", min_request_interval_seconds=0)
        response = _Response({"choices": [{"message": {"content": "[PLPROTECTEDX9999oops]"}}]})
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", return_value=response) as call:
            with self.assertRaisesRegex(ValueError, "分段译文包含意外保护标记"):
                provider.translate([Segment("id-repeat", "SST and SST.")], self.context)
        self.assertEqual(call.call_count, 2)

    def test_qwen_mt_does_not_confuse_literal_source_markers(self) -> None:
        """原文本来包含同名标记时使用另一命名空间，字面内容不会被本地吞掉。"""
        source = "Literal [PLPROTECTED0001] and {v1}."
        def respond(request, timeout):
            wire = json.loads(request.data)["messages"][0]["content"]
            self.assertIn("[PLPROTECTEDX0001]", wire)
            return _Response({"choices": [{"message": {"content": "译文 " + wire}}]})
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret-key", model="qwen-mt-plus", min_request_interval_seconds=0)
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=respond):
            target = provider.translate([Segment("id-literal", source)], self.context)[0].target
        self.assertIn("Literal [PLPROTECTED0001]", " ".join(target.split()))
        self.assertIn("{v1}", target)

    def test_qwen_mt_overlapping_version_number_and_url_spans_stay_whole(self) -> None:
        """GLDAS-2.0和分辨率公式不可拆开，URL斜线前的版面空格不得暴露UUID。"""
        source = "GLDAS-2.0 at 0.{v1}.{v2} and 0.03{v3} resolution: https://example.org/data /9775f2b4-7370/."
        def respond(request, timeout):
            wire = json.loads(request.data)["messages"][0]["content"]
            self.assertNotIn("GLDAS", wire)
            self.assertNotIn("9775", wire)
            self.assertNotIn("{v1}", wire)
            self.assertEqual(len(re.findall(r"\[PLPROTECTED\d+\]", wire)), 4)
            return _Response({"choices": [{"message": {"content": "资料 " + wire}}]})
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret", model="qwen-mt-plus", min_request_interval_seconds=0)
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=respond):
            target = provider.translate([Segment("id-version", source)], self.context)[0].target
        self.assertIn("GLDAS-2.0", target)
        self.assertIn("0.{v1}.{v2}", target)
        self.assertIn("0.03{v3}", target)
        self.assertIn("https://example.org/data /9775f2b4-7370/.", target)

    def test_qwen_mt_retries_temporary_disconnect(self) -> None:
        """临时断连可短重试，但同一片段最终只返回一条译文。"""

        provider = QwenMTProvider(
            base_url="https://example.test/compatible-mode/v1",
            api_key="secret-key",
            model="qwen-mt-plus", min_request_interval_seconds=0,
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

    def test_qwen_rate_limit_retry_is_bounded_and_auth_is_not_retried(self) -> None:
        import io
        import urllib.error
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret", model="qwen-mt-plus", min_request_interval_seconds=0)
        def error(status, code):
            return urllib.error.HTTPError("https://example.test/v1", status, "rejected", {"Retry-After": "1"},
                io.BytesIO(json.dumps({"error": {"code": code}}).encode()))
        response = _Response({"choices": [{"message": {"content": "译文"}}]})
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=[error(429, "limit_requests"), response]) as call, patch("paperlocale.providers.qwen_mt.time.sleep") as sleep:
            self.assertEqual(provider._request_translation({}), "译文")
            self.assertEqual(call.call_count, 2)
            sleep.assert_called_once_with(1.0)
        for status, code, expected in [(429, "limit_requests", 2), (401, "invalid_api_key", 1)]:
            with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=[error(status, code), error(status, code)]) as call, patch("paperlocale.providers.qwen_mt.time.sleep"):
                with self.assertRaises(RuntimeError):
                    provider._request_translation({})
                self.assertEqual(call.call_count, expected)

    def test_qwen_requests_share_minimum_interval(self) -> None:
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret", model="qwen-mt-plus")
        response = _Response({"choices": [{"message": {"content": "译文"}}]})
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", return_value=response), patch("paperlocale.providers.qwen_mt.time.monotonic", return_value=10), patch("paperlocale.providers.qwen_mt.time.sleep") as sleep:
            provider._request_translation({})
            provider._request_translation({})
            sleep.assert_called_once()
            self.assertAlmostEqual(sleep.call_args.args[0], 1.1)

    def test_qwen_mt_restores_ascii_hyphen_in_abbreviation(self) -> None:
        """科学缩写由本地原样恢复，模型不再负责生成其中的 ASCII 连字符。"""

        provider = QwenMTProvider(
            base_url="https://example.test/compatible-mode/v1",
            api_key="secret-key",
            model="qwen-mt-plus", min_request_interval_seconds=0,
        )
        response = _Response({"choices": [{"message": {"content": "采用[PLPROTECTED0001]指数。"}}]})
        with patch(
            "paperlocale.providers.qwen_mt.urllib.request.urlopen",
            return_value=response,
        ):
            result = provider.translate(
                [Segment("id-hyphen", "The HDI-P index was used.")],
                self.context,
            )
        self.assertEqual(result[0].target, "采用HDI-P指数。")

    def test_qwen_mt_recovers_missing_marker_by_translating_source_gaps(self) -> None:
        """丢标记后不修改坏译文，只翻译原文中的间隙并按原位置插回本地标识。"""
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret-key", model="qwen-mt-plus", min_request_interval_seconds=0)
        calls = []
        def respond(request, timeout):
            content = json.loads(request.data)["messages"][0]["content"]
            calls.append(content)
            if len(calls) == 1:
                target = "BAD OMITTED TRANSLATION"
            else:
                self.assertEqual(content, "Standardized Precipitation Index (")
                target = "标准化降水指数（"
            return _Response({"choices": [{"message": {"content": target}}]})
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=respond):
            target = provider.translate([Segment("id-style", "Standardized Precipitation Index (SPI{v1}).")], self.context)[0].target
        self.assertEqual(len(calls), 2)
        self.assertIn("SPI{v1}", target)
        self.assertNotIn("BAD", target)

    def test_qwen_mt_repairs_year_glued_to_extra_digits(self) -> None:
        """标记数正确但还原后变成202015时，按原文间隙重译而非放宽数字门禁。"""
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret-key", model="qwen-mt-plus", min_request_interval_seconds=0)
        responses = [_Response({"choices": [{"message": {"content": "事件20[PLPROTECTED0001]年。"}}]}),
                     _Response({"choices": [{"message": {"content": "事件发生在"}}]})]
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=responses) as call:
            target = provider.translate([Segment("id-year", "The event occurred in 2015.")], self.context)[0].target
        self.assertIn("2015", target)
        self.assertNotIn("202015", target)
        self.assertEqual(call.call_count, 2)

    def test_qwen_quantity_allows_natural_unit_translation(self) -> None:
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret", model="qwen-mt-plus", min_request_interval_seconds=0)
        def respond(request, timeout):
            self.assertEqual(json.loads(request.data)["messages"][0]["content"], "Within 50 km of the station.")
            return _Response({"choices": [{"message": {"content": "在距站点50千米范围内。"}}]})
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=respond) as call:
            target = provider.translate([Segment("id-unit", "Within 50 km of the station.")], self.context)[0].target
        self.assertIn("50千米", target)
        self.assertEqual(call.call_count, 1)

    def test_qwen_recovers_whole_quantity_after_unit_omission(self) -> None:
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret", model="qwen-mt-plus", min_request_interval_seconds=0)
        calls = []
        def respond(request, timeout):
            content = json.loads(request.data)["messages"][0]["content"]
            calls.append(content)
            target = "在站点50范围内。" if len(calls) == 1 else {"Within": "在", "of the station.": "距站点范围内。"}[content]
            return _Response({"choices": [{"message": {"content": target}}]})
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", side_effect=respond):
            target = provider.translate([Segment("id-unit", "Within 50 km of the station.")], self.context)[0].target
        self.assertIn("50 km", target)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("km", ' '.join(calls[1:]))

    def test_qwen_quantity_recovery_still_rejects_empty_text(self) -> None:
        provider = QwenMTProvider(base_url="https://example.test/v1", api_key="secret", model="qwen-mt-plus", min_request_interval_seconds=0)
        with patch("paperlocale.providers.qwen_mt.urllib.request.urlopen", return_value=_Response({"choices": [{"message": {"content": ""}}]})) as call:
            with self.assertRaisesRegex(ValueError, "分段译文未通过门禁"):
                provider.translate([Segment("id-unit", "Within 50 km of the station.")], self.context)
        self.assertEqual(call.call_count, 2)

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
