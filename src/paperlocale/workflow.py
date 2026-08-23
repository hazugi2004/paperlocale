"""PDF 翻译运行清单和单向状态机。

每个阶段都先检查前置状态，再执行唯一外部命令，成功后才原子更新清单。
网络、模型或版面引擎失败时，既有片段和译文仍可用于断点续跑。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pymupdf as fitz
from fontTools import subset as fonttools_subset
from fontTools.ttLib import TTFont

from . import __version__
from .contracts import read_jsonl, validate_translation_files
from .domains import DomainPack
from .passthrough import (
    confirm_passthrough_map,
    load_passthrough_map,
    passthrough_segment_ids,
)
from .pipeline import translate_segment_file
from .providers import TranslationProvider
from .qa import inspect_pdf_pair
from .references import (
    REFERENCE_POLICIES,
    confirm_reference_review,
    load_reference_map,
    prepare_reference_review,
)
from .segment_safety import (
    load_segment_safety_summary,
    prepare_segment_safety_review,
)

STATES = (
    "initialized",
    "collected",
    "translated",
    "validated",
    "rendered",
    "qa_generated",
    "accepted",
)
SCHEMA_VERSION = 4


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    """运行清单需要绑定确切源 PDF，因此此处哈希具有实际完整性用途。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(run_dir: Path) -> Path:
    return run_dir / "run_manifest.json"


def load_manifest(run_dir: Path) -> dict[str, object]:
    path = _manifest_path(run_dir)
    if not path.is_file():
        raise FileNotFoundError(f"运行清单不存在：{path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") not in STATES:
        raise ValueError(f"运行清单状态非法：{manifest.get('status')!r}")
    return manifest


def save_manifest(run_dir: Path, manifest: dict[str, object]) -> None:
    """原子更新清单，避免进程中断留下半个 JSON。"""

    run_dir.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = _utc_now()
    path = _manifest_path(run_dir)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _verify_source_pdf(manifest: dict[str, object]) -> Path:
    """确认后续阶段仍在处理初始化时绑定的同一份源 PDF。"""

    source = Path(str(manifest["source_pdf"]))
    if not source.is_file():
        raise FileNotFoundError(f"源 PDF 不存在：{source}")
    actual = _sha256(source)
    expected = str(manifest["source_sha256"])
    if actual != expected:
        raise ValueError(
            "源 PDF 在运行初始化后发生变化；请恢复原文件或创建新的运行目录"
        )
    return source


def _verify_rendered_pdf(
    manifest: dict[str, object],
    *,
    bind_legacy: bool = False,
) -> Path:
    """确认候选 PDF 与渲染阶段绑定的 SHA-256 一致。

    v0.1.0 的运行清单尚无 ``rendered_sha256``。它只允许在重新执行 QA 时
    绑定当前候选；已有旧 QA 不能直接 accept，必须重新生成报告。
    """

    rendered = manifest.get("rendered_pdf")
    if not isinstance(rendered, str) or not Path(rendered).is_file():
        raise FileNotFoundError("运行清单中的 rendered_pdf 不存在")
    path = Path(rendered).resolve()
    actual = _sha256(path)
    expected = manifest.get("rendered_sha256")
    if not isinstance(expected, str) or not expected:
        if not bind_legacy:
            raise ValueError("运行清单缺少译文 PDF 哈希；请重新执行 qa")
        manifest["rendered_sha256"] = actual
        manifest["schema_version"] = SCHEMA_VERSION
        return path
    if actual != expected:
        raise ValueError("译文 PDF 在 render 后发生变化；请重新执行 render 和 qa")
    return path


def _verify_domain_languages(
    manifest: dict[str, object],
    domain: DomainPack,
) -> None:
    """拒绝语言或已记录领域包不一致，避免续跑时静默更换术语合同。"""

    expected = (str(manifest["source_language"]), str(manifest["target_language"]))
    actual = (domain.source_language, domain.target_language)
    if actual != expected:
        raise ValueError(
            "领域包语言与运行不一致："
            f"run={expected[0]}->{expected[1]}, domain={actual[0]}->{actual[1]}"
        )
    recorded = manifest.get("domain_pack")
    if isinstance(recorded, dict):
        recorded_identity = (str(recorded.get("id")), str(recorded.get("version")))
        actual_identity = (domain.pack_id, domain.version)
        if actual_identity != recorded_identity:
            raise ValueError(
                "领域包与翻译记录不一致："
                f"run={recorded_identity[0]}@{recorded_identity[1]}, "
                f"domain={actual_identity[0]}@{actual_identity[1]}"
            )
        recorded_hash = recorded.get("content_sha256")
        if isinstance(recorded_hash, str) and recorded_hash != domain.content_sha256:
            raise ValueError(
                "领域包内容与翻译记录不一致："
                f"run={recorded_hash}, domain={domain.content_sha256}"
            )


def _domain_provenance(domain: DomainPack) -> dict[str, str]:
    """生成运行清单使用的领域包完整身份。"""

    return {
        "id": domain.pack_id,
        "version": domain.version,
        "content_sha256": domain.content_sha256,
    }


def prepare_reference_review_run(run_dir: Path) -> dict[str, object]:
    """为已收集运行生成全片段复核清单，不自动确认任何不确定片段。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] != "collected":
        raise ValueError(
            f"reference-review 只接受 collected，当前为 {manifest['status']}"
        )
    source = _verify_source_pdf(manifest)
    return prepare_reference_review(
        source_pdf=source,
        source_sha256=str(manifest["source_sha256"]),
        segments_path=Path(str(manifest["segments_path"])),
        output_dir=root,
    )


def confirm_reference_run(
    run_dir: Path,
    *,
    additional_segment_ids: list[str],
    confirmed_by: str,
) -> dict[str, object]:
    """确认自动结果和用户补充 ID；已有断点译文后禁止改写映射。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] != "collected":
        raise ValueError(
            f"confirm-references 只接受 collected，当前为 {manifest['status']}"
        )
    translations = Path(str(manifest["translations_path"]))
    if translations.exists() and read_jsonl(translations):
        raise ValueError("已有断点译文，不能改变参考文献映射")
    source = _verify_source_pdf(manifest)
    return confirm_reference_review(
        source_pdf=source,
        source_sha256=str(manifest["source_sha256"]),
        segments_path=Path(str(manifest["segments_path"])),
        output_dir=root,
        additional_segment_ids=additional_segment_ids,
        confirmed_by=confirmed_by,
    )


