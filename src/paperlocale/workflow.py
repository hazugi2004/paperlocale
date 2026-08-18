"""PDF 翻译运行清单和单向状态机。

每个阶段都先检查前置状态，再执行唯一外部命令，成功后才原子更新清单。
网络、模型或版面引擎失败时，既有片段和译文仍可用于断点续跑。
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .contracts import read_jsonl, validate_translation_files
from .domains import DomainPack
from .pipeline import translate_segment_file
from .providers import TranslationProvider
from .qa import inspect_pdf_pair

STATES = (
    "initialized",
    "collected",
    "translated",
    "validated",
    "rendered",
    "qa_generated",
    "accepted",
)


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
        manifest["schema_version"] = 2
        return path
    if actual != expected:
        raise ValueError("译文 PDF 在 render 后发生变化；请重新执行 render 和 qa")
    return path


def _verify_domain_languages(
    manifest: dict[str, object],
    domain: DomainPack,
) -> None:
    """拒绝运行语言与领域包不一致，避免提示和术语规则被错误套用。"""

    expected = (str(manifest["source_language"]), str(manifest["target_language"]))
    actual = (domain.source_language, domain.target_language)
    if actual != expected:
        raise ValueError(
            "领域包语言与运行不一致："
            f"run={expected[0]}->{expected[1]}, domain={actual[0]}->{actual[1]}"
        )


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
        "schema_version": 2,
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
    command = _common_layout_args(
        manifest,
        pdf2zh_bin=_resolve_pdf2zh(pdf2zh_bin),
        output_dir=write_target,
        bridge_command=bridge,
    )
    _invoke(command, root / "logs" / "collect.log")
    rows = read_jsonl(segments)
    if not rows:
        raise RuntimeError("版面引擎成功退出但未收集到任何片段")
    manifest["segment_count"] = len(rows)
    manifest["status"] = "collected"
    save_manifest(root, manifest)


def translate_run(
    *,
    run_dir: Path,
    provider: TranslationProvider,
    domain: DomainPack,
    max_segments: int = 200,
    max_characters: int = 30000,
) -> tuple[int, int]:
    """翻译已收集片段，成功后把运行推进到 ``translated``。"""

    root = run_dir.expanduser().resolve()
    manifest = load_manifest(root)
    if manifest["status"] not in {"collected", "translated"}:
        raise ValueError(f"translate 不接受状态：{manifest['status']}")
    _verify_source_pdf(manifest)
    _verify_domain_languages(manifest, domain)
    reused, translated = translate_segment_file(
        segments_path=Path(str(manifest["segments_path"])),
        translations_path=Path(str(manifest["translations_path"])),
        provider=provider,
        domain=domain,
        max_segments=max_segments,
        max_characters=max_characters,
    )
    manifest["domain_pack"] = {"id": domain.pack_id, "version": domain.version}
    manifest["translation_count"] = reused + translated
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
    validate_translation_files(
        Path(str(manifest["segments_path"])),
        Path(str(manifest["translations_path"])),
        domain,
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
    command = _common_layout_args(
        manifest,
        pdf2zh_bin=_resolve_pdf2zh(pdf2zh_bin),
        output_dir=output_dir,
        bridge_command=bridge,
    )
    _invoke(command, root / "logs" / "render.log")
    candidates = sorted(output_dir.rglob("*.pdf"))
    if len(candidates) != 1:
        raise RuntimeError(f"渲染后应有且仅有一个 PDF，实际 {len(candidates)} 个")
    manifest["rendered_pdf"] = str(candidates[0].resolve())
    manifest["rendered_sha256"] = _sha256(candidates[0])
    manifest["schema_version"] = 2
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
    manifest["schema_version"] = 2
    manifest["status"] = "qa_generated"
    save_manifest(root, manifest)
    return report


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
