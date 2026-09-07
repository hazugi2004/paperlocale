"""验证0.5.1的短键传输合同；不联网，不用模糊匹配挽救错误响应。"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from paperlocale.contracts import segment_id, write_jsonl_atomic
from paperlocale.domains import load_domain_pack
from paperlocale.pipeline import translate_segment_file
from paperlocale.providers import CodexLocalProvider, Segment, TranslationContext
from paperlocale.providers.codex_local import _keyed_request, _parse_keyed_response


class CodexResponseKeysTest(unittest.TestCase):
    def setUp(self):
        self.domain = load_domain_pack("atmospheric-science")
        self.context = TranslationContext("en", "zh-CN", self.domain)
        # 第一个ID就是实际故障中被模型漏抄字符的完整哈希。
        self.segments = [
            Segment("05d5d9aeaf228599ccbb66424513f5110d8e13042a7a840e6e13f3407a08fd7c", "First source."),
            Segment("a" * 64, "Second source."),
        ]

    def test_hashes_are_local_and_all_short_keys_are_required(self):
        prompt, schema = _keyed_request(self.segments, self.context)
        for segment in self.segments:
            self.assertNotIn(segment.id, prompt)
            self.assertNotIn(segment.id, json.dumps(schema))
        fields = schema["properties"]["translations"]
        self.assertEqual(fields["properties"], {"s1": {"type": "string"}, "s2": {"type": "string"}})
        self.assertEqual(fields["required"], ["s1", "s2"])
        self.assertFalse(fields["additionalProperties"])

    def test_reordered_response_maps_by_key_not_response_order(self):
        result = _parse_keyed_response('{"translations":{"s2":"第二条","s1":"第一条"}}', self.segments)
        self.assertEqual([(x.id, x.target) for x in result],
                         [(self.segments[0].id, "第一条"), (self.segments[1].id, "第二条")])

    def test_malformed_keys_and_values_are_not_guessed(self):
        invalid = [
            '{"translations":{"s1":"a"}}',
            '{"translations":{"s1":"a","s3":"b"}}',
            '{"translations":{"s1":"a","s2":null}}',
            '{"translations":{"s1":"a","s2":"b"},"extra":1}',
            '{"translations":[{"id":"05d5d9aeaf228599cc66424513f5110d8e13042a7a840e6e13f3407a08fd7c","target":"a"}]}',
            '{"translations":',
        ]
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                _parse_keyed_response(response, self.segments)

    def test_duplicate_json_keys_are_rejected_before_overwrite(self):
        for response in [
            '{"translations":{"s1":"wrong","s1":"a","s2":"b"}}',
            '{"translations":{},"translations":{"s1":"a","s2":"b"}}',
        ]:
            with self.assertRaisesRegex(ValueError, "重复键"):
                _parse_keyed_response(response, self.segments)

    def test_reference_and_repair_feedback_follow_local_aliases(self):
        sid = self.segments[1].id
        context = replace(self.context, reference_segment_ids=frozenset([sid]),
                          repair_feedback={sid: ("previous candidate", ("missing marker",))})
        prompt, _schema = _keyed_request(self.segments, context)
        rows = json.JSONDecoder().raw_decode(prompt.split("待翻译 JSON：\n", 1)[1])[0]
        self.assertEqual(rows[1]["id"], "s2")
        self.assertEqual(rows[1]["kind"], "reference")
        self.assertEqual(rows[1]["previous_target"], "previous candidate")
        self.assertEqual(rows[1]["validation_errors"], ["missing marker"])
        self.assertEqual(context.reference_segment_ids, frozenset([sid]))

    def test_empty_or_duplicate_input_is_rejected_before_model_call(self):
        for segments in [[], [self.segments[0], self.segments[0]]]:
            with self.assertRaises(ValueError):
                _keyed_request(segments, self.context)

    def test_persisted_cache_keeps_original_hash_and_resumes_without_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = "Soil moisture was 10 mm."
            sid = segment_id(source)
            write_jsonl_atomic(root / "segments.jsonl", [{"id": sid, "source": source}])

            def fake_run(command, **kwargs):
                schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
                self.assertEqual(schema["properties"]["translations"]["required"], ["s1"])
                Path(command[command.index("--output-last-message") + 1]).write_text(
                    json.dumps({"translations": {"s1": "土壤湿度为10 mm。"}}, ensure_ascii=False))
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            arguments = dict(segments_path=root / "segments.jsonl", translations_path=root / "translations.jsonl",
                             provider=CodexLocalProvider(codex_bin="/fake/codex"), domain=self.domain)
            with patch("paperlocale.providers.codex_local.subprocess.run", side_effect=fake_run) as run:
                self.assertEqual(translate_segment_file(**arguments), (0, 1))
                self.assertEqual(translate_segment_file(**arguments), (1, 0))
                self.assertEqual(run.call_count, 1)
            self.assertEqual(json.loads((root / "translations.jsonl").read_text())["id"], sid)
