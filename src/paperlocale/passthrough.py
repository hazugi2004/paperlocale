"""不应翻译片段的人工确认映射与输入身份校验。

纯公式、作者姓名串或被版面引擎切断的文字对象可能没有合理中文译文。此类片段
不能通过放宽全局中文门禁来处理；只有人工按稳定片段 ID 确认后，流水线才允许
把原文原样写入译文表。映射同时绑定源 PDF 和 ``segments.jsonl`` 的确切字节。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .contracts import read_jsonl, segment_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    """同目录原子替换，避免中断留下半份人工审计记录。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _ordered_segment_ids(segments_path: Path) -> list[str]:
    """验证片段稳定身份并按收集顺序返回唯一 ID。"""

    ordered: list[str] = []
    seen: set[str] = set()
    for row in read_jsonl(segments_path):
        source = str(row.get("source", ""))
        sid = str(row.get("id", ""))
        if not source or sid != segment_id(source):
            raise ValueError(f"片段缺少原文或 ID 不一致：{sid!r}")
        if sid in seen:
            raise ValueError(f"片段包含重复 ID：{sid}")
        seen.add(sid)
        ordered.append(sid)
    return ordered


def passthrough_segment_ids(mapping: dict[str, object]) -> set[str]:
    """从已校验映射中返回透传片段 ID。"""

    entries = mapping["entries"]
    if not isinstance(entries, list):  # pragma: no cover - 公开加载器已先校验。
        raise ValueError("透传映射 entries 字段非法")
    return {str(entry["id"]) for entry in entries if isinstance(entry, dict)}


def load_passthrough_map(
    *,
    source_sha256: str,
    segments_path: Path,
    map_path: Path,
) -> dict[str, object]:
    """读取透传映射，并验证它仍属于当前源 PDF 和片段文件。"""

    if not map_path.is_file():
        raise FileNotFoundError(f"透传映射不存在：{map_path}")
    mapping = json.loads(map_path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        raise ValueError("透传映射根节点必须是对象")
    if mapping.get("schema_version") != 1:
        raise ValueError("透传映射 schema_version 非法")
    if mapping.get("source_sha256") != source_sha256:
        raise ValueError("透传映射不属于当前源 PDF")
    if mapping.get("segments_sha256") != _sha256(segments_path):
        raise ValueError("segments.jsonl 已变化；请重新复核透传映射")

    known_ids = set(_ordered_segment_ids(segments_path))

    entries = mapping.get("entries")
    if not isinstance(entries, list):
        raise ValueError("透传映射 entries 字段非法")
    selected: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("透传映射 entries 成员必须是对象")
        sid = entry.get("id")
        reason = entry.get("reason")
        confirmed_by = entry.get("confirmed_by")
        confirmed_at = entry.get("confirmed_at")
        if not isinstance(sid, str) or not sid:
            raise ValueError("透传映射条目缺少片段 ID")
        if sid in selected:
            raise ValueError("透传映射包含重复片段 ID")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"透传映射条目缺少 reason：{sid}")
        if not isinstance(confirmed_by, str) or not confirmed_by.strip():
            raise ValueError(f"透传映射条目缺少 confirmed_by：{sid}")
        if not isinstance(confirmed_at, str) or not confirmed_at.strip():
            raise ValueError(f"透传映射条目缺少 confirmed_at：{sid}")
        selected.add(sid)
    unknown = sorted(selected - known_ids)
    if unknown:
        raise ValueError(f"透传映射包含未知片段 ID：{unknown}")
    return mapping


def confirm_passthrough_map(
    *,
    source_sha256: str,
    segments_path: Path,
    output_dir: Path,
    segment_ids: list[str],
    reason: str,
    confirmed_by: str,
) -> dict[str, object]:
    """新增一组具有同一明确原因的人工透传片段。

    已确认条目只允许幂等重复，不能用后续命令静默改写原因或确认人。若需纠正
    尚未翻译的错误映射，应明确删除整份映射并重新复核，而不是抹掉审计历史。
    """

    cleaned_reason = reason.strip()
    cleaned_reviewer = confirmed_by.strip()
    if not segment_ids:
        raise ValueError("至少需要一个 segment_id")
    if not cleaned_reason:
        raise ValueError("reason 不能为空")
    if not cleaned_reviewer:
        raise ValueError("confirmed_by 不能为空")

    map_path = output_dir / "passthrough_map.json"
    if map_path.is_file():
        mapping = load_passthrough_map(
            source_sha256=source_sha256,
            segments_path=segments_path,
            map_path=map_path,
        )
        entries = list(mapping["entries"])
    else:
        mapping = {
            "schema_version": 1,
            "source_sha256": source_sha256,
            "segments_sha256": _sha256(segments_path),
            "entries": [],
        }
        entries = []

    ordered_ids = _ordered_segment_ids(segments_path)
    requested = set(segment_ids)
    unknown = sorted(requested - set(ordered_ids))
    if unknown:
        raise ValueError(f"透传确认包含未知片段 ID：{unknown}")

    existing = {
        str(entry["id"]): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    for sid in requested & set(existing):
        entry = existing[sid]
        if (
            entry.get("reason") != cleaned_reason
            or entry.get("confirmed_by") != cleaned_reviewer
        ):
            raise ValueError(f"透传片段已用不同审计信息确认：{sid}")

    new_ids = requested - set(existing)
    if not new_ids:
        # 完全相同的重复确认不改时间戳和文件字节，保持命令真正幂等。
        return mapping

    confirmed_at = _utc_now()
    for sid in ordered_ids:
        if sid in new_ids:
            entries.append(
                {
                    "id": sid,
                    "reason": cleaned_reason,
                    "confirmed_by": cleaned_reviewer,
                    "confirmed_at": confirmed_at,
                }
            )
    mapping["entries"] = entries
    mapping["updated_at"] = confirmed_at
    _write_json_atomic(map_path, mapping)
    return mapping
