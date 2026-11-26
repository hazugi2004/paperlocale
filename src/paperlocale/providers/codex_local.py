"""使用用户本机 Codex 登录态的本地翻译 Provider。

本模块只调用 ``codex exec``。它不会读取、复制或解析 ``~/.codex/auth.json``，
也不把 ChatGPT 订阅登录暴露为网络服务。调用固定使用只读沙箱和临时会话。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
    _keyed_request,
    _parse_keyed_response,
)

REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_ROUTING_FAILURE = {"space routing discovery failed", "ERROR: space routing discovery failed"}


def _run_codex(
    command: list[str], *, timeout: float, input: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Own one CLI process group so failed calls cannot overlap their retries.

    macOS/Linux descendants inherit this session's process group. No global
    process lookup or PID tree traversal is needed. Detached sessions are outside
    this boundary; Codex must not daemonize its work. Other platforms need an
    equivalent containment mechanism before they can safely run this provider.
    """
    if os.name != "posix":
        raise NotImplementedError("Codex 本地 Provider 的安全进程组管理目前仅支持 macOS/Linux（POSIX）")
    with subprocess.Popen(
        command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", cwd=cwd, start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException:
            # Kill the group even when the leader has exited: descendants may
            # still hold its stdout/stderr pipes open. Never use our own group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Reap the child we own. Orphaned descendants are reaped by the OS;
            # do not wait on inherited pipes that a detached process may hold.
            process.wait()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def resolve_codex(codex_bin: str | Path | None) -> str:
    """显式路径 > PATH > macOS 常见用户安装位置；不修改系统符号链接。

    Finder 的 PATH 不含 .local/bin，本次还遇到已失效的 codex 软链。
    仅在未指定且 PATH 没有可执行命令时探测本机 app；绝不为模型报错
    自动换 CLI 或模型。显式路径仍由 subprocess 报告其真实启动错误。
    """
    if codex_bin:
        selected = os.path.expanduser(str(codex_bin))
        # Translation runs in a temporary cwd; bind explicit relative paths now.
        # A bare executable name still uses normal PATH lookup.
        return str(Path(selected).resolve()) if os.path.dirname(selected) else selected
    resolved = shutil.which("codex")
    if resolved:
        return resolved
    if sys.platform == "darwin":
        candidates = [Path.home() / ".local/bin/codex"]
        for base in (Path("/Applications"), Path.home() / "Applications"):
            candidates.extend(base / app / "Contents/Resources/codex"
                              for app in ("Codex.app", "ChatGPT.app"))
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    raise FileNotFoundError("未找到可执行的 codex（请检查软链接是否失效）；请先安装 Codex CLI 并执行 codex login，或使用 --codex-bin 指定")


class CodexLocalProvider(TranslationProvider):
    """通过官方 Codex CLI 的结构化非交互模式翻译一个批次。"""

    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
        codex_bin: str | Path | None = None,
        timeout_seconds: int = 1800,
    ) -> None:
        self.codex_bin = resolve_codex(codex_bin)
        self.model = model
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                "reasoning_effort 必须是 " + ", ".join(REASONING_EFFORTS)
            )
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds

    def provenance(self) -> dict[str, object]:
        """读取实际 Codex CLI 版本，不把登录信息或用户配置写入清单。"""

        completed = _run_codex(
            [self.codex_bin, "--version"],
            timeout=30,
        )
        version = (completed.stdout.strip() or completed.stderr.strip()).splitlines()
        if completed.returncode != 0 or not version:
            raise RuntimeError(
                f"无法读取 Codex CLI 版本，exit={completed.returncode}"
            )
        return {
            "provider": "codex-local",
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "codex_cli_version": version[-1].strip(),
        }

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        prompt, schema = _keyed_request(segments, context)
        with tempfile.TemporaryDirectory(prefix="paperlocale-codex-") as directory:
            root = Path(directory)
            schema_path = root / "translation-schema.json"
            output_path = root / "translation-output.json"
            schema_path.write_text(
                json.dumps(schema, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            command = [
                self.codex_bin,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if self.model:
                command.extend(["--model", self.model])
            if self.reasoning_effort:
                # ``-c`` 接收 TOML 值；JSON 字符串同时也是合法 TOML 字符串，
                # 可避免手工拼接引号或把用户输入解释成新的配置表达式。
                command.extend(
                    [
                        "-c",
                        "model_reasoning_effort=" + json.dumps(self.reasoning_effort),
                    ]
                )
            command.append("-")
            completed = _run_codex(
                command,
                input=prompt,
                timeout=self.timeout_seconds,
                cwd=root,
            )
            # 路由发现发生于模型响应之前。只在已知的终止诊断且尚无输出文件
            # 时重开一次同一批次，覆盖 App 关闭外层等待的正常启动方式。
            if (completed.returncode == 1 and not output_path.exists()
                    and _ROUTING_FAILURE.intersection(completed.stderr.strip().splitlines())):
                logging.getLogger(__name__).warning("Codex space routing discovery failed; retrying batch once.")
                completed = _run_codex(command, input=prompt, timeout=self.timeout_seconds, cwd=root)
            if completed.returncode != 0:
                diagnostic = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(
                    f"Codex 翻译失败，exit={completed.returncode}：{diagnostic[-2000:]}"
                )
            if not output_path.is_file():
                raise RuntimeError("Codex 成功退出但没有生成结构化译文文件")
            return _parse_keyed_response(output_path.read_text(encoding="utf-8"), segments)
