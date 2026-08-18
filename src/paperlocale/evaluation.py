"""用领域包公开案例生成可复核的 Provider 翻译评估报告。"""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import segment_id, validate_translation
from .domains import DomainPack
from .providers import Segment, TranslationContext, TranslationProvider


def evaluate_provider(
    *,
    provider: TranslationProvider,
    provider_name: str,
    model: str | None,
    domain: DomainPack,
) -> dict[str, object]:
    """翻译全部领域案例，并区分自动合同结果与人工语义复核。

    参考译文完全一致只是一项可复现事实，不代表只有一种正确译法；因此报告
    不计算自动语义准确率，也不会因为措辞不同而把候选直接判为错误。
    """

    if not domain.eval_cases:
        raise ValueError(f"领域包没有评估案例：{domain.pack_id}")
    segments = [
        Segment(id=segment_id(case["source"]), source=case["source"])
        for case in domain.eval_cases
    ]
    if len({segment.id for segment in segments}) != len(segments):
        raise ValueError("领域包评估案例包含重复原文")
    context = TranslationContext(
        source_language=domain.source_language,
        target_language=domain.target_language,
        domain=domain,
    )
    translations = provider.translate(segments, context)
    if [item.id for item in translations] != [item.id for item in segments]:
        raise ValueError("Provider 评估输出顺序或 ID 与领域案例不一致")

    cases: list[dict[str, object]] = []
    contract_passed = 0
    exact_matches = 0
    for index, (case, segment, translation) in enumerate(
        zip(domain.eval_cases, segments, translations),
        1,
    ):
        errors = validate_translation(segment.source, translation.target, domain)
        passed = not errors
        exact_match = translation.target == case["target"]
        contract_passed += int(passed)
        exact_matches += int(exact_match)
        cases.append(
            {
                "case": index,
                "id": segment.id,
                "source": segment.source,
                "reference_target": case["target"],
                "candidate_target": translation.target,
                "contract_passed": passed,
                "contract_errors": errors,
                "exact_reference_match": exact_match,
                "semantic_review": "required",
            }
        )

    total = len(cases)
    return {
        "schema_version": 1,
        "domain_pack": {"id": domain.pack_id, "version": domain.version},
        "source_language": domain.source_language,
        "target_language": domain.target_language,
        "provider": {"name": provider_name, "model": model},
        "case_count": total,
        "contract_passed_count": contract_passed,
        "contract_failed_count": total - contract_passed,
        "exact_reference_match_count": exact_matches,
        "manual_semantic_review_required": True,
        "cases": cases,
    }


def write_evaluation_report(path: Path, report: dict[str, object]) -> Path:
    """原子写入评估 JSON，避免模型完成后留下半个报告。"""

    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination
