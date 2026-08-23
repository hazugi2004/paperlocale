"""Provider 领域评估只自动判断可证明合同，语义质量保留人工复核。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paperlocale.domains import load_domain_pack
from paperlocale.evaluation import evaluate_provider, write_evaluation_report
from paperlocale.providers import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
)


class _ReferenceProvider(TranslationProvider):
    """按领域包参考译文返回，验证报告成功路径。"""

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        references = {
            case["source"]: case["target"] for case in context.domain.eval_cases
        }
        return [
            Translation(id=segment.id, target=references[segment.source])
            for segment in segments
        ]


class _SourceEchoProvider(TranslationProvider):
    """故意回传英文原文，验证失败仍生成可复核详情。"""

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        return [
            Translation(id=segment.id, target=segment.source)
            for segment in segments
        ]


class _SingleCaseReferenceProvider(_ReferenceProvider):
    """模拟评估时仍需单条调用的专用翻译模型。"""

    max_batch_segments = 1

    def __init__(self) -> None:
        self.call_count = 0

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        if len(segments) != 1:
            raise AssertionError("评估器未遵守 Provider 的单案例上限")
        self.call_count += 1
        return super().translate(segments, context)


class EvaluationTest(unittest.TestCase):
    def test_evaluation_honors_provider_batch_limit(self) -> None:
        """评估入口与正式流水线必须使用同一 Provider 边界。"""

        domain = load_domain_pack("atmospheric-science")
        provider = _SingleCaseReferenceProvider()
        report = evaluate_provider(
            provider=provider,
            provider_name="single-case-test",
            model=None,
            domain=domain,
        )
        self.assertEqual(provider.call_count, len(domain.eval_cases))
        self.assertEqual(report["contract_passed_count"], len(domain.eval_cases))

    def test_reference_provider_generates_atomic_review_report(self) -> None:
        domain = load_domain_pack("atmospheric-science")
        report = evaluate_provider(
            provider=_ReferenceProvider(),
            provider_name="reference-test",
            model=None,
            domain=domain,
        )
        self.assertEqual(report["contract_passed_count"], len(domain.eval_cases))
        self.assertEqual(report["exact_reference_match_count"], len(domain.eval_cases))
        self.assertTrue(report["manual_semantic_review_required"])
        self.assertTrue(
            all(case["semantic_review"] == "required" for case in report["cases"])
        )

        with tempfile.TemporaryDirectory() as directory:
            output = write_evaluation_report(Path(directory) / "report.json", report)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved, report)
            self.assertFalse(output.with_name(output.name + ".tmp").exists())

    def test_contract_failures_are_reported_without_semantic_guessing(self) -> None:
        domain = load_domain_pack("atmospheric-science")
        report = evaluate_provider(
            provider=_SourceEchoProvider(),
            provider_name="bad-test",
            model=None,
            domain=domain,
        )
        self.assertGreater(report["contract_failed_count"], 0)
        self.assertEqual(report["exact_reference_match_count"], 0)
        self.assertTrue(
            any(case["contract_errors"] for case in report["cases"])
        )
        self.assertTrue(report["manual_semantic_review_required"])


if __name__ == "__main__":
    unittest.main()
