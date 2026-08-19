"""片段翻译断点必须只复用通过门禁的结果。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paperlocale.contracts import (
    read_jsonl,
    segment_id,
    validate_translation_files,
    write_jsonl_atomic,
)
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

    def test_batch_failure_saves_valid_rows_and_rejected_candidate(self) -> None:
        """单条失败不能迫使同批其他合格译文再次调用高成本模型。"""

        sources = (
            "Soil moisture was 10 mm.",
            "Air temperature was 20 °C.",
            "Precipitation was 30 mm.",
        )
        mapping = {
            sources[0]: "土壤湿度为10 mm。",
            sources[1]: "气温发生变化。",
            sources[2]: "降水量为30 mm。",
        }
        provider = _MappingProvider(mapping)
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            write_jsonl_atomic(
                segments,
                [{"id": segment_id(source), "source": source} for source in sources],
            )

            with self.assertRaisesRegex(ValueError, "合格译文已保存"):
                translate_segment_file(
                    segments_path=segments,
                    translations_path=translations,
                    provider=provider,
                    domain=domain,
                )

            accepted = read_jsonl(translations)
            rejected_path = root / "rejected_translations.jsonl"
            rejected = read_jsonl(rejected_path)
            self.assertEqual([row["source"] for row in accepted], [sources[0], sources[2]])
            self.assertEqual(rejected[0]["source"], sources[1])
            self.assertTrue(rejected[0]["errors"])

            provider.mapping[sources[1]] = "气温为20 °C。"
            self.assertEqual(
                translate_segment_file(
                    segments_path=segments,
                    translations_path=translations,
                    provider=provider,
                    domain=domain,
                ),
                (2, 1),
            )
            self.assertEqual(len(read_jsonl(translations)), 3)
            self.assertFalse(rejected_path.exists())

    def test_preserve_policy_skips_provider_and_body_glossary_for_references(self) -> None:
        """参考文献原样写入，正文仍走模型和领域术语门禁。"""

        body = "Soil moisture was 10 mm."
        reference = (
            "Smith, A., 2020: Soil moisture observations in a dry region. "
            "J. Hydrol., 12, 10–20, https://doi.org/10.1000/example."
        )
        provider = _MappingProvider({body: "土壤湿度为10 mm。"})
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            write_jsonl_atomic(
                segments,
                [
                    {"id": segment_id(reference), "source": reference},
                    {"id": segment_id(body), "source": body},
                ],
            )
            result = translate_segment_file(
                segments_path=segments,
                translations_path=translations,
                provider=provider,
                domain=domain,
                reference_segment_ids={segment_id(reference)},
                reference_policy="preserve",
            )
            self.assertEqual(result, (0, 2))
            self.assertEqual(provider.calls, 1)
            rows = {row["id"]: row for row in read_jsonl(translations)}
            self.assertEqual(rows[segment_id(reference)]["target"], reference)
            validate_translation_files(
                segments,
                translations,
                domain,
                reference_segment_ids={segment_id(reference)},
                reference_policy="preserve",
            )

    def test_translate_titles_skips_body_domain_gate_for_reference_rows(self) -> None:
        """标题翻译保留书目信息，但参考文献不强制使用正文术语译法。"""

        reference = (
            "Smith, A., 2020: Gross primary productivity under drought. "
            "J. Climate, 12, 10–20, https://doi.org/10.1000/example."
        )
        target = (
            "Smith, A., 2020: 干旱条件下的生产力研究. "
            "J. Climate, 12, 10–20, https://doi.org/10.1000/example."
        )
        provider = _MappingProvider({reference: target})
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            write_jsonl_atomic(
                segments,
                [{"id": segment_id(reference), "source": reference}],
            )
            translate_segment_file(
                segments_path=segments,
                translations_path=translations,
                provider=provider,
                domain=domain,
                reference_segment_ids={segment_id(reference)},
                reference_policy="translate-titles",
            )
            self.assertEqual(read_jsonl(translations)[0]["target"], target)
            validate_translation_files(
                segments,
                translations,
                domain,
                reference_segment_ids={segment_id(reference)},
                reference_policy="translate-titles",
            )


if __name__ == "__main__":
    unittest.main()
