"""可断点续跑的片段翻译阶段。

本模块只处理 ``segments.jsonl -> translations.jsonl``。PDF 收集、重建和
视觉 QA 属于后续明确阶段，不能在翻译成功时被隐式触发。
"""

from __future__ import annotations

from pathlib import Path

from .contracts import read_jsonl, segment_id, validate_translation, write_jsonl_atomic
from .domains import DomainPack
from .providers import Segment, TranslationContext, TranslationProvider


def make_batches(
    segments: list[Segment],
    *,
    max_segments: int,
    max_characters: int,
) -> list[list[Segment]]:
    """同时限制片段数和字符数，避免结构化输出过长而截断。"""

    if max_segments <= 0 or max_characters <= 0:
        raise ValueError("批次上限必须为正数")
    batches: list[list[Segment]] = []
    current: list[Segment] = []
    current_characters = 0
    for segment in segments:
        size = len(segment.source)
        if current and (
            len(current) >= max_segments
            or current_characters + size > max_characters
        ):
            batches.append(current)
            current = []
            current_characters = 0
        current.append(segment)
        current_characters += size
    if current:
        batches.append(current)
    return batches


def translate_segment_file(
    *,
    segments_path: Path,
    translations_path: Path,
    provider: TranslationProvider,
    domain: DomainPack,
    max_segments: int = 200,
    max_characters: int = 30000,
    reference_segment_ids: set[str] | frozenset[str] = frozenset(),
    reference_policy: str = "preserve",
) -> tuple[int, int]:
    """翻译尚未通过门禁的片段，并在每批成功后原子写入断点。

    返回 ``(复用数量, 新译数量)``。既有译文若已失效会明确失败，避免静默
    覆盖人工修订或把错误缓存继续带入 PDF。
    """

    if reference_policy not in {"preserve", "translate-titles"}:
        raise ValueError(f"参考文献策略非法：{reference_policy}")
    raw_segments = read_jsonl(segments_path)
    segments: list[Segment] = []
    seen: set[str] = set()
    for row in raw_segments:
        source = str(row.get("source", ""))
        sid = str(row.get("id", ""))
        if not source or sid != segment_id(source):
            raise ValueError(f"片段缺少原文或 ID 不一致：{sid!r}")
        if sid in seen:
            raise ValueError(f"片段包含重复 ID：{sid}")
        seen.add(sid)
        segments.append(Segment(id=sid, source=source))

    unknown_reference_ids = set(reference_segment_ids) - seen
    if unknown_reference_ids:
        raise ValueError(f"参考文献映射包含未知片段 ID：{sorted(unknown_reference_ids)}")

    existing_rows = read_jsonl(translations_path) if translations_path.exists() else []
    rejected_path = translations_path.with_name("rejected_translations.jsonl")
    existing: dict[str, dict[str, object]] = {}
    for row in existing_rows:
        sid = str(row.get("id", ""))
        if sid in existing:
            raise ValueError(f"既有译文包含重复 ID：{sid}")
        source = str(row.get("source", ""))
        target = str(row.get("target", ""))
        if sid not in seen or source != next(item.source for item in segments if item.id == sid):
            raise ValueError(f"既有译文不属于当前片段集合：{sid}")
        if sid in reference_segment_ids and reference_policy == "preserve":
            errors = [] if target == source else ["preserve 策略要求参考文献原样保留"]
        elif sid in reference_segment_ids:
            errors = validate_translation(source, target, None)
        else:
            errors = validate_translation(source, target, domain)
        if errors:
            raise ValueError(f"既有译文未通过门禁：{sid}: {errors}")
        existing[sid] = row

    reused_count = len(existing)
    ordered = list(existing.values())
    preserved_count = 0
    if reference_policy == "preserve":
        for segment in segments:
            if segment.id in reference_segment_ids and segment.id not in existing:
                row = {"id": segment.id, "source": segment.source, "target": segment.source}
                existing[segment.id] = row
                ordered.append(row)
                preserved_count += 1
        if preserved_count:
            write_jsonl_atomic(translations_path, ordered)

    pending = [segment for segment in segments if segment.id not in existing]
    context = TranslationContext(
        source_language=domain.source_language,
        target_language=domain.target_language,
        domain=domain,
        reference_policy=reference_policy,
        reference_segment_ids=frozenset(reference_segment_ids),
    )
    for batch in make_batches(
        pending,
        max_segments=max_segments,
        max_characters=max_characters,
    ):
        translated = provider.translate(batch, context)
        if [item.id for item in translated] != [item.id for item in batch]:
            raise ValueError("Provider 返回顺序或 ID 与输入批次不一致")
        accepted: list[dict[str, object]] = []
        rejected: list[dict[str, object]] = []
        for source_segment, result in zip(batch, translated):
            if source_segment.id in reference_segment_ids:
                errors = validate_translation(source_segment.source, result.target, None)
            else:
                errors = validate_translation(source_segment.source, result.target, domain)
            if errors:
                rejected.append(
                    {
                        "id": result.id,
                        "source": source_segment.source,
                        "target": result.target,
                        "errors": errors,
                    }
                )
                continue
            accepted.append(
                {
                    "id": result.id,
                    "source": source_segment.source,
                    "target": result.target,
                }
            )
        if accepted:
            # 即使同批另有失败项，也先保存已通过门禁的结果，避免重复消耗模型额度。
            ordered.extend(accepted)
            write_jsonl_atomic(translations_path, ordered)
        if rejected:
            write_jsonl_atomic(rejected_path, rejected)
            raise ValueError(
                f"本批有 {len(rejected)} 条新译文未通过门禁；"
                f"合格译文已保存，失败候选见 {rejected_path}"
            )
        if rejected_path.exists():
            # 成功重试后清除已经解决的诊断文件，避免把旧失败误认为当前状态。
            rejected_path.unlink()
    return reused_count, preserved_count + len(pending)
