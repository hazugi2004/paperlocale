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


class _SingleSegmentProvider(_MappingProvider):
    """模拟每次只允许一条消息的专用翻译模型。"""

    max_batch_segments = 1

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        if len(segments) != 1:
            raise AssertionError("流水线未遵守 Provider 的单片段上限")
        return super().translate(segments, context)


class _RepairingProvider(TranslationProvider):
    """首轮返回坏候选，收到同一 Provider 修复反馈后返回合格译文。"""

    def __init__(self, source: str, initial: str, repaired: str) -> None:
        self.source = source
        self.initial = initial
        self.repaired = repaired
        self.contexts: list[TranslationContext] = []

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        self.contexts.append(context)
        target = self.repaired if context.repair_feedback else self.initial
        return [Translation(segment.id, target) for segment in segments]


class PipelineTest(unittest.TestCase):
    def test_batches_respect_character_limit(self) -> None:
        segments = [Segment(str(index), "x" * 10) for index in range(5)]
        batches = make_batches(segments, max_segments=10, max_characters=25)
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])

    def test_provider_batch_limit_overrides_user_batch_size(self) -> None:
        """用户给出的大批次不能越过 Provider 自身的更严上限。"""

        sources = (
            "Soil moisture was 10 mm.",
            "Air temperature was 20 °C.",
        )
        provider = _SingleSegmentProvider(
            {
                sources[0]: "土壤湿度为10 mm。",
                sources[1]: "气温为20 °C。",
            }
        )
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            write_jsonl_atomic(
                segments,
                [{"id": segment_id(source), "source": source} for source in sources],
            )
            self.assertEqual(
                translate_segment_file(
                    segments_path=segments,
                    translations_path=translations,
                    provider=provider,
                    domain=domain,
                    max_segments=200,
                ),
                (0, 2),
            )
            self.assertEqual(provider.calls, 2)

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

    def test_contract_failure_is_repaired_by_the_same_provider(self) -> None:
        """失败片段携带候选和错误定向重试，不重译同批已合格片段。"""

        source = "The NDVI value was 2.0."
        provider = _RepairingProvider(
            source,
            initial="该值为2.0。",
            repaired="NDVI值为2.0。",
        )
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            write_jsonl_atomic(segments, [{"id": segment_id(source), "source": source}])

            self.assertEqual(
                translate_segment_file(
                    segments_path=segments,
                    translations_path=translations,
                    provider=provider,
                    domain=domain,
                ),
                (0, 1),
            )
            self.assertEqual(len(provider.contexts), 2)
            feedback = provider.contexts[1].repair_feedback[segment_id(source)]
            self.assertEqual(feedback[0], "该值为2.0。")
            self.assertTrue(any("NDVI" in error for error in feedback[1]))
            self.assertEqual(read_jsonl(translations)[0]["target"], "NDVI值为2.0。")
            self.assertFalse((root / "rejected_translations.jsonl").exists())

    def test_second_contract_failure_keeps_both_attempts(self) -> None:
        """一次定向重试仍失败时停止，并把两轮候选留作审计证据。"""

        source = "The NDVI value was 2.0."
        provider = _RepairingProvider(source, initial="该值为2.0。", repaired="数值为2.0。")
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            write_jsonl_atomic(segments, [{"id": segment_id(source), "source": source}])

            with self.assertRaisesRegex(ValueError, "定向重试后仍有"):
                translate_segment_file(
                    segments_path=segments,
                    translations_path=translations,
                    provider=provider,
                    domain=domain,
                )
            rejected = read_jsonl(root / "rejected_translations.jsonl")
            self.assertEqual(len(rejected[0]["attempts"]), 2)
            self.assertEqual(len(provider.contexts), 2)

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

    def test_rejected_nontranslatable_segment_can_resume_as_passthrough(self) -> None:
        """人工透传不能放宽门禁，但可接管尚未保存的真实失败片段。"""

        source = (
            "Alice Smith, Bob Jones, Carol White, David Brown, "
            "Edward Green, and Frances Black"
        )
        sid = segment_id(source)
        provider = _MappingProvider({source: source})
        domain = load_domain_pack("atmospheric-science")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.jsonl"
            translations = root / "translations.jsonl"
            write_jsonl_atomic(segments, [{"id": sid, "source": source}])

            with self.assertRaisesRegex(ValueError, "未通过门禁"):
                translate_segment_file(
                    segments_path=segments,
                    translations_path=translations,
                    provider=provider,
                    domain=domain,
                )
            rejected = root / "rejected_translations.jsonl"
            self.assertTrue(rejected.is_file())

            self.assertEqual(
                translate_segment_file(
                    segments_path=segments,
                    translations_path=translations,
                    provider=provider,
                    domain=domain,
                    passthrough_segment_ids={sid},
                ),
                (0, 1),
            )
            # 0.4.2 先用同一 Provider 做一次定向合同修复；两次候选
            # 均失败后才等待透传确认，不再反复调用模型。
            self.assertEqual(provider.calls, 2)
            self.assertFalse(rejected.exists())
            self.assertEqual(read_jsonl(translations)[0]["target"], source)
            validate_translation_files(
                segments,
                translations,
                domain,
                passthrough_segment_ids={sid},
            )
            write_jsonl_atomic(
                translations,
                [{"id": sid, "source": source, "target": "作者名单"}],
            )
            with self.assertRaisesRegex(ValueError, "必须与原文完全相同"):
                validate_translation_files(
                    segments,
                    translations,
                    domain,
                    passthrough_segment_ids={sid},
                )


if __name__ == "__main__":
    unittest.main()
