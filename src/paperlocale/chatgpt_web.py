"""ChatGPT 网页端普通 Chat 的人工、可审计翻译桥接。

本模块不登录、不抓取也不自动点击 ``chatgpt.com``。它只把当前运行的待译片段
导出为带哈希的提示文件，并把用户从网页 Chat 保存的严格 JSON 回复重新送入现有
``translate_run``。因此所有参考文献、人工透传、内容合同和断点语义仍只有一套。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .contracts import read_jsonl, segment_id, write_jsonl_atomic
from .domains import DomainPack
from .pipeline import make_batches
from .providers.base import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
    build_prompt,
    parse_payload,
)
from .workflow import (
    _load_passthrough_configuration,
    _load_reference_configuration,
    _prepare_segment_safety_configuration,
    _sha256,
    _verify_domain_languages,
    _verify_source_pdf,
    load_manifest,
    save_manifest,
    translate_run,
)

BATCH_SCHEMA_VERSION = 1
PROVIDER_NAME = "chatgpt-web-manual"


def _utc_now() -> str:
    """使用与运行清单一致的 UTC 秒级时间，便于人工对照审计记录。"""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_sha256(value: object) -> str:
    """对结构化身份使用排序、紧凑 JSON，避免缩进差异改变批次哈希。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path: Path, value: object) -> None:
    """同目录原子替换 JSON，避免复制网页回复时留下半份批次清单。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_segments(path: Path) -> list[Segment]:
    """读取并验证稳定片段身份，拒绝把改写后的原文导出到旧运行。"""

    segments: list[Segment] = []
    seen: set[str] = set()
    for row in read_jsonl(path):
        source = str(row.get("source", ""))
        sid = str(row.get("id", ""))
        if not source or sid != segment_id(source):
            raise ValueError(f"片段缺少原文或 ID 不一致：{sid!r}")
        if sid in seen:
            raise ValueError(f"片段包含重复 ID：{sid}")
        seen.add(sid)
        segments.append(Segment(id=sid, source=source))
    return segments


def _batch_identity(
    *,
    batch_id: str,
    batch: list[Segment],
    context: TranslationContext,
) -> dict[str, object]:
    """批次哈希只绑定稳定输入，不包含生成时间或本机绝对路径。"""

    return {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "source_language": context.source_language,
        "target_language": context.target_language,
        "domain_pack_sha256": context.domain.content_sha256,
        "reference_policy": context.reference_policy,
        "segments": [
            {
                "id": segment.id,
                "source": segment.source,
                "kind": (
                    "reference"
                    if segment.id in context.reference_segment_ids
                    else "body"
                ),
            }
            for segment in batch
        ],
    }


def _web_prompt(
    *,
    batch_id: str,
    batch_sha256: str,
    batch: list[Segment],
    context: TranslationContext,
) -> str:
    """在共用翻译提示外增加网页人工交换所需的批次回显字段。"""

    return f"""# PaperLocale ChatGPT Web 人工翻译批次

请在 ChatGPT 网页端的普通 **Chat** 模式完成本批翻译。不要切换到 Codex 或
ChatGPT Work。不要上传源 PDF；本提示只包含版面引擎已经拆出的具体片段。

只返回一个合法 JSON 对象，并把它放入一个且仅一个 `json` 代码块。
代码块外不要添加解释。这样可避免 ChatGPT 富文本层把 URL 或邮箱
自动改写为 Markdown 链接。顶层字段必须且只能是 `batch_id`、
`batch_sha256` 和 `translations`：

{{
  "batch_id": "{batch_id}",
  "batch_sha256": "{batch_sha256}",
  "translations": [
    {{"id": "原样回显输入 ID", "target": "中文译文"}}
  ]
}}

`batch_id` 与 `batch_sha256` 必须逐字回显；`translations` 必须覆盖下方全部输入 ID，
不得缺失、重复或增加 ID。完成后使用代码块的“复制”按钮，把
其中的 JSON 对象保存为
`responses/{batch_id}.json`，再交给 PaperLocale 导入。