def confirm_passthrough_run(
    run_dir: Path,
    *,
    segment_ids: list[str],
    reason: str,
    confirmed_by: str,
) -> dict[str, object]:
    """确认无需翻译的片段，并把映射哈希立即绑定到运行清单。

    部分批次失败后运行状态仍为 ``collected``，因此可以把失败但确实不应翻译的
    片段加入映射。若该片段已有与原文不同的合格译文，则拒绝改变其既有语义。
    """

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] != "collected":
        raise ValueError(
            f"confirm-passthrough 只接受 collected，当前为 {manifest['status']}"
        )
    _verify_source_pdf(manifest)
    segments_path = Path(str(manifest["segments_path"]))
    translations_path = Path(str(manifest["translations_path"]))
    translation_rows = (
        read_jsonl(translations_path) if translations_path.is_file() else []
    )
    existing_translations = {
        str(row.get("id", "")): row for row in translation_rows
    }
    if len(existing_translations) != len(translation_rows):
        raise ValueError("既有译文包含重复 ID，不能确认人工透传")
    for sid in set(segment_ids):
        row = existing_translations.get(sid)
        if row is not None and row.get("target") != row.get("source"):
            raise ValueError(f"片段已有非透传译文，不能改为人工透传：{sid}")
    reference_map_path = root / "reference_map.json"
    if not reference_map_path.is_file():
        raise ValueError("请先执行 confirm-references，再确认人工透传片段")
    reference_mapping = load_reference_map(
        source_sha256=str(manifest["source_sha256"]),
        segments_path=segments_path,
        map_path=reference_map_path,
    )
    reference_ids = {
        str(segment_id)
        for segment_id in reference_mapping["reference_segment_ids"]
    }
    overlap = set(segment_ids) & reference_ids
    if overlap:
        raise ValueError(
            f"参考文献片段不能重复确认为人工透传：{sorted(overlap)}"
        )

    mapping = confirm_passthrough_map(
        source_sha256=str(manifest["source_sha256"]),
        segments_path=segments_path,
        output_dir=root,
        segment_ids=segment_ids,
        reason=reason,
        confirmed_by=confirmed_by,
    )
    map_path = root / "passthrough_map.json"
    manifest["passthrough_map"] = str(map_path.resolve())
    manifest["passthrough_map_sha256"] = _sha256(map_path)
    manifest["passthrough_segment_count"] = len(passthrough_segment_ids(mapping))
    manifest["schema_version"] = SCHEMA_VERSION
    save_manifest(root, manifest)
    return mapping


def _load_reference_configuration(
    root: Path,
    manifest: dict[str, object],
    reference_policy: str,
) -> tuple[set[str], Path]:
    """读取人工确认映射；缺失时先生成复核文件，再明确停止。"""

    if reference_policy not in REFERENCE_POLICIES:
        raise ValueError(
            "reference_policy 必须是 " + ", ".join(REFERENCE_POLICIES)
        )
    recorded_policy = manifest.get("reference_policy")
    if isinstance(recorded_policy, str) and recorded_policy != reference_policy:
        raise ValueError(
            "参考文献策略与已有断点不一致："
            f"run={recorded_policy}, current={reference_policy}"
        )
    map_path = root / "reference_map.json"
    if not map_path.is_file():
        summary = prepare_reference_review_run(root)
        raise ValueError(
            "参考文献映射尚未人工确认；请检查 "
            f"{summary['review_jsonl']}，然后执行 paperlocale confirm-references"
        )
    mapping = load_reference_map(
        source_sha256=str(manifest["source_sha256"]),
        segments_path=Path(str(manifest["segments_path"])),
        map_path=map_path,
    )
    selected = {str(segment_id) for segment_id in mapping["reference_segment_ids"]}
    return selected, map_path


def _load_passthrough_configuration(
    root: Path,
    manifest: dict[str, object],
) -> tuple[set[str], Path | None]:
    """读取可选人工透传映射，并拒绝绕过确认命令的字节改写。"""

    map_path = root / "passthrough_map.json"
    if not map_path.is_file():
        if (
            "passthrough_map" in manifest
            or "passthrough_map_sha256" in manifest
            or manifest.get("passthrough_segment_count") not in {None, 0}
        ):
            raise FileNotFoundError("运行清单已绑定透传映射，但映射文件不存在")
        return set(), None
    recorded_hash = manifest.get("passthrough_map_sha256")
    if not isinstance(recorded_hash, str):
        raise ValueError("透传映射未由 confirm-passthrough 绑定到运行清单")
    if _sha256(map_path) != recorded_hash:
        raise ValueError("透传映射在人工确认后发生变化")
    mapping = load_passthrough_map(
        source_sha256=str(manifest["source_sha256"]),
        segments_path=Path(str(manifest["segments_path"])),
        map_path=map_path,
    )
    return passthrough_segment_ids(mapping), map_path


def _recorded_reference_configuration(
    manifest: dict[str, object],
) -> tuple[set[str], str]:
    """验证阶段读取翻译时绑定的映射；无字段表示 v0.2 旧运行。"""

    policy = manifest.get("reference_policy")
    if not isinstance(policy, str):
        return set(), "preserve"
    map_value = manifest.get("reference_map")
    if not isinstance(map_value, str):
        raise ValueError("运行清单缺少翻译时绑定的 reference_map")
    map_path = Path(map_value)
    expected_hash = manifest.get("reference_map_sha256")
    if not isinstance(expected_hash, str) or _sha256(map_path) != expected_hash:
        raise ValueError("参考文献映射在翻译后发生变化")
    mapping = load_reference_map(
        source_sha256=str(manifest["source_sha256"]),
        segments_path=Path(str(manifest["segments_path"])),
        map_path=map_path,
    )
    return {str(segment_id) for segment_id in mapping["reference_segment_ids"]}, policy


