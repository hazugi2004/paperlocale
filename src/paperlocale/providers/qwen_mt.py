"""通过阿里云百炼 OpenAI 兼容接口调用 Qwen-MT 专用翻译模型。"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..contracts import FORMULA_RE, STYLE_RE, protected_counts
from .base import Segment, Translation, TranslationContext, TranslationProvider


class QwenMTProvider(TranslationProvider):
    """逐片段调用 Qwen-MT，避免把控制提示误当成待翻译正文。"""

    # Qwen-MT 的官方语义是单条 user message。把上限公开给流水线，
    # 可以保证每条成功译文都立即进入原子检查点。
    max_batch_segments = 1

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 300,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("Qwen-MT base_url 必须是 HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Qwen-MT base_url 不得包含用户名或密码")
        if parsed.query or parsed.fragment:
            raise ValueError("Qwen-MT base_url 不得包含查询参数或片段")
        if parsed.path.rstrip("/").endswith("/chat/completions"):
            raise ValueError("Qwen-MT base_url 不得包含 /chat/completions")
        if not api_key:
            raise ValueError("api_key 不能为空")
        if not model:
            raise ValueError("model 不能为空")
        self.base_url = base_url.rstrip("/")
        self.endpoint = self.base_url + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def provenance(self) -> dict[str, object]:
        """只记录可审计的模型和端点身份，不记录 API Key。"""

        return {
            "provider": "qwen-mt",
            "model": self.model,
            "base_url": self.base_url,
        }

    @staticmethod
    def _language_code(language: str) -> str:
        """把 PaperLocale 语言标签转为 Qwen-MT 接口使用的代码。"""

        aliases = {"zh-CN": "zh", "zh-TW": "zh_tw"}
        return aliases.get(language, language.split("-", 1)[0].lower())

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        """按官方单轮语义逐条翻译，并由调用方继续执行完整性门禁。"""

        if len(segments) != 1:
            raise ValueError("Qwen-MT Provider 每次必须且只能翻译一个片段")
        translations: list[Translation] = []
        for segment in segments:
            # 专用翻译模型有时会把纯占位符视为无语义噪声并删除。先替换成可读的
            # ASCII 哨兵，响应返回后再逐一恢复；原始片段与最终译文仍由合同核对。
            # BabelDOC 有时把变量写成 ``{v2}HDI-P{v3}``。若只保护两侧
            # 占位符，Qwen 可能保留前者却省略变量和后者；因此优先把整个
            # “占位符+缩写+占位符”作为一个不可拆分对象，再处理剩余标记。
            annotated_abbreviation_re = re.compile(
                r"(?:\{v\d+\}[A-Z][A-Z0-9/-]+(?:\{v\d+\})?"
                r"|[A-Z][A-Z0-9/-]+\{v\d+\})"
            )
            formula_expression_re = re.compile(r"[A-Z][A-Z0-9/-]*\s+\{v\d+\}[.:]?\d+")
            formula_expressions = formula_expression_re.findall(segment.source)
            expression_placeholders = {
                placeholder
                for expression in formula_expressions
                for placeholder in FORMULA_RE.findall(expression)
            }
            protected_markup = (
                annotated_abbreviation_re.findall(segment.source)
                + [
                    placeholder
                    for placeholder in FORMULA_RE.findall(segment.source)
                    if placeholder not in expression_placeholders
                ]
                + STYLE_RE.findall(segment.source)
            )
            source_for_translation = segment.source
            sentinels: dict[str, str] = {}
            for index, value in enumerate(dict.fromkeys(protected_markup), 1):
                # 方括号引用形态会被 Qwen-MT 当作需保留的文内标记；实测纯字母
                # 哨兵仍可能被摘要式重写删除。
                sentinel = f"[PLPROTECTED{index:04d}]"
                sentinels[sentinel] = value
                # 两侧空格防止哨兵与 ``HDI-P`` 等相邻缩写粘连后被模型吞并。
                source_for_translation = source_for_translation.replace(
                    value,
                    f" {sentinel} ",
                )

            # 官方术语干预同时承担两项职责：固定领域译法，并强制模型原样保留
            # 公式占位符、缩写、单位、URL 与 DOI。重复源词只保留第一条映射。
            terms_by_source: dict[str, str] = {}
            source_folded = segment.source.casefold()
            for entry in context.domain.glossary:
                if entry.source.casefold() in source_folded:
                    terms_by_source.setdefault(entry.source, entry.target)
            counts = protected_counts(segment.source)
            for category in ("url", "doi", "number", "abbreviation", "unit"):
                for value in counts[category]:
                    terms_by_source.setdefault(value, value)
            for expression in formula_expressions:
                # Qwen-MT 对有意义的完整公式术语可稳定原样保留；若将其中占位符
                # 单独替换为哨兵，模型反而可能删掉整个括号表达式。
                terms_by_source.setdefault(expression, expression)
            for sentinel in sentinels:
                terms_by_source.setdefault(sentinel, sentinel)

            # Qwen-MT 不支持 system message；领域说明必须放在 translation_options。
            body = {
                "model": self.model,
                "messages": [{"role": "user", "content": source_for_translation}],
                "translation_options": {
                    "source_lang": self._language_code(context.source_language),
                    "target_lang": self._language_code(context.target_language),
                    # 领域说明来自当前 DomainPack，Provider 不再内置任何
                    # 学科、论文或目标语言特例。
                    "domains": context.domain.prompt,
                    "terms": [
                        {"source": source, "target": target}
                        for source, target in terms_by_source.items()
                    ],
                },
            }
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(
                        request, timeout=self.timeout_seconds
                    ) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Qwen-MT 接口返回 HTTP {exc.code}：{detail[-2000:]}"
                    ) from exc
                except (urllib.error.URLError, http.client.RemoteDisconnected) as exc:
                    # 实际长文运行中观察到代理端口耗尽和远端临时断连；仅对这两类
                    # 连接失败做两次短重试，HTTP 业务错误仍立即失败并保留原诊断。
                    if attempt == 2:
                        reason = getattr(exc, "reason", str(exc))
                        raise RuntimeError(f"无法连接 Qwen-MT 接口：{reason}") from exc
                    time.sleep(2)

            try:
                target = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError("Qwen-MT 响应缺少 choices[0].message.content") from exc
            if not isinstance(target, str):
                raise TypeError("Qwen-MT 返回的 message.content 不是字符串")
            for sentinel, value in sentinels.items():
                if sentinel not in target:
                    raise ValueError(f"Qwen-MT 删除了保护标记：{sentinel}")
                target = target.replace(sentinel, value)
            # Qwen 常把指标名中的 ASCII 连字符排成不换行连字符；指标身份要求
            # ``HDI-P`` 等标记逐字保留，因此只在两侧都是 ASCII 字母或数字时还原。
            target = re.sub(
                r"(?<=[A-Za-z0-9])[\u2010\u2011](?=[A-Za-z0-9])",
                "-",
                target,
            )
            translations.append(Translation(id=segment.id, target=target.strip()))
        return translations
