"""从真实翻译门禁一路验证到 CLI/弹窗 JSON；不请求模型、不读取私人论文。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from paperlocale.contracts import segment_id, validate_translation, write_jsonl_atomic
from paperlocale.diagnostics import LocatedError, print_context, record_error
from paperlocale.domains import load_domain_pack
from paperlocale.pipeline import translate_segment_file
from paperlocale.providers import Translation, TranslationProvider


class TranslationDiagnosticsTests(unittest.TestCase):
    def test_current_candidate_rules_survive_location_enrichment_and_retry(self):
        # 首轮还漏数字，重试修好数字后只剩缩写；弹窗不能拼入旧失败原因。
        source = 'CDHE occurs in 1980. CDHE intensity increases.'
        class Provider(TranslationProvider):
            def translate(self, segments, context):
                target = 'CDHE强度增加。' if not context.repair_feedback else '1980年CDHE强度增加。'
                return [Translation(s.id, target) for s in segments]
        for retry in (False, True):
            with self.subTest(retry=retry), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sid = segment_id(source)
                write_jsonl_atomic(root/'segments.jsonl', [{'id': sid, 'source': source}])
                (root/'source_locations.json').write_text(json.dumps([
                    {'id': sid, 'source': source, 'pages': [2], 'regions': []}]))
                with self.assertRaises(LocatedError) as caught:
                    translate_segment_file(segments_path=root/'segments.jsonl',
                        translations_path=root/'translations.jsonl', provider=Provider(),
                        domain=load_domain_pack('ecology'), contract_repair=retry)
                report = record_error(caught.exception, root)
                item = report['items'][0]
                self.assertEqual(item['id'], sid)
                self.assertEqual(item['pages'], [2])
                self.assertTrue(any("原文要求 {'CDHE': 2}；译文实际 {'CDHE': 1}" in e for e in item['errors']))
                self.assertEqual(any(e.startswith('number ') for e in item['errors']), not retry)
                self.assertEqual('1980' in item['target'], retry)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    print_context(report)
                self.assertIn('违反规则与差异：abbreviation', stderr.getvalue())
                self.assertIn(item['target'], stderr.getvalue())
                self.assertEqual(json.loads((root/'error_report.json').read_text())['items'], report['items'])

    def test_cached_failure_has_target_and_rules_without_request(self):
        class NoRequest(TranslationProvider):
            def translate(self, segments, context):
                raise AssertionError('失效缓存诊断不得请求模型')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = 'NDVI was 2.'
            row = {'id': segment_id(source), 'source': source, 'target': '数值为2。'}
            write_jsonl_atomic(root/'segments.jsonl', [{'id': row['id'], 'source': source}])
            write_jsonl_atomic(root/'translations.jsonl', [row])
            with self.assertRaises(LocatedError) as caught:
                translate_segment_file(segments_path=root/'segments.jsonl',
                    translations_path=root/'translations.jsonl', provider=NoRequest(),
                    domain=load_domain_pack('ecology'))
            item = record_error(caught.exception, root)['items'][0]
            self.assertEqual(item['target'], row['target'])
            self.assertIn('NDVI', item['errors'][0])

    def test_anchor_form_is_preserved_alongside_readable_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = 'See {v0} and {v1}.', '见{v0}。'
            row = {'id': 'anchor', 'source': source, 'target': target,
                   'errors': validate_translation(source, target)}
            (root/'source_locations.json').write_text(json.dumps([
                {'id': 'anchor', 'source': 'See Fig. 1 and Fig. 2.', 'pages': [3]}]))
            item = record_error(LocatedError('failed', [row], 'translation'), root)['items'][0]
            self.assertEqual(item['source'], 'See Fig. 1 and Fig. 2.')
            self.assertEqual(item['validation_source'], source)
            self.assertEqual(item['target'], target)
            self.assertIn('{v1}', item['errors'][0])

    def test_unrelated_error_does_not_load_stale_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl_atomic(root/'rejected_translations.jsonl',
                [{'id': 'stale', 'source': 'old', 'target': '旧译文', 'errors': ['old']}])
            self.assertEqual(record_error(ValueError('network error'), root)['items'], [])

    def test_anchor_order_and_restored_brackets_show_actual_difference(self):
        from paperlocale.source_layout import anchor_errors
        unit = {'source': 'See {v0} and {v1}.', 'anchors': [{'text': '(1)'}, {'text': '(2)'}]}
        order = anchor_errors(unit, '见{v1}和{v0}。')[0]
        self.assertIn("原文要求 ['{v0}', '{v1}']；译文实际 ['{v1}', '{v0}']", order)
        brackets = anchor_errors(unit, '见{v0}）和{v1}。')[0]
        self.assertIn("原文括号序列 '()()'；译文括号序列 '()）()'", brackets)
        self.assertEqual(anchor_errors(unit, '见{v0}和{v1}。'), [])