def _recorded_passthrough_configuration(
    manifest: dict[str, object],
) -> set[str]:
    """验证阶段重新核对翻译时绑定的透传映射。"""

    map_value = manifest.get("passthrough_map")
    if not isinstance(map_value, str):
        if manifest.get("passthrough_segment_count") not in {None, 0}:
            raise ValueError("运行清单缺少翻译时绑定的 passthrough_map")
        return set()
    map_path = Path(map_value)
    expected_hash = manifest.get("passthrough_map_sha256")
    if not isinstance(expected_hash, str) or _sha256(map_path) != expected_hash:
        raise ValueError("透传映射在翻译后发生变化")
    mapping = load_passthrough_map(
        source_sha256=str(manifest["source_sha256"]),
        segments_path=Path(str(manifest["segments_path"])),
        map_path=map_path,
    )
    return passthrough_segment_ids(mapping)


def _prepare_segment_safety_configuration(
    root: Path,
    manifest: dict[str, object],
    passthrough_ids: set[str],
) -> set[str]:
    """生成确定性安全清单，并阻止未确认的碎词或不可见短片段进入模型。"""

    summary = prepare_segment_safety_review(
        source_pdf=Path(str(manifest["source_pdf"])),
        source_sha256=str(manifest["source_sha256"]),
        segments_path=Path(str(manifest["segments_path"])),
        output_dir=root,
    )
    summary_path = root / "segment_safety_summary.json"
    manifest["segment_safety_summary"] = str(summary_path.resolve())
    manifest["segment_safety_summary_sha256"] = _sha256(summary_path)
    manifest["segment_safety_required_count"] = summary[
        "required_passthrough_count"
    ]
    manifest["schema_version"] = SCHEMA_VERSION
    save_manifest(root, manifest)

    required = {
        str(sid) for sid in summary["required_passthrough_segment_ids"]
    }
    missing = sorted(required - passthrough_ids)
    if missing:
        raise ValueError(
            "检测到不可安全独立翻译的片段；请检查 "
            f"{summary['review_jsonl']}，再用 confirm-passthrough 确认全部 ID："
            f"{missing}"
        )
    return required


def _recorded_segment_safety_configuration(
    manifest: dict[str, object],
    passthrough_ids: set[str],
) -> set[str]:
    """验证阶段核对翻译前生成的安全清单及其人工透传闭合关系。"""

    summary_value = manifest.get("segment_safety_summary")
    if not isinstance(summary_value, str):
        # v0.3.1 和早期 schema 4 运行没有安全清单，仍允许按原证据验证。
        return set()
    summary_path = Path(summary_value)
    expected_hash = manifest.get("segment_safety_summary_sha256")
    if not isinstance(expected_hash, str) or _sha256(summary_path) != expected_hash:
        raise ValueError("片段安全复核摘要在翻译后发生变化")
    summary = load_segment_safety_summary(
        source_sha256=str(manifest["source_sha256"]),
        segments_path=Path(str(manifest["segments_path"])),
        summary_path=summary_path,
    )
    required = {
        str(sid) for sid in summary["required_passthrough_segment_ids"]
    }
    missing = sorted(required - passthrough_ids)
    if missing:
        raise ValueError(f"片段安全复核所需透传映射不闭合：{missing}")
    return required


def _layout_provenance(executable: str) -> dict[str, str]:
    """读取实际 pdf2zh-next 命令版本和同环境 BabelDOC 包版本。"""

    completed: subprocess.CompletedProcess[str] | None = None
    for attempt in range(2):
        try:
            completed = subprocess.run(
                [executable, "--version"],
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=60,
                check=False,
            )
            break
        except subprocess.TimeoutExpired as exc:
            # pdf2zh-next 干净安装后的首次启动可能超过 60 秒；被终止后第二次会复用
            # 已建立的本地缓存。只对这个已观察到的冷启动超时重试一次。
            if attempt == 1:
                raise RuntimeError("读取 pdf2zh-next 版本连续两次超时") from exc
    if completed is None:  # pragma: no cover - 循环只会成功赋值或抛出异常。
        raise RuntimeError("无法启动 pdf2zh-next 版本探测")
    combined = completed.stdout + "\n" + completed.stderr
    match = re.search(r"pdf2zh-next version:\s*([^\s]+)", combined)
    if completed.returncode != 0 or match is None:
        raise RuntimeError(
            f"无法读取 pdf2zh-next 版本，exit={completed.returncode}"
        )
    try:
        babeldoc_version = importlib.metadata.version("babeldoc")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("当前 Python 环境无法读取 BabelDOC 版本") from exc
    resolved_executable = shutil.which(executable) or str(
        Path(executable).expanduser().resolve()
    )
    return {
        "executable": resolved_executable,
        "pdf2zh_next_version": match.group(1),
        "babeldoc_version": babeldoc_version,
    }


def initialize_run(
    *,
    source_pdf: Path,
    run_dir: Path,
    source_language: str,
    target_language: str,
    pages: str | None = None,
) -> dict[str, object]:
    """创建一次绑定源文件身份的运行；已有清单时拒绝覆盖。"""

    source = source_pdf.expanduser().resolve()
    root = run_dir.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"源 PDF 不存在：{source}")
    if _manifest_path(root).exists():
        raise FileExistsError(f"运行目录已有清单，请继续原运行或使用新目录：{root}")
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        # 不同版本可以读取同一清单架构，但新运行必须留下
        # 实际创建它的 PaperLocale 版本，便于重现和故障定位。
        "paperlocale_version": __version__,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "status": "initialized",
        "source_pdf": str(source),
        "source_sha256": _sha256(source),
        "source_language": source_language,
        "target_language": target_language,
        "pages": pages,
        "segments_path": str(root / "segments.jsonl"),
        "translations_path": str(root / "translations.jsonl"),
        "collect_output_dir": str(root / "collect_output"),
        "render_output_dir": str(root / "render_output"),
        "qa_output_dir": str(root / "qa"),
        "rendered_pdf": None,
        "rendered_sha256": None,
    }
    save_manifest(root, manifest)
    return manifest


def _resolve_pdf2zh(executable: str | Path | None) -> str:
    if executable:
        return str(executable)
    resolved = shutil.which("pdf2zh_next")
    if resolved:
        return resolved

    # 直接执行 ``.venv/bin/python`` 不会自动把 ``.venv/bin`` 加入 PATH，
    # 但 pip 会把 pdf2zh_next 安装在该解释器旁边。检查这个确定位置可让
    # 已正确安装的隔离环境无需预先 activate，同时不跨环境猜测其他命令。
    sibling = Path(sys.executable).with_name("pdf2zh_next")
    if sibling.is_file():
        return str(sibling)
    raise FileNotFoundError(
        "未找到 pdf2zh_next；请安装兼容的 PDFMathTranslate-next/BabelDOC"
    )


