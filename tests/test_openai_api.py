"""Official API tests use only fake credentials and mocked HTTP, never a paid call."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from paperlocale import cli
from paperlocale.cli import (
    _apply_run_provider_defaults,
    _provider_from_args,
    build_parser,
)
from paperlocale.contracts import segment_id
from paperlocale.domains import load_domain_pack
from paperlocale.error_categories import classify
from paperlocale.providers import OpenAIAPIProvider, Segment, TranslationContext
from paperlocale.providers.openai_api import _NoRedirect, _responses_opener
from paperlocale.recovery import _transient


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()


def _completed(translations):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "status": "completed", "role": "assistant", "content": [
            {"type": "output_text", "text": json.dumps({"translations": translations})},
        ]},
    ]}


class OpenAIAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TranslationContext("en", "zh-CN", load_domain_pack("atmospheric-science"))
        source = "Soil moisture was 10 mm."
        cls.segments = [Segment(segment_id(source), source)]
        cls.translation = {"s1": "土壤湿度为10 mm。"}
        cls.fake_key = "unit-test-fake-key-only"

    def provider(self):
        return OpenAIAPIProvider(api_key=self.fake_key, model="explicit-test-model", timeout_seconds=7)

    def mock_http(self, payload):
        opener = Mock()
        opener.open.return_value = _Response(payload)
        return patch("paperlocale.providers.openai_api.urllib.request.build_opener", return_value=opener), opener

    def test_request_uses_fixed_responses_schema_and_no_key_in_body_or_metadata(self):
        provider = self.provider()
        http, opener = self.mock_http(_completed(self.translation))
        with http:
            result = provider.translate(self.segments, self.context)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 7})
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.fake_key)
        body = json.loads(request.data)
        self.assertEqual(body["model"], "explicit-test-model")
        self.assertFalse(body["store"])
        self.assertNotIn("temperature", body)
        self.assertEqual(body["text"]["format"], {
            "type": "json_schema", "name": "paperlocale_translations", "strict": True,
            "schema": {
                "type": "object",
                "properties": {"translations": {
                    "type": "object", "properties": {"s1": {"type": "string"}},
                    "required": ["s1"], "additionalProperties": False,
                }},
                "required": ["translations"], "additionalProperties": False,
            },
        })
        self.assertIn("待翻译 JSON", body["input"])
        self.assertNotIn(self.segments[0].id, request.data.decode())
        self.assertNotIn(self.fake_key, request.data.decode())
        self.assertNotIn(self.fake_key, json.dumps(provider.provenance()))
        self.assertEqual(provider.provenance()["provider"], "openai-api")
        self.assertEqual((result[0].id, result[0].target), (self.segments[0].id, self.translation["s1"]))

    def test_reasoning_is_requested_only_for_known_model_repair(self):
        repair = replace(self.context, repair_feedback={self.segments[0].id: ("土壤湿度为20 mm。", ("Wrong value",))})
        http, opener = self.mock_http(_completed(self.translation))
        with http:
            provider = OpenAIAPIProvider(api_key=self.fake_key, model="gpt-5.4")
            provider.translate(self.segments, self.context)
            provider.translate(self.segments, repair)
            self.provider().translate(self.segments, repair)
        initial, gpt54_repair, other_repair = [json.loads(call.args[0].data) for call in opener.open.call_args_list]
        self.assertNotIn("reasoning", initial)
        self.assertEqual(gpt54_repair["reasoning"], {"effort": "low"})
        self.assertNotIn("reasoning", other_repair)

    def test_response_keys_restore_original_ids_and_follow_remapped_context(self):
        segments = [Segment(segment_id(source), source) for source in ("One.", "Two {v0}.")]
        second_id = segments[1].id
        context = replace(self.context, reference_segment_ids=frozenset({second_id}),
                          repair_feedback={second_id: ("Previous target", ("Wrong value",))},
                          anchor_text={second_id: {"{v0}": "10 mm"}})
        http, opener = self.mock_http(_completed({"s2": "二{v0}。", "s1": "一。"}))
        with http:
            translations = self.provider().translate(segments, context)
        self.assertEqual([(row.id, row.target) for row in translations],
                         [(segments[0].id, "一。"), (second_id, "二{v0}。")])
        body = json.loads(opener.open.call_args.args[0].data)
        schema = body["text"]["format"]["schema"]["properties"]["translations"]
        self.assertEqual(schema["required"], ["s1", "s2"])
        rows = json.JSONDecoder().raw_decode(body["input"].split("待翻译 JSON：\n", 1)[1])[0]
        self.assertEqual([row["id"] for row in rows], ["s1", "s2"])
        self.assertNotIn("previous_target", rows[0])
        self.assertEqual(rows[1]["kind"], "reference")
        self.assertEqual(rows[1]["previous_target"], "Previous target")
        self.assertEqual(rows[1]["fixed_anchor_text"], {"{v0}": "10 mm"})

    def test_id_and_value_contract_rejects_invalid_results_without_echoing_response(self):
        bodies = [json.dumps({"translations": rows}) for rows in (
            {}, {"s1": "一。", self.fake_key: "unexpected"}, {"s1": None}, [{"id": "s1", "target": "一。"}],
        )]
        bodies.append('{"translations":{"s1":"一。","s1":"二。"}}')
        for text in bodies:
            with self.subTest(text=text):
                payload = _completed(self.translation)
                payload["output"][1]["content"][0]["text"] = text
                http, opener = self.mock_http(payload)
                with http, self.assertRaisesRegex(ValueError, "IDs or value types") as raised:
                    self.provider().translate(self.segments, self.context)
                self.assertNotIn(self.fake_key, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                opener.open.assert_called_once()

    def test_refusal_is_explicit_and_not_echoed(self):
        payload = _completed(self.translation)
        payload["output"][1]["content"] = [{"type": "refusal", "refusal": self.fake_key}]
        http, opener = self.mock_http(payload)
        with http, self.assertRaisesRegex(ValueError, "refused") as raised:
            self.provider().translate(self.segments, self.context)
        self.assertNotIn(self.fake_key, str(raised.exception))
        opener.open.assert_called_once()

    def test_incomplete_failed_or_missing_status_cannot_accept_partial_json(self):
        for status in ["incomplete", "failed", "queued", "in_progress", None]:
            payload = _completed(self.translation)
            payload["status"] = status
            http, opener = self.mock_http(payload)
            with http, self.assertRaisesRegex(ValueError, "did not complete"):
                self.provider().translate(self.segments, self.context)
            opener.open.assert_called_once()

    def test_output_limit_guides_smaller_batches_without_retry(self):
        payload = _completed(self.translation)
        payload.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        http, opener = self.mock_http(payload)
        with http, self.assertRaisesRegex(ValueError, "smaller batch"):
            self.provider().translate(self.segments, self.context)
        opener.open.assert_called_once()

    def test_malformed_envelopes_json_and_missing_output_are_rejected(self):
        payloads = [b"not-json", b"\xff", [], {"status": "completed"},
                    {"status": "completed", "output": []},
                    {"status": "completed", "output": [None]},
                    {"status": "completed", "output": [{"type": "message", "content": "bad"}]},
                    {"status": "completed", "output": [{"type": "message", "status": "incomplete"}]},
                    {"status": "completed", "output": [{"type": "message", "content": [None]}]},
                    {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "{"}]}]},
                    {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": 3}]}]}]
        for payload in payloads:
            with self.subTest(payload=payload):
                http, opener = self.mock_http(payload)
                with http, self.assertRaises((ValueError, TypeError)):
                    self.provider().translate(self.segments, self.context)
                opener.open.assert_called_once()

    def test_http_failures_do_not_leak_key_retry_or_fall_back(self):
        for status in [302, 400, 401, 403, 404, 422, 429, 500, 503]:
            with self.subTest(status=status):
                request_id = "req_" + ("4" * 32) if status == 429 else self.fake_key
                error = urllib.error.HTTPError(OpenAIAPIProvider.endpoint, status, self.fake_key,
                    {"x-request-id": request_id}, io.BytesIO(self.fake_key.encode()))
                opener = Mock()
                opener.open.side_effect = error
                with patch("paperlocale.providers.openai_api.urllib.request.build_opener", return_value=opener), \
                     self.assertRaisesRegex(RuntimeError, "HTTP " + str(status)) as raised:
                    self.provider().translate(self.segments, self.context)
                self.assertNotIn(self.fake_key, str(raised.exception))
                if status == 429:
                    self.assertIn(request_id, str(raised.exception))
                    self.assertEqual(classify(raised.exception, "provider")["code"], "service.quota")
                elif status in {401, 403}:
                    self.assertEqual(classify(raised.exception, "provider")["code"], "service.authentication")
                elif status == 404:
                    self.assertIn("新建运行", classify(raised.exception, "provider")["guidance"])
                self.assertIsNone(raised.exception.__cause__)
                self.assertFalse(_transient(raised.exception))
                opener.open.assert_called_once()

    def test_connection_errors_have_no_automatic_recovery_or_raw_reason(self):
        for error in [urllib.error.URLError(self.fake_key), TimeoutError(self.fake_key), ConnectionResetError(self.fake_key)]:
            opener = Mock()
            opener.open.side_effect = error
            with patch("paperlocale.providers.openai_api.urllib.request.build_opener", return_value=opener), \
                 self.assertRaisesRegex(RuntimeError, "connection failed or timed out") as raised:
                self.provider().translate(self.segments, self.context)
            self.assertNotIn(self.fake_key, str(raised.exception))
            self.assertFalse(_transient(raised.exception))
            opener.open.assert_called_once()

    def test_redirect_handler_never_forwards_request(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.test"))

    def test_macos_uses_system_proxy_for_https_when_only_wss_is_in_environment(self):
        module = "paperlocale.providers.openai_api"
        system = {"https": "http://system-proxy.example:1234"}
        for environment, bypass, use_system in (
            ({"wss": "http://only-wss.example"}, False, True),
            ({"wss": "http://only-wss.example"}, True, False),
            ({"wss": "http://only-wss.example", "https": "http://explicit.example"}, False, False),
            ({"wss": "http://only-wss.example", "no": "api.openai.com"}, False, False),
        ):
            with self.subTest(environment=environment, bypass=bypass), \
                 patch(f"{module}.sys.platform", "darwin"), \
                 patch(f"{module}.urllib.request.getproxies_environment", return_value=environment), \
                 patch(f"{module}.urllib.request.getproxies_macosx_sysconf", return_value=system, create=True), \
                 patch(f"{module}.urllib.request.proxy_bypass_macosx_sysconf", return_value=bypass, create=True), \
                 patch(f"{module}.urllib.request.build_opener") as build:
                _responses_opener()
                handlers = build.call_args.args
                self.assertIsInstance(handlers[-1], _NoRedirect)
                if use_system:
                    self.assertEqual(handlers[0].proxies, system)
                else:
                    self.assertEqual(len(handlers), 1)

    def test_constructor_rejects_empty_credentials_and_model_without_network(self):
        with patch("paperlocale.providers.openai_api.urllib.request.build_opener") as network:
            for key, model in [("", "model"), ("a\nb", "model"), ("密钥", "model"),
                               ("a\0b", "model"), (self.fake_key, ""), (self.fake_key, " ")]:
                with self.assertRaises(ValueError):
                    OpenAIAPIProvider(api_key=key, model=model)
            network.assert_not_called()

    def test_cli_accepts_official_provider_on_all_translation_commands(self):
        commands = [["run", "paper.pdf", "--run-dir", "run"], ["translate", "--run-dir", "run"],
                    ["translate-segments", "--segments", "s.jsonl", "--translations", "t.jsonl"],
                    ["provider-eval", "--output", "eval.json"]]
        for command in commands:
            args = build_parser().parse_args(command + ["--provider", "openai-api", "--model", "test-model"])
            with patch.dict("os.environ", {"OPENAI_API_KEY": self.fake_key, "PAPERLOCALE_API_KEY": "other-fake-key"}, clear=True):
                provider = _provider_from_args(args)
            self.assertIsInstance(provider, OpenAIAPIProvider)
            self.assertEqual(provider._api_key, self.fake_key)

    def test_cli_explicit_key_variable_and_compatible_default_are_preserved(self):
        args = build_parser().parse_args(["run", "paper.pdf", "--provider", "openai-api", "--model", "test",
            "--api-key-env", "TEST_ONLY_KEY"])
        with patch.dict("os.environ", {"TEST_ONLY_KEY": self.fake_key}, clear=True):
            self.assertEqual(_provider_from_args(args)._api_key, self.fake_key)
        args = build_parser().parse_args(["run", "paper.pdf", "--provider", "openai-compatible", "--model", "test",
            "--base-url", "https://example.test/v1"])
        with patch.dict("os.environ", {"PAPERLOCALE_API_KEY": self.fake_key}, clear=True):
            self.assertEqual(_provider_from_args(args).api_key, self.fake_key)

    def test_cli_rejects_missing_model_key_and_incompatible_options(self):
        for extra, expected in [([], "requires --model"), (["--model", "test"], "OPENAI_API_KEY"),
                (["--model", "test", "--base-url", "https://example.test"], "fixed official endpoint"),
                (["--model", "test", "--api-key-csv", "must-not-read.csv"], "qwen-mt"),
                (["--model", "test", "--reasoning-effort", "high"], "codex-local")]:
            args = build_parser().parse_args(["run", "paper.pdf", "--provider", "openai-api"] + extra)
            with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(ValueError, expected):
                _provider_from_args(args)
        args = build_parser().parse_args(["run", "paper.pdf", "--provider", "openai-api",
            "--model", "test", "--api-key-env", "CREDENTIAL"])
        for value in ("", "two words"):
            with patch.dict("os.environ", {"CREDENTIAL": value}, clear=True), self.assertRaises(ValueError) as raised:
                _provider_from_args(args)
            self.assertEqual(classify(raised.exception, "operation")["code"], "service.authentication")

    def test_fresh_default_is_still_codex_but_api_resume_restores_recorded_identity(self):
        args = build_parser().parse_args(["run", "paper.pdf"])
        _apply_run_provider_defaults(args, {"status": "initialized"}, True)
        self.assertEqual(args.provider, "codex-local")
        args = build_parser().parse_args(["run", "paper.pdf"])
        _apply_run_provider_defaults(args, {"status": "collected", "translation_provider": {
            "provider": "openai-api", "model": "recorded-model"}}, False)
        self.assertEqual((args.provider, args.model), ("openai-api", "recorded-model"))
        args.model = "explicit-model"
        _apply_run_provider_defaults(args, {"status": "collected", "translation_provider": {
            "provider": "openai-api", "model": "recorded-model"}}, False)
        self.assertEqual(args.model, "explicit-model")

    def test_rendered_api_resume_needs_no_key_or_provider_call(self):
        args = build_parser().parse_args(["run", "paper.pdf", "--run-dir", "run", "--no-qa", "--no-export"])
        manifest = {"status": "rendered", "layout_mode": "paragraph", "rendered_pdf": "candidate.pdf",
            "translation_provider": {"provider": "openai-api", "model": "recorded-model"}}
        with patch.dict("os.environ", {}, clear=True), \
             patch("paperlocale.cli._initialize_or_load_run", return_value=manifest), \
             patch("paperlocale.workflow.save_manifest"), \
             patch("paperlocale.preserved_workflow.run_preserved", return_value=manifest) as run, \
             patch("paperlocale.workflow._verify_rendered_pdf", return_value=Path("candidate.pdf")), \
             patch("paperlocale.cli._provider_from_args") as provider, patch("builtins.print"):
            self.assertEqual(cli._execute(args), 0)
        provider.assert_not_called()
        self.assertIsNone(run.call_args.kwargs["provider"])
        self.assertEqual((args.provider, args.model), ("openai-api", "recorded-model"))

    def test_mocked_cli_translation_runs_shared_gates_and_writes_no_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = "Soil moisture was 10 mm."
            sid = segment_id(source)
            segments = root / "segments.jsonl"
            target = root / "translations.jsonl"
            segments.write_text(json.dumps({"id": sid, "source": source}) + "\n")
            argv = ["paperlocale", "translate-segments", "--segments", str(segments),
                    "--translations", str(target), "--provider", "openai-api", "--model", "test-model"]
            http, opener = self.mock_http(_completed({"s1": "土壤湿度为10 mm。"}))
            with http, patch.dict("os.environ", {"OPENAI_API_KEY": self.fake_key}, clear=True), \
                 patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(json.loads(target.read_text())["target"], "土壤湿度为10 mm。")
            for file in root.iterdir():
                self.assertNotIn(self.fake_key, file.read_text())
            opener.open.assert_called_once()

    def test_mocked_invalid_translation_cannot_bypass_content_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = "Soil moisture was 10 mm."
            sid = segment_id(source)
            segments = root / "segments.jsonl"
            target = root / "translations.jsonl"
            segments.write_text(json.dumps({"id": sid, "source": source}) + "\n")
            args = build_parser().parse_args(["translate-segments", "--segments", str(segments),
                "--translations", str(target), "--provider", "openai-api", "--model", "test-model", "--no-contract-repair"])
            http, opener = self.mock_http(_completed({"s1": "土壤湿度为11 mm。"}))
            with http, patch.dict("os.environ", {"OPENAI_API_KEY": self.fake_key}, clear=True), self.assertRaises(ValueError):
                cli._execute(args)
            self.assertFalse(target.exists())
            opener.open.assert_called_once()

    def test_cli_keeps_first_batch_when_service_fails_and_resumes_pending_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = ["Soil moisture was 10 mm.", "Soil moisture was 20 mm."]
            ids = [segment_id(source) for source in sources]
            segments = root / "segments.jsonl"
            target = root / "translations.jsonl"
            segments.write_text("".join(json.dumps({"id": sid, "source": source}) + "\n"
                                        for sid, source in zip(ids, sources)))
            argv = ["paperlocale", "translate-segments", "--segments", str(segments),
                    "--translations", str(target), "--provider", "openai-api", "--model", "test-model",
                    "--max-segments", "1"]
            first, second = [_Response(_completed({"s1": f"土壤湿度为{mm} mm。"})) for mm in (10, 20)]
            http, opener = self.mock_http(None)
            opener.open.side_effect = [first,
                urllib.error.HTTPError(OpenAIAPIProvider.endpoint, 429, self.fake_key, {}, io.BytesIO()),
                second]
            stderr = io.StringIO()
            with http, patch.dict("os.environ", {"OPENAI_API_KEY": self.fake_key}, clear=True), \
                 patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(cli.main(), 1)
                self.assertEqual(opener.open.call_count, 2)
                self.assertEqual(json.loads(target.read_text())["id"], ids[0])
                self.assertEqual(cli.main(), 0)
            self.assertEqual(opener.open.call_count, 3)
            self.assertEqual([json.loads(line)["id"] for line in target.read_text().splitlines()], ids)
            self.assertNotIn(self.fake_key, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
