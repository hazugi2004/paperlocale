"""片段翻译断点必须只复用通过门禁的结果。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paperlocale.contracts import read_jsonl, segment_id, write_jsonl_atomic
from paperlocale.domains import load_domain_pack
from paperlocale.pipeline import make_batches, translate_segment_file
from paperlocale.providers import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
)


class _MappingProvider(TranslationProvider):
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls = 0

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        self.calls += 1
        return [Translation(segment.id, self.mapping[segment.source]) for segment in segments]


class PipelineTest(unittest.TestCase):
    def test_batches_respect_character_limit(self) -> None:
        segments = [Segment(str(index), "x" * 10) for index in range(5)]
        batches = make_batches(segments, max_segments=10, max_characters=25)
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])

    def test_resume_does_not_call_provider_twice(self) -> None:
        source = "Soil moisture was 10 mm."
        target = "土壤湿度为10 mm。"
        provider = _MappingProvider({source: target})
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            write_jsonl_atomic(segments, [{"id": segment_id(source), "source": source}])
            first = translate_segment_file(
                segments_path=segments,
                translations_path=translations,
                provider=provider,
                domain=domain,
            )
            second = translate_segment_file(
                segments_path=segments,
                translations_path=translations,
                provider=provider,
                domain=domain,
            )
            self.assertEqual(first, (0, 1))
            self.assertEqual(second, (1, 0))
            self.assertEqual(provider.calls, 1)
            self.assertEqual(len(read_jsonl(translations)), 1)


if __name__ == "__main__":
    unittest.main()