{build_prompt(batch, context)}
"""


def _load_existing_batch_manifest(
    root: Path,
    run_manifest: dict[str, object],
) -> dict[str, object] | None:
    """若已导出则复用确切清单，禁止部分导入后静默重分批。"""

    recorded = run_manifest.get("chatgpt_web_batch_manifest")
    if recorded is None:
        return None
    path = Path(str(recorded))
    expected_hash = run_manifest.get("chatgpt_web_batch_manifest_sha256")
    if not path.is_file() or not isinstance(expected_hash, str):
        raise ValueError("运行清单绑定的 ChatGPT Web 批次清单不存在或缺少哈希")
    if _sha256(path) != expected_hash:
        raise ValueError("ChatGPT Web 批次清单在导出后发生变化")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("ChatGPT Web 批次清单必须是 JSON 对象")
    if path.parent != (root / "chatgpt_web").resolve():
        raise ValueError("ChatGPT Web 批次清单不属于当前运行目录")
    return value


def _verify_batch_manifest_identity(
    *,
    batch_manifest: dict[str, object],
    run_manifest: dict[str, object],
    domain: DomainPack,
    reference_policy: str,
) -> None:
    """重新核对所有仍可变化的输入，禁止把旧提示回复导入新片段或新领域包。"""

    segments_path = Path(str(run_manifest["segments_path"]))
    root = segments_path.parent
    reference_map_path = root / "reference_map.json"
    passthrough_map_path = root / "passthrough_map.json"
    expected = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "provider": PROVIDER_NAME,
        "source_sha256": run_manifest["source_sha256"],
        "segments_sha256": _sha256(segments_path),
        "domain_pack_sha256": domain.content_sha256,
        "reference_policy": reference_policy,
        "reference_map_sha256": _sha256(reference_map_path),
        "passthrough_map_sha256": (
            _sha256(passthrough_map_path)
            if passthrough_map_path.is_file()
            else None
        ),
    }
    mismatched = {
        key: (batch_manifest.get(key), value)
        for key, value in expected.items()
        if batch_manifest.get(key) != value
    }
    if mismatched:
        raise ValueError(f"ChatGPT Web 批次身份与当前运行不一致：{mismatched}")


def export_chatgpt_web_batches(
    *,
    run_dir: Path,
    domain: DomainPack,
    max_segments: int = 20,
    max_characters: int = 12000,
    reference_policy: str = "preserve",
) -> dict[str, object]:
    """导出当前运行全部待译片段，并将批次清单哈希绑定到运行清单。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] not in {"collected", "translated"}:
        raise ValueError(f"chatgpt-web-export 不接受状态：{manifest['status']}")
    _verify_source_pdf(manifest)
    _verify_domain_languages(manifest, domain)

    existing_manifest = _load_existing_batch_manifest(root, manifest)
    if existing_manifest is not None:
        _verify_batch_manifest_identity(
            batch_manifest=existing_manifest,
            run_manifest=manifest,
            domain=domain,
            reference_policy=reference_policy,
        )
        expected = {
            "max_segments": max_segments,
            "max_characters": max_characters,
        }
        mismatched = {
            key: (existing_manifest.get(key), value)
            for key, value in expected.items()
            if existing_manifest.get(key) != value
        }
        if mismatched:
            raise ValueError(f"已有批次清单与本次导出参数不一致：{mismatched}")
        return existing_manifest

    recorded_provider = manifest.get("translation_provider")
    if recorded_provider is not None:
        raise ValueError("当前运行已绑定其他翻译 Provider，不能改为 ChatGPT Web")

    reference_ids, reference_map_path = _load_reference_configuration(
        root,
        manifest,
        reference_policy,
    )
    passthrough_ids, passthrough_map_path = _load_passthrough_configuration(
        root,
        manifest,
    )
    overlap = reference_ids & passthrough_ids
    if overlap:
        raise ValueError(f"参考文献与透传映射不能包含相同片段：{sorted(overlap)}")
    _prepare_segment_safety_configuration(
        root,
        manifest,
        passthrough_ids,
        reference_ids,
    )

    segments_path = Path(str(manifest["segments_path"]))
    segments = _read_segments(segments_path)
    existing_rows = (
        read_jsonl(Path(str(manifest["translations_path"])))
        if Path(str(manifest["translations_path"])).is_file()
        else []
    )
    if existing_rows:
        raise ValueError(
            "首次导出前已存在来源未绑定的译文；请使用新的运行目录，"
            "或继续原 Provider 运行"
        )
    existing_ids = {str(row.get("id", "")) for row in existing_rows}
    if len(existing_ids) != len(existing_rows):
        raise ValueError("既有译文包含重复 ID，不能导出网页批次")

    preserved_ids = set(passthrough_ids)
    if reference_policy == "preserve":
        preserved_ids.update(reference_ids)
    pending = [
        segment
        for segment in segments
        if segment.id not in preserved_ids and segment.id not in existing_ids
    ]
    if not pending:
        raise ValueError("当前运行没有需要发送到 ChatGPT Web 的片段")

    context = TranslationContext(
        source_language=domain.source_language,
        target_language=domain.target_language,
        domain=domain,
        reference_policy=reference_policy,
        reference_segment_ids=frozenset(reference_ids),
    )
    root_output = (root / "chatgpt_web").resolve()
    prompts_dir = root_output / "prompts"
    responses_dir = root_output / "responses"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)

    batch_rows: list[dict[str, object]] = []
    for index, batch in enumerate(
        make_batches(
            pending,
            max_segments=max_segments,
            max_characters=max_characters,
        ),
        1,
    ):
        batch_id = f"batch-{index:03d}"
        identity = _batch_identity(batch_id=batch_id, batch=batch, context=context)
        batch_sha256 = _canonical_sha256(identity)
        prompt_path = prompts_dir / f"{batch_id}.md"
        prompt_path.write_text(
            _web_prompt(
                batch_id=batch_id,
                batch_sha256=batch_sha256,
                batch=batch,
                context=context,
            ),
            encoding="utf-8",
        )
        batch_rows.append(
            {
                "batch_id": batch_id,
                "batch_sha256": batch_sha256,
                "prompt": str(prompt_path),
                "prompt_sha256": _sha256(prompt_path),
                "response": str(responses_dir / f"{batch_id}.json"),
                "segment_ids": [segment.id for segment in batch],
                "source_characters": sum(len(segment.source) for segment in batch),
            }
        )

    batch_manifest: dict[str, object] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "provider": PROVIDER_NAME,
        "source_sha256": manifest["source_sha256"],
        "segments_sha256": _sha256(segments_path),
        "domain_pack_sha256": domain.content_sha256,
        "reference_policy": reference_policy,
        "reference_map_sha256": _sha256(reference_map_path),
        "passthrough_map_sha256": (
            _sha256(passthrough_map_path)
            if passthrough_map_path is not None
            else None
        ),
        "max_segments": max_segments,
        "max_characters": max_characters,
        "batch_count": len(batch_rows),
        "segment_count": len(pending),
        "batches": batch_rows,
    }
    batch_manifest_path = root_output / "batch_manifest.json"
    _write_json_atomic(batch_manifest_path, batch_manifest)
    manifest["chatgpt_web_batch_manifest"] = str(batch_manifest_path)
    manifest["chatgpt_web_batch_manifest_sha256"] = _sha256(batch_manifest_path)
    save_manifest(root, manifest)
    return batch_manifest