def _bridge_command(*parts: str | Path) -> str:
    """用 shell 安全引用生成上游 ``--clitranslator-command`` 参数。"""

    return shlex.join([str(part) for part in parts])


def _common_layout_args(
    manifest: dict[str, object],
    *,
    pdf2zh_bin: str,
    output_dir: Path,
    bridge_command: str,
) -> list[str]:
    """收集和渲染共享同一版面参数，保证片段 ID 可以复现。"""

    command = [
        pdf2zh_bin,
        str(manifest["source_pdf"]),
        "--lang-in",
        str(manifest["source_language"]),
        "--lang-out",
        str(manifest["target_language"]),
        "--output",
        str(output_dir),
        "--qps",
        "1",
        "--pool-max-workers",
        "1",
        "--watermark-output-mode",
        "no_watermark",
        "--ignore-cache",
        "--no-dual",
        "--no-auto-extract-glossary",
        "--clitranslator",
        "--clitranslator-command",
        bridge_command,
        "--clitranslator-timeout",
        "180",
    ]
    if manifest.get("pages"):
        command.extend(["--pages", str(manifest["pages"])])
    return command


def _invoke(command: list[str], log_path: Path, timeout_seconds: int = 7200) -> None:
    """执行版面阶段并保存日志；非零退出时不更新运行状态。"""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    log_path.write_text(
        completed.stdout + "\n--- STDERR ---\n" + completed.stderr,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"版面阶段失败，exit={completed.returncode}；详见 {log_path}")


def collect_run(run_dir: Path, pdf2zh_bin: str | Path | None = None) -> None:
    """让版面引擎遍历 PDF 并收集稳定片段，不做任何真实翻译。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] != "initialized":
        raise ValueError(f"collect 只接受 initialized，当前为 {manifest['status']}")
    _verify_source_pdf(manifest)
    segments = Path(str(manifest["segments_path"]))
    write_target = Path(str(manifest["collect_output_dir"]))
    write_target.mkdir(parents=True, exist_ok=True)
    bridge = _bridge_command(
        sys.executable,
        "-m",
        "paperlocale.layout_bridge",
        "collect",
        "--segments",
        segments,
    )
    layout_executable = _resolve_pdf2zh(pdf2zh_bin)
    layout_provenance = _layout_provenance(layout_executable)
    command = _common_layout_args(
        manifest,
        pdf2zh_bin=layout_executable,
        output_dir=write_target,
        bridge_command=bridge,
    )
    _invoke(command, root / "logs" / "collect.log")
    rows = read_jsonl(segments)
    if not rows:
        raise RuntimeError("版面引擎成功退出但未收集到任何片段")
    manifest["segment_count"] = len(rows)
    manifest["layout_engine"] = layout_provenance
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["status"] = "collected"
    save_manifest(root, manifest)


def translate_run(
    *,
    run_dir: Path,
    provider: TranslationProvider,
    domain: DomainPack,
    max_segments: int = 200,
    max_characters: int = 30000,
    reference_policy: str = "preserve",
) -> tuple[int, int]:
    """翻译已收集片段，成功后把运行推进到 ``translated``。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] not in {"collected", "translated"}:
        raise ValueError(f"translate 不接受状态：{manifest['status']}")
    _verify_source_pdf(manifest)
    _verify_domain_languages(manifest, domain)
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
        raise ValueError(
            f"参考文献与透传映射不能包含相同片段：{sorted(overlap)}"
        )
    _prepare_segment_safety_configuration(root, manifest, passthrough_ids)
    provider_provenance = provider.provenance()
    recorded_provider = manifest.get("translation_provider")
    if isinstance(recorded_provider, dict) and recorded_provider != provider_provenance:
        raise ValueError(
            "Provider 配置与已有断点译文不一致："
            f"run={recorded_provider!r}, current={provider_provenance!r}"
        )
    # 在模型调用前绑定领域包和 Provider；即使本批只有部分译文通过，
    # 已原子保存的断点也不会变成来源不明的数据。
    manifest["domain_pack"] = _domain_provenance(domain)
    manifest["translation_provider"] = provider_provenance
    manifest["reference_policy"] = reference_policy
    manifest["reference_map"] = str(reference_map_path.resolve())
    manifest["reference_map_sha256"] = _sha256(reference_map_path)
    manifest["reference_segment_count"] = len(reference_ids)
    if passthrough_map_path is not None:
        manifest["passthrough_map"] = str(passthrough_map_path.resolve())
        manifest["passthrough_map_sha256"] = _sha256(passthrough_map_path)
    else:
        manifest.pop("passthrough_map", None)
        manifest.pop("passthrough_map_sha256", None)
    manifest["passthrough_segment_count"] = len(passthrough_ids)
    manifest["schema_version"] = SCHEMA_VERSION
    save_manifest(root, manifest)
    translations_path = Path(str(manifest["translations_path"]))
    rejected_path = translations_path.with_name("rejected_translations.jsonl")
    try:
        reused, translated = translate_segment_file(
            segments_path=Path(str(manifest["segments_path"])),
            translations_path=translations_path,
            provider=provider,
            domain=domain,
            max_segments=max_segments,
            max_characters=max_characters,
            reference_segment_ids=reference_ids,
            reference_policy=reference_policy,
            passthrough_segment_ids=passthrough_ids,
        )
    except Exception:
        # translate_segment_file 可能已经保存同批中通过门禁的译文；清单同步记录
        # 真实断点数量，但状态保持 collected，绝不把部分成功冒充为完成。
        manifest["translation_count"] = (
            len(read_jsonl(translations_path)) if translations_path.exists() else 0
        )
        if rejected_path.exists():
            manifest["rejected_translations"] = str(rejected_path.resolve())
        save_manifest(root, manifest)
        raise
    manifest["translation_count"] = reused + translated
    manifest.pop("rejected_translations", None)
    manifest["status"] = "translated"
    save_manifest(root, manifest)
    return reused, translated


