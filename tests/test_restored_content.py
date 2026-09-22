"""缓存前检查完整锚点、源标点与明显缺译；适用于所有 Provider。"""
import tempfile
import unittest
from pathlib import Path
from paperlocale.pipeline import restored_content_errors, translate_segment_file
from paperlocale.contracts import write_jsonl_atomic, read_jsonl, segment_id
from paperlocale.domains import load_domain_pack
from paperlocale.providers import Translation, TranslationProvider

class Mapping(TranslationProvider):
    def translate(self, segments, context):
        return [Translation(s.id,'结果({v0}。') for s in segments]

class RestoredContentTest(unittest.TestCase):
    def test_parentheses_and_source_anomaly(self):
        self.assertTrue(restored_content_errors('Results ({v0}.','结果({v0})。',{'{v0}':'Wu, 2021)'}))
        self.assertEqual(restored_content_errors('Conditions)).','状况))。',{}),[])
        self.assertTrue(restored_content_errors('Conditions)).','状况)。',{}))
        self.assertTrue(restored_content_errors('During summer.','在……期间。',{}))

    def test_invalid_cached_anchors_are_archived_then_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source='Results ({v0}.';sid=segment_id(source)
            write_jsonl_atomic(root/'s.jsonl',[{'id':sid,'source':source}])
            write_jsonl_atomic(root/'t.jsonl',[{'id':sid,'source':source,'target':'结果({v0})。'}])
            result=translate_segment_file(segments_path=root/'s.jsonl',translations_path=root/'t.jsonl',
                provider=Mapping(),domain=load_domain_pack('atmospheric-science'),
                anchor_text={sid:{'{v0}':'Wu, 2021)'}})
            self.assertEqual(result,(0,1))
            self.assertEqual(read_jsonl(root/'t.jsonl')[0]['target'],'结果({v0}。')
            self.assertTrue((root/'quantity_cache_rejections.jsonl').is_file())

    def test_completed_qwen_rate_retry_is_not_repeated_by_outer_loop(self):
        from paperlocale.providers.qwen_mt import QwenRequestError
        from paperlocale.recovery import _transient
        from urllib.error import HTTPError
        error=QwenRequestError('HTTP 429')
        error.__cause__=HTTPError('https://example.test',429,'quota',{},None)
        self.assertFalse(_transient(error))
