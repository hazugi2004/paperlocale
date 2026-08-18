"""使用用户本机 Codex 登录态的本地翻译 Provider。

本模块只调用 ``codex exec``。它不会读取、复制或解析 ``~/.codex/auth.json``，
也不把 ChatGPT 订阅登录暴露为网络服务。调用固定使用只读沙箱和临时会话。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
    build_prompt,
    output_schema,
    parse_payload,
)


class CodexLocalProvider(TranslationProvider):
    """通过官方 Codex CLI 的结构化非交互模式翻译一个批次。"""

    def __init__(
        self,
        model: str | None = None,
        codex_bin: str | Path | None = None,
        timeout_seconds: int = 1800,
    ) -> None:
        resolved = str(codex_bin) if codex_bin else shutil.which("codex")
        if not resolved:
            raise FileNotFoundError("未找到 codex；请先安装 Codex CLI 并执行 codex login")
        self.codex_bin = resolved
        self.model = model
        self.timeout_seconds = timeout_seconds

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        prompt = build_prompt(segments, context)
        with tempfile.TemporaryDirectory(prefix="paperlocale-codex-") as directory:
            root = Path(directory)
            schema_path = root / "translation-schema.json"
            output_path = root / "translation-output.json"
            schema_path.write_text(
                json.dumps(output_schema(), ensure_ascii=False, indent=2),
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
            command.append("-")
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                cwd=root,
            )
            if completed.returncode != 0:
                diagnostic = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(
                    f"Codex 翻译失败，exit={completed.returncode}：{diagnostic[-2000:]}"
                )
            if not output_path.is_file():
                raise RuntimeError("Codex 成功退出但没有生成结构化译文文件")
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        return parse_payload(payload, segments)