class _ChatGPTWebResponseProvider(TranslationProvider):
    """把人工保存的网页回复适配为现有同步 Provider 合同。"""

    def __init__(
        self,
        *,
        batch_manifest_path: Path,
        model_label: str,
    ) -> None:
        label = model_label.strip()
        if not label:
            raise ValueError("--model-label 不能为空")
        self.batch_manifest_path = batch_manifest_path.resolve()
        self.batch_manifest_sha256 = _sha256(self.batch_manifest_path)
        self.model_label = label
        self.response_paths: list[Path] = []
        self.response_hashes: dict[str, str] = {}
        self.translations: dict[str, Translation] = {}

        value = json.loads(self.batch_manifest_path.read_text(encoding="utf-8"))
        batches = value.get("batches") if isinstance(value, dict) else None
        if not isinstance(batches, list):
            raise TypeError("ChatGPT Web 批次清单缺少 batches 数组")
        for row in batches:
            if not isinstance(row, dict):
                raise TypeError("ChatGPT Web 批次记录必须是对象")
            batch_id = str(row.get("batch_id", ""))
            response_path = Path(str(row.get("response", "")))
            if not response_path.is_file():
                continue
            payload = json.loads(response_path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or set(payload) != {
                "batch_id",
                "batch_sha256",
                "translations",
            }:
                raise ValueError(
                    f"网页回复顶层字段非法：{response_path}"
                )
            if payload["batch_id"] != batch_id:
                raise ValueError(f"网页回复 batch_id 不匹配：{response_path}")
            if payload["batch_sha256"] != row.get("batch_sha256"):
                raise ValueError(f"网页回复 batch_sha256 不匹配：{response_path}")
            expected = [
                Segment(id=str(sid), source="")
                for sid in row.get("segment_ids", [])
            ]
            parsed = parse_payload(payload, expected)
            for translation in parsed:
                if translation.id in self.translations:
                    raise ValueError(f"网页回复重复覆盖片段 ID：{translation.id}")
                self.translations[translation.id] = translation
            self.response_paths.append(response_path.resolve())
            self.response_hashes[batch_id] = _sha256(response_path)

    def provenance(self) -> dict[str, object]:
        """记录人工网页边界，不把它冒充为官方 API 或自动化 Provider。"""

        return {
            "provider": PROVIDER_NAME,
            "model": self.model_label,
            "interface_mode": "manual-copy-paste",
            "browser_automation": False,
            "batch_manifest_sha256": self.batch_manifest_sha256,
        }

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        """按本次仍待译的 ID 查表，支持合同失败后只重试原批次子集。"""

        missing = [segment.id for segment in segments if segment.id not in self.translations]
        if missing:
            raise ValueError(
                "缺少覆盖当前片段的 ChatGPT Web 回复；请保存对应 responses/*.json："
                f"{missing}"
            )
        return [self.translations[segment.id] for segment in segments]


def _snapshot_responses(root: Path, provider: _ChatGPTWebResponseProvider) -> list[dict[str, str]]:
    """导入前保存网页回复原始字节，后续覆盖修订文件也不会抹去旧证据。"""

    snapshots: list[dict[str, str]] = []
    target_dir = root / "chatgpt_web" / "imports"
    target_dir.mkdir(parents=True, exist_ok=True)
    for source in provider.response_paths:
        response_sha256 = _sha256(source)
        target = target_dir / f"{source.stem}-{response_sha256}.json"
        if not target.exists():
            shutil.copyfile(source, target)
        snapshots.append(
            {
                "source": str(source),
                "snapshot": str(target.resolve()),
                "sha256": response_sha256,
            }
        )
    return snapshots


def _record_import_attempt(
    *,
    root: Path,
    model_label: str,
    snapshots: list[dict[str, str]],
    outcome: str,
    error: str | None,
) -> None:
    """原子追加导入尝试，并把历史文件哈希重新绑定到运行清单。"""

    history_path = root / "chatgpt_web" / "import_history.jsonl"
    history = read_jsonl(history_path) if history_path.is_file() else []
    row: dict[str, object] = {
        "imported_at": _utc_now(),
        "model_label": model_label,
        "outcome": outcome,
        "responses": snapshots,
    }
    if error is not None:
        row["error"] = error
    history.append(row)

    write_jsonl_atomic(history_path, history)
    manifest = load_manifest(root)
    manifest["chatgpt_web_import_history"] = str(history_path.resolve())
    manifest["chatgpt_web_import_history_sha256"] = _sha256(history_path)
    save_manifest(root, manifest)


def import_chatgpt_web_responses(
    *,
    run_dir: Path,
    domain: DomainPack,
    model_label: str,
    reference_policy: str = "preserve",
) -> tuple[int, int]:
    """验证网页回复身份，保留原始字节，并复用生产翻译状态机导入译文。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    batch_manifest_value = manifest.get("chatgpt_web_batch_manifest")
    if batch_manifest_value is None:
        raise ValueError("请先执行 chatgpt-web-export")
    if not isinstance(batch_manifest_value, str):
        raise TypeError("运行清单中的 ChatGPT Web 批次路径必须是字符串")
    batch_manifest_path = Path(batch_manifest_value)
    expected_hash = manifest.get("chatgpt_web_batch_manifest_sha256")
    if not isinstance(expected_hash, str) or _sha256(batch_manifest_path) != expected_hash:
        raise ValueError("ChatGPT Web 批次清单缺失或哈希不一致")
    batch_manifest = json.loads(batch_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(batch_manifest, dict):
        raise TypeError("ChatGPT Web 批次清单必须是 JSON 对象")
    _verify_batch_manifest_identity(
        batch_manifest=batch_manifest,
        run_manifest=manifest,
        domain=domain,
        reference_policy=reference_policy,
    )

    provider = _ChatGPTWebResponseProvider(
        batch_manifest_path=batch_manifest_path,
        model_label=model_label,
    )
    if not provider.response_paths:
        raise ValueError("尚未找到任何 responses/*.json 网页回复")
    snapshots = _snapshot_responses(root, provider)
    try:
        result = translate_run(
            run_dir=root,
            provider=provider,
            domain=domain,
            max_segments=int(batch_manifest["max_segments"]),
            max_characters=int(batch_manifest["max_characters"]),
            reference_policy=reference_policy,
        )
    except Exception as exc:
        _record_import_attempt(
            root=root,
            model_label=provider.model_label,
            snapshots=snapshots,
            outcome="failed",
            error=str(exc),
        )
        raise
    _record_import_attempt(
        root=root,
        model_label=provider.model_label,
        snapshots=snapshots,
        outcome="completed",
        error=None,
    )
    return result