def validate_run(run_dir: Path, domain: DomainPack) -> None:
    """重新全量验证断点译文，只有全部闭合才推进状态。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] not in {"translated", "validated"}:
        raise ValueError(f"validate 不接受状态：{manifest['status']}")
    _verify_source_pdf(manifest)
    _verify_domain_languages(manifest, domain)
    reference_ids, reference_policy = _recorded_reference_configuration(manifest)
    passthrough_ids = _recorded_passthrough_configuration(manifest)
    _recorded_segment_safety_configuration(manifest, passthrough_ids)
    validate_translation_files(
        Path(str(manifest["segments_path"])),
        Path(str(manifest["translations_path"])),
        domain,
        reference_segment_ids=reference_ids,
        reference_policy=reference_policy,
        passthrough_segment_ids=passthrough_ids,
    )
    manifest["validated_count"] = len(read_jsonl(Path(str(manifest["translations_path"]))))
    manifest["status"] = "validated"
    save_manifest(root, manifest)


def render_run(run_dir: Path, pdf2zh_bin: str | Path | None = None) -> Path:
    """用已经验证的译文查表重建 PDF，并要求输出候选唯一。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] not in {"validated", "rendered"}:
        raise ValueError(f"render 不接受状态：{manifest['status']}")
    _verify_source_pdf(manifest)
    translations = Path(str(manifest["translations_path"]))
    output_dir = Path(str(manifest["render_output_dir"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    bridge = _bridge_command(
        sys.executable,
        "-m",
        "paperlocale.layout_bridge",
        "lookup",
        "--translations",
        translations,
    )
    layout_executable = _resolve_pdf2zh(pdf2zh_bin)
    layout_provenance = _layout_provenance(layout_executable)
    recorded_layout = manifest.get("layout_engine")
    if isinstance(recorded_layout, dict):
        for field in ("pdf2zh_next_version", "babeldoc_version"):
            if recorded_layout.get(field) != layout_provenance[field]:
                raise ValueError(
                    f"collect 与 render 的版面引擎版本不一致：{field} "
                    f"run={recorded_layout.get(field)!r}, "
                    f"current={layout_provenance[field]!r}"
                )
    else:
        # 旧运行没有版面版本；重新 render 时从当前实际环境开始建立记录。
        manifest["layout_engine"] = layout_provenance
    command = _common_layout_args(
        manifest,
        pdf2zh_bin=layout_executable,
        output_dir=output_dir,
        bridge_command=bridge,
    )
    _invoke(command, root / "logs" / "render.log")
    candidates = sorted(output_dir.rglob("*.pdf"))
    if len(candidates) != 1:
        raise RuntimeError(f"渲染后应有且仅有一个 PDF，实际 {len(candidates)} 个")
    manifest["rendered_pdf"] = str(candidates[0].resolve())
    manifest["rendered_sha256"] = _sha256(candidates[0])
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["status"] = "rendered"
    save_manifest(root, manifest)
    return candidates[0].resolve()


def qa_run(
    run_dir: Path,
    *,
    dpi: int = 144,
    pdftoppm_bin: str | Path | None = None,
) -> dict[str, object]:
    """生成机器 QA 与逐页对照图；零机器错误后仍要求人工视觉批准。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] not in {"rendered", "qa_generated"}:
        raise ValueError(f"qa 不接受状态：{manifest['status']}")
    _verify_source_pdf(manifest)
    rendered = _verify_rendered_pdf(manifest, bind_legacy=True)
    report = inspect_pdf_pair(
        source_pdf=Path(str(manifest["source_pdf"])),
        translated_pdf=rendered,
        output_dir=Path(str(manifest["qa_output_dir"])),
        dpi=dpi,
        pdftoppm_bin=pdftoppm_bin,
    )
    if report["errors"]:
        raise RuntimeError(f"PDF 机器 QA 失败：{report['errors']}")
    if report.get("source_sha256") != manifest["source_sha256"]:
        raise RuntimeError("源 PDF 在 QA 读取期间发生变化")
    if report.get("translated_sha256") != manifest["rendered_sha256"]:
        raise RuntimeError("译文 PDF 在 QA 读取期间发生变化")
    manifest["qa_report"] = str(Path(str(manifest["qa_output_dir"])) / "qa_report.json")
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["status"] = "qa_generated"
    save_manifest(root, manifest)
    return report


def _verify_vector_repair(before: Path, candidate: Path) -> list[dict[str, int]]:
    """确认候选只增加矢量绘图，不改变页数、页面尺寸、文字或图片数量。"""

    before_document = fitz.open(before)
    candidate_document = fitz.open(candidate)
    changes: list[dict[str, int]] = []
    try:
        if before_document.page_count != candidate_document.page_count:
            raise ValueError("矢量修复候选改变了 PDF 页数")
        for index in range(before_document.page_count):
            before_page = before_document[index]
            candidate_page = candidate_document[index]
            if any(
                abs(left - right) > 0.1
                for left, right in zip(before_page.rect, candidate_page.rect)
            ):
                raise ValueError(f"矢量修复候选改变了第{index + 1}页尺寸")
            if before_page.get_text() != candidate_page.get_text():
                raise ValueError(f"矢量修复候选改变了第{index + 1}页文字")
            if len(before_page.get_images(full=True)) != len(
                candidate_page.get_images(full=True)
            ):
                raise ValueError(f"矢量修复候选改变了第{index + 1}页图片数量")
            before_count = len(before_page.get_drawings())
            candidate_count = len(candidate_page.get_drawings())
            if candidate_count < before_count:
                raise ValueError(f"矢量修复候选减少了第{index + 1}页矢量绘图")
            if candidate_count > before_count:
                changes.append(
                    {
                        "page": index + 1,
                        "before": before_count,
                        "after": candidate_count,
                    }
                )
    finally:
        candidate_document.close()
        before_document.close()
    if not changes:
        raise ValueError("矢量修复候选没有增加任何矢量绘图")
    return changes


def apply_vector_repair(
    run_dir: Path,
    *,
    repaired_pdf: Path,
    description: str,
) -> Path:
    """受控导入矢量修复候选，并在清单留下可回滚、可审计历史。"""

    if not description.strip():
        raise ValueError("description 不能为空")
    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] not in {"rendered", "qa_generated", "accepted"}:
        raise ValueError(
            "apply-vector-repair 只接受 rendered、qa_generated 或 accepted，"
            f"当前为 {manifest['status']}"
        )
    _verify_source_pdf(manifest)
    rendered = _verify_rendered_pdf(manifest)
    candidate = repaired_pdf.expanduser().resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"矢量修复候选不存在：{candidate}")
    if candidate == rendered:
        raise ValueError("矢量修复候选必须是独立文件，不能原地覆盖已绑定 PDF")

    vector_changes = _verify_vector_repair(rendered, candidate)
    before_hash = str(manifest["rendered_sha256"])
    after_hash = _sha256(candidate)
    if after_hash == before_hash:
        raise ValueError("矢量修复候选与当前 PDF 字节完全相同")
    history = manifest.setdefault("repair_history", [])
    if not isinstance(history, list):
        raise ValueError("运行清单 repair_history 字段非法")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = root / "repair_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{timestamp}-before-{before_hash[:12]}.pdf"
    shutil.copy2(rendered, backup)

    temporary = rendered.with_name(rendered.name + ".repair.tmp")
    shutil.copy2(candidate, temporary)
    temporary.replace(rendered)
    try:
        if _sha256(rendered) != after_hash:
            raise RuntimeError("矢量修复候选复制后哈希不一致")
        history.append(
            {
                "applied_at": _utc_now(),
                "type": "vector",
                "description": description.strip(),
                "candidate_pdf": str(candidate),
                "backup_pdf": str(backup.resolve()),
                "before_sha256": before_hash,
                "after_sha256": after_hash,
                "vector_changes": vector_changes,
            }
        )
        manifest["rendered_sha256"] = after_hash
        manifest["status"] = "rendered"
        manifest["schema_version"] = SCHEMA_VERSION
        manifest.pop("qa_report", None)
        manifest.pop("accepted_by", None)
        save_manifest(root, manifest)
    except Exception:
        # PDF 与清单必须共同成功；清单写入失败时用已生成的备份恢复旧候选。
        restore = rendered.with_name(rendered.name + ".restore.tmp")
        shutil.copy2(backup, restore)
        restore.replace(rendered)
        raise
    return rendered


def _normalized_pdf_text(value: str) -> str:
    """忽略 PDF 抽取产生的换行差异，但不改写实际文字内容。"""

    return re.sub(r"\s+", " ", value).strip()


def _outside_word_signature(
    page: fitz.Page,
    rectangle: fitz.Rect,
) -> list[tuple[object, ...]]:
    """记录修复矩形外文字及坐标，确保文字删除没有波及相邻正文。"""

    words: list[tuple[object, ...]] = []
    for word in page.get_text("words"):
        word_rectangle = fitz.Rect(word[:4])
        if word_rectangle.intersects(rectangle):
            continue
        words.append(
            (
                *(round(float(value), 2) for value in word[:4]),
                str(word[4]),
            )
        )
    return sorted(words)


def _subset_repair_font(
    font_file: Path,
    replacement: str,
) -> tuple[bytes, dict[str, object]]:
    """只为本次替换文字建立字体子集，不改写 PDF 中已有字体程序。"""

    original_font = fitz.Font(fontfile=str(font_file))
    missing = sorted(
        {
            character
            for character in replacement
            if not character.isspace()
            and original_font.has_glyph(ord(character)) == 0
        }
    )
    if missing:
        preview = "".join(missing[:12])
        raise ValueError(f"font-file 缺少 replacement 所需字形：{preview!r}")

    original_bytes = bytes(original_font.buffer)
    try:
        font = TTFont(
            str(font_file),
            fontNumber=0,
            recalcTimestamp=False,
        )
    except Exception as error:
        raise ValueError(f"font-file 无法由 FontTools 读取：{font_file}") from error
    try:
        options = fonttools_subset.Options()
        # 保留字体声明的全部排版特性，避免阿拉伯文等复杂文字在修复后失去字形替换。
        options.layout_features = ["*"]
        subsetter = fonttools_subset.Subsetter(options=options)
        subsetter.populate(text=replacement)
        subsetter.subset(font)
        output = BytesIO()
        font.save(output, reorderTables=True)
        subset_bytes = output.getvalue()
    except Exception as error:
        raise ValueError(f"font-file 无法为 replacement 生成子集：{font_file}") from error
    finally:
        font.close()

    if len(subset_bytes) >= len(original_bytes):
        raise RuntimeError(
            "字体子集没有减小嵌入程序；拒绝生成可能异常膨胀的修复 PDF"
        )
    subset_font = fitz.Font(fontbuffer=subset_bytes)
    subset_missing = sorted(
        {
            character
            for character in replacement
            if not character.isspace()
            and subset_font.has_glyph(ord(character)) == 0
        }
    )
    if subset_missing:
        preview = "".join(subset_missing[:12])
        raise RuntimeError(f"生成的字体子集缺少 replacement 字形：{preview!r}")

    font_sha256 = _sha256(font_file)
    subset_sha256 = hashlib.sha256(subset_bytes).hexdigest()
    return subset_bytes, {
        "font_file": str(font_file),
        "font_file_sha256": font_sha256,
        "font_name": original_font.name,
        "font_subset_sha256": subset_sha256,
        "font_resource_name": f"PLRepair{subset_sha256[:12]}",
        "font_program_bytes_before_subset": len(original_bytes),
        "font_program_bytes_after_subset": len(subset_bytes),
        "font_subset_bytes_saved": len(original_bytes) - len(subset_bytes),
    }


def _create_text_repair_candidate(
    *,
    rendered: Path,
    candidate: Path,
    page_number: int,
    rectangle: fitz.Rect,
    replacement: str,
    font_file: Path | None,
    font_size: float | None,
) -> dict[str, object]:
    """只在候选中删除指定文字，并在需要时嵌入子集替换字体。"""

    document = fitz.open(rendered)
    try:
        if not 1 <= page_number <= document.page_count:
            raise ValueError(
                f"page 超出范围：{page_number}，当前 PDF 共 {document.page_count} 页"
            )
        page = document[page_number - 1]
        if (
            not all(math.isfinite(float(value)) for value in rectangle)
            or not rectangle.is_valid
            or rectangle.is_empty
        ):
            raise ValueError("rect 必须是有限且非空的 PDF 点坐标矩形")
        if not page.rect.contains(rectangle):
            raise ValueError(f"rect 超出第{page_number}页边界：{tuple(rectangle)}")

        before_text = page.get_textbox(rectangle)
        if not _normalized_pdf_text(before_text):
            raise ValueError("指定 rect 内没有可替换文字")
        if _normalized_pdf_text(before_text) == _normalized_pdf_text(replacement):
            raise ValueError("replacement 与 rect 内现有文字相同")

        # apply_redactions 会处理页面上的全部 redaction；已有标注必须先由用户处置，
        # 否则一次图注修复可能意外执行无关删除。
        if list(page.annots(types=(fitz.PDF_ANNOT_REDACT,)) or []):
            raise ValueError(f"第{page_number}页已有 redaction 标注，拒绝批量应用")

        if replacement:
            if font_file is None or font_size is None:
                raise ValueError("非空 replacement 必须提供 font-file 和 font-size")
            subset_bytes, font_evidence = _subset_repair_font(font_file, replacement)
            font_resource_name = str(font_evidence["font_resource_name"])
        else:
            # 只删除模式不嵌入任何字体程序。保留显式空字段，
            # 使 repair_history 能区分“没有记录”与“经审计的文字删除”。
            subset_bytes = b""
            font_resource_name = ""
            font_evidence = {
                "font_file": None,
                "font_file_sha256": None,
                "font_name": None,
                "font_subset_sha256": None,
                "font_resource_name": None,
                "font_program_bytes_before_subset": 0,
                "font_program_bytes_after_subset": 0,
                "font_subset_bytes_saved": 0,
            }
        # redaction 只负责移除文字；透明填充避免白色覆盖层遮住矩形内仍保留的
        # 图片或矢量对象。下面的 images/graphics NONE 则禁止删除这些对象本身。
        page.add_redact_annot(rectangle, fill=False, cross_out=False)
        applied = page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        if not applied:
            raise RuntimeError("PyMuPDF 没有应用文字修复 redaction")

        if replacement:
            font_xref = page.insert_font(
                fontname=font_resource_name,
                fontbuffer=subset_bytes,
            )
            remaining_height = page.insert_textbox(
                rectangle,
                replacement,
                fontname=font_resource_name,
                fontsize=font_size,
                color=(0, 0, 0),
            )
            if remaining_height < 0:
                raise ValueError(
                    "replacement 无法放入 rect；请扩大矩形或减小 --font-size"
                )

            embedded_font = document.extract_font(font_xref)[-1]
            if hashlib.sha256(embedded_font).hexdigest() != font_evidence[
                "font_subset_sha256"
            ]:
                raise RuntimeError("嵌入的修复字体与已验证子集哈希不一致")
        document.save(candidate, garbage=4, deflate=True)
    finally:
        document.close()

    return {
        "before_text": before_text,
        **font_evidence,
    }


def _verify_text_repair(
    *,
    before: Path,
    candidate: Path,
    page_number: int,
    rectangle: fitz.Rect,
    replacement: str,
) -> dict[str, object]:
    """确认候选只改变目标矩形文字，并保持页面、图片、链接和既有矢量。"""

    before_document = fitz.open(before)
    candidate_document = fitz.open(candidate)
    try:
        if before_document.page_count != candidate_document.page_count:
            raise ValueError("文字修复候选改变了 PDF 页数")
        for index in range(before_document.page_count):
            before_page = before_document[index]
            candidate_page = candidate_document[index]
            if any(
                abs(left - right) > 0.1
                for left, right in zip(before_page.rect, candidate_page.rect)
            ):
                raise ValueError(f"文字修复候选改变了第{index + 1}页尺寸")
            if len(before_page.get_images(full=True)) != len(
                candidate_page.get_images(full=True)
            ):
                raise ValueError(f"文字修复候选改变了第{index + 1}页图片数量")
            if len(before_page.get_links()) != len(candidate_page.get_links()):
                raise ValueError(f"文字修复候选改变了第{index + 1}页链接数量")
            if len(list(before_page.annots() or [])) != len(
                list(candidate_page.annots() or [])
            ):
                raise ValueError(f"文字修复候选改变了第{index + 1}页标注数量")
            if len(candidate_page.get_drawings()) < len(before_page.get_drawings()):
                raise ValueError(f"文字修复候选减少了第{index + 1}页矢量绘图")
            if (
                index != page_number - 1
                and before_page.get_text() != candidate_page.get_text()
            ):
                raise ValueError(f"文字修复候选改变了非目标第{index + 1}页文字")

        before_page = before_document[page_number - 1]
        candidate_page = candidate_document[page_number - 1]
        if _outside_word_signature(before_page, rectangle) != _outside_word_signature(
            candidate_page, rectangle
        ):
            raise ValueError("文字修复候选改变了 rect 外的文字或坐标")
        after_text = candidate_page.get_textbox(rectangle)
        if _normalized_pdf_text(after_text) != _normalized_pdf_text(replacement):
            raise ValueError(
                "修复后 rect 文字与 replacement 不一致："
                f"{after_text!r} != {replacement!r}"
            )
        return {
            "after_text": after_text,
            "image_count": len(candidate_page.get_images(full=True)),
            "vector_count_before": len(before_page.get_drawings()),
            "vector_count_after": len(candidate_page.get_drawings()),
            "link_count": len(candidate_page.get_links()),
            "annotation_count": len(list(candidate_page.annots() or [])),
        }
    finally:
        candidate_document.close()
        before_document.close()


def apply_text_repair(
    run_dir: Path,
    *,
    page_number: int,
    rectangle: tuple[float, float, float, float],
    replacement: str,
    font_file: Path | None,
    font_size: float | None,
    description: str,
) -> Path:
    """在一个明确矩形内受控替换文字，并强制重新执行机器与人工 QA。"""

    removal_only = replacement == ""
    if not removal_only and not replacement.strip():
        raise ValueError("replacement 不能只包含空白字符")
    if not description.strip():
        raise ValueError("description 不能为空")
    if not removal_only:
        if font_file is None or font_size is None:
            raise ValueError("非空 replacement 必须提供 font-file 和 font-size")
        if not math.isfinite(font_size) or font_size <= 0:
            raise ValueError("font-size 必须是有限且大于 0 的数值")

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] not in {"rendered", "qa_generated", "accepted"}:
        raise ValueError(
            "apply-text-repair 只接受 rendered、qa_generated 或 accepted，"
            f"当前为 {manifest['status']}"
        )
    _verify_source_pdf(manifest)
    rendered = _verify_rendered_pdf(manifest)
    resolved_font = font_file.expanduser().resolve() if font_file else None
    if resolved_font is not None and not resolved_font.is_file():
        raise FileNotFoundError(f"font-file 不存在：{resolved_font}")

    repair_rectangle = fitz.Rect(rectangle)
    before_hash = str(manifest["rendered_sha256"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = root / f".text-repair-{timestamp}-{before_hash[:12]}.pdf"
    try:
        repair = _create_text_repair_candidate(
            rendered=rendered,
            candidate=candidate,
            page_number=page_number,
            rectangle=repair_rectangle,
            replacement=replacement,
            font_file=resolved_font,
            font_size=font_size,
        )
        structure = _verify_text_repair(
            before=rendered,
            candidate=candidate,
            page_number=page_number,
            rectangle=repair_rectangle,
            replacement=replacement,
        )
        after_hash = _sha256(candidate)
        if after_hash == before_hash:
            raise RuntimeError("文字修复候选与当前 PDF 字节完全相同")

        history = manifest.setdefault("repair_history", [])
        if not isinstance(history, list):
            raise ValueError("运行清单 repair_history 字段非法")
        backup_dir = root / "repair_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{timestamp}-before-{before_hash[:12]}.pdf"
        shutil.copy2(rendered, backup)

        temporary = rendered.with_name(rendered.name + ".text-repair.tmp")
        shutil.copy2(candidate, temporary)
        temporary.replace(rendered)
        try:
            if _sha256(rendered) != after_hash:
                raise RuntimeError("文字修复候选复制后哈希不一致")
            history.append(
                {
                    "applied_at": _utc_now(),
                    "type": "text-removal" if removal_only else "text-overlay",
                    "description": description.strip(),
                    "page": page_number,
                    "rect": [round(float(value), 3) for value in repair_rectangle],
                    "replacement": replacement,
                    "font_size": font_size,
                    "backup_pdf": str(backup.resolve()),
                    "before_sha256": before_hash,
                    "after_sha256": after_hash,
                    **repair,
                    **structure,
                }
            )
            manifest["rendered_sha256"] = after_hash
            manifest["status"] = "rendered"
            manifest["schema_version"] = SCHEMA_VERSION
            manifest.pop("qa_report", None)
            manifest.pop("accepted_by", None)
            save_manifest(root, manifest)
        except Exception:
            # PDF 与清单必须共同成功；清单写入失败时恢复已经备份的旧候选。
            restore = rendered.with_name(rendered.name + ".restore.tmp")
            shutil.copy2(backup, restore)
            restore.replace(rendered)
            raise
        return rendered
    finally:
        candidate.unlink(missing_ok=True)


def run_to_qa(
    *,
    run_dir: Path,
    provider: TranslationProvider | None,
    domain: DomainPack,
    pdf2zh_bin: str | Path | None = None,
    dpi: int = 144,
    pdftoppm_bin: str | Path | None = None,
    reference_policy: str = "preserve",
    max_segments: int = 200,
    max_characters: int = 30000,
) -> dict[str, object]:
    """从当前断点沿唯一生产路径推进到机器 QA，保留人工验收边界。

    每个阶段仍由原有阶段函数完成并原子更新清单；这里仅按清单状态依次调用，
    不复制翻译、验证或渲染逻辑。已经完成翻译的运行不再要求 Provider，方便
    在模型额度或登录环境不可用时继续执行验证、重建与 QA。
    """

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] == "initialized":
        collect_run(root, pdf2zh_bin)
        manifest = load_manifest(root)
    if manifest["status"] == "collected":
        if provider is None:
            raise ValueError("运行尚未翻译；请提供 --provider 后重试")
        translate_run(
            run_dir=root,
            provider=provider,
            domain=domain,
            max_segments=max_segments,
            max_characters=max_characters,
            reference_policy=reference_policy,
        )
        manifest = load_manifest(root)
    if manifest["status"] == "translated":
        validate_run(root, domain)
        manifest = load_manifest(root)
    if manifest["status"] == "validated":
        render_run(root, pdf2zh_bin)
        manifest = load_manifest(root)
    if manifest["status"] == "rendered":
        qa_run(root, dpi=dpi, pdftoppm_bin=pdftoppm_bin)
        manifest = load_manifest(root)

    if manifest["status"] in {"qa_generated", "accepted"}:
        # 无需执行新阶段时仍核对产物身份，避免把已被替换的 PDF 报告为可验收。
        _verify_source_pdf(manifest)
        _verify_rendered_pdf(manifest)
    return manifest


def accept_run(run_dir: Path, *, reviewed_by: str) -> None:
    """记录人工逐页验收；必须显式提供复核者，不允许自动批准。"""

    if not reviewed_by.strip():
        raise ValueError("reviewed_by 不能为空")
    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] != "qa_generated":
        raise ValueError(f"accept 只接受 qa_generated，当前为 {manifest['status']}")
    source = _verify_source_pdf(manifest)
    rendered = _verify_rendered_pdf(manifest)
    report_path = Path(str(manifest.get("qa_report", "")))
    if not report_path.is_file():
        raise FileNotFoundError("QA 报告不存在")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("errors"):
        raise ValueError("QA 报告仍包含机器错误，不能人工批准")
    if Path(str(report.get("source_pdf", ""))).resolve() != source:
        raise ValueError("QA 报告不属于当前源 PDF")
    if Path(str(report.get("translated_pdf", ""))).resolve() != rendered:
        raise ValueError("QA 报告不属于当前译文 PDF")
    if report.get("source_sha256") != manifest["source_sha256"]:
        raise ValueError("QA 报告的源 PDF 哈希不一致")
    if report.get("translated_sha256") != manifest["rendered_sha256"]:
        raise ValueError("QA 报告的译文 PDF 哈希不一致")
    report["visual_accepted"] = True
    report["visual_reviewed_by"] = reviewed_by.strip()
    report["visual_reviewed_at"] = _utc_now()
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report_path)
    manifest["status"] = "accepted"
    manifest["accepted_by"] = reviewed_by.strip()
    save_manifest(root, manifest)
