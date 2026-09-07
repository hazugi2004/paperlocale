"""使用用户本机 Codex 登录态的本地翻译 Provider。

本模块只调用 ``codex exec``。它不会读取、复制或解析 ``~/.codex/auth.json``，
也不把 ChatGPT 订阅登录暴露为网络服务。调用固定使用只读沙箱和临时会话。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from .base import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
    build_prompt,
)


def _keyed_request(
    segments: list[Segment], context: TranslationContext,
) -> tuple[str, dict[str, object]]:
    """只让模型填充短键对应的译文，稳定片段哈希始终留在本地。

    s1、s2 等键仅在当前批次有效；每个键都是 schema 中的必填属性，且不允许
    额外属性。参考文献标记与合同修复反馈按同一映射转移，不修改原 context。
    磁盘上的片段 ID、缓存、内容验证和其它 Provider 接口继续使用原始哈希。
    """

    ids = [segment.id for segment in segments]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Codex 输入批次不能为空或包含重复 ID")
    aliases = {sid: f"s{index}" for index, sid in enumerate(ids, 1)}
    wire_segments = [Segment(aliases[s.id], s.source) for s in segments]
    wire_context = replace(
        context,
        reference_segment_ids=frozenset(aliases[sid] for sid in ids
                                        if sid in context.reference_segment_ids),
        repair_feedback={aliases[sid]: context.repair_feedback[sid] for sid in ids
                         if sid in context.repair_feedback},
    )
    schema = {
        "type": "object",
        "properties": {"translations": {
            "type": "object",
            "properties": {key: {"type": "string"} for key in aliases.values()},
            "required": list(aliases.values()),
            "additionalProperties": False,
        }},
        "required": ["translations"],
        "additionalProperties": False,
    }
    prompt = build_prompt(wire_segments, wire_context) + (
        '\n输出 translations 必须是对象：键为输入的 s1、s2 等短 ID，'
        '值为对应的完整译文字符串。不要返回数组或 id/target 子对象。\n'
    )
    return prompt, schema


def _parse_keyed_response(text: str, segments: list[Segment]) -> list[Translation]:
    """验证短键闭合后恢复本地哈希；不按响应顺序配对，也不做模糊 ID 修补。"""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Codex 输出重复键：{key}")
            result[key] = value
        return result

    # 默认 json.loads 会静默覆盖重复对象键；这里必须在解析时拒绝，避免错配。
    payload = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(payload, dict) or set(payload) != {"translations"}:
        raise ValueError("Codex 输出必须且只能包含 translations 对象")
    targets = payload["translations"]
    expected = {f"s{index}" for index in range(1, len(segments) + 1)}
    if not isinstance(targets, dict) or set(targets) != expected:
        raise ValueError("Codex 输出短键集合不闭合；拒绝猜测译文与原文的对应关系")
    if any(not isinstance(target, str) for target in targets.values()):
        raise ValueError("Codex 输出的每条译文必须是字符串")
    return [Translation(segment.id, targets[f"s{index}"])
            for index, segment in enumerate(segments, 1)]

REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


class CodexLocalProvider(TranslationProvider):
    """通过官方 Codex CLI 的结构化非交互模式翻译一个批次。"""

    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
        codex_bin: str | Path | None = None,
        timeout_seconds: int = 1800,
    ) -> None:
        resolved = str(codex_bin) if codex_bin else shutil.which("codex")
        if not resolved:
            raise FileNotFoundError("未找到 codex；请先安装 Codex CLI 并执行 codex login")
        self.codex_bin = resolved
        self.model = model
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                "reasoning_effort 必须是 " + ", ".join(REASONING_EFFORTS)
            )
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds

    def provenance(self) -> dict[str, object]:
        """读取实际 Codex CLI 版本，不把登录信息或用户配置写入清单。"""

        completed = subprocess.run(
            [self.codex_bin, "--version"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=False,
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
            return _parse_keyed_response(output_path.read_text(encoding="utf-8"), segments)
