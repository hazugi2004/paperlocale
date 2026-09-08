"""通过阿里云百炼 OpenAI 兼容接口调用 Qwen-MT 专用翻译模型。"""

from __future__ import annotations

import http.client
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..contracts import ABBREVIATION_RE, NUMBER_RE, FORMULA_RE, STYLE_RE, protected_counts, source_term_is_present, validate_translation
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
        min_request_interval_seconds: float = 1.1,
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
        if not math.isfinite(min_request_interval_seconds) or min_request_interval_seconds < 0:
            raise ValueError("请求最小间隔必须是非负有限数")
        self.min_request_interval_seconds = min_request_interval_seconds
        self._next_request_at = 0.0
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
                formula_expressions
                + annotated_abbreviation_re.findall(segment.source)
                + [
                    placeholder
                    for placeholder in FORMULA_RE.findall(segment.source)
                    if placeholder not in expression_placeholders
                ]
                + STYLE_RE.findall(segment.source)
            )
            counts = protected_counts(segment.source)
            sentinels: dict[str, str] = {}
            # 与原文里的字面标记隔离；每次出现都使用独立标识，避免重复缩写
            # 被专用翻译模型合并后仍被误判为完整。只做原位置替换，禁止事后补词。
            prefix = "PLPROTECTED"
            while prefix in segment.source:
                prefix += "X"

            def shield(value: str) -> str:
                sentinel = f"[{prefix}{len(sentinels) + 1:04d}]"
                sentinels[sentinel] = value
                return f" {sentinel} "

            # URL 内部可以有公式占位符；在点/斜线之后的版面空格，仅当后方
            # 仍是带点/斜线的地址片段时一起保护。保留源字符串，不猜测或修复网址。
            # 先保护完整地址，再处理其余公式，避免嵌套哨兵或只保护域名前缀。
            url_pattern = re.compile(
                r"https?://(?:\{v\d+\}|[^\s)\]}>;,，。；：！？）】》\u3400-\u9fff{]+"
                r"|(?<=[./])\s+(?=[A-Za-z0-9_~%-]+[./]))+", re.IGNORECASE
            )
            source_for_translation = re.sub(
                r"\[PLPROTECTED[A-Z]*\d+\]", lambda match: shield(match.group()), segment.source
            )
            source_for_translation = url_pattern.sub(lambda match: shield(match.group()), source_for_translation)
            for value in dict.fromkeys(protected_markup):
                if value in source_for_translation:
                    source_for_translation = re.sub(
                        re.escape(value), lambda match: shield(match.group()), source_for_translation
                    )

            # 所有科学缩写与数字均按出现位置保护，包括只出现一次的引文年份。
            # 只扫描尚未保护的正文间隙，绝不再次扫描哨兵编号或URL内部。
            marker_pattern = r"\[" + prefix + r"\d{4,}\]"
            for pattern, values in ((ABBREVIATION_RE, counts["abbreviation"]),
                                    (NUMBER_RE, counts["number"])):
                parts = re.split("(" + marker_pattern + ")", source_for_translation)
                source_for_translation = "".join(
                    part if part in sentinels else pattern.sub(
                        lambda match: shield(match.group()) if match.group() in values else match.group(), part
                    ) for part in parts
                )

            # 官方术语干预同时承担两项职责：固定领域译法，并强制模型原样保留
            # 公式占位符、缩写、单位、URL 与 DOI。重复源词只保留第一条映射。
            terms_by_source: dict[str, str] = {}
            for entry in context.domain.glossary:
                if source_term_is_present(segment.source, entry.source):
                    terms_by_source.setdefault(entry.source, entry.target)
            for category in ("url", "doi", "number", "abbreviation", "unit"):
                for value in counts[category]:
                    if value in source_for_translation:
                        terms_by_source.setdefault(value, value)
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
            target = self._request_translation(body)
            marker_pattern = r"\[" + prefix + r"\d{4,}\]"
            # 连同改写命名空间/空格的标记一起识别为异常，不模糊映射到合法ID。
            returned_marker_pattern = r"\[\s*PLPROTECTED[A-Z]*\s*\d+\s*\]"
            returned_markers = re.findall(returned_marker_pattern, target)
            # 未知或重复标记意味着整段候选不可用，不能猜测其对应原文。
            # 与缺标记共用下方唯一的原文间隙恢复路径，整份坏候选被丢弃；
            # 若分段请求本身又产生标记，仍立即失败，不递归重试。
            valid_markers = (set(returned_markers) == set(sentinels)
                             and all(target.count(marker) == 1 for marker in sentinels))
            restored_target = target
            if valid_markers:
                for marker, value in sentinels.items():
                    restored_target = restored_target.replace(marker, value)
            domain = None if segment.id in context.reference_segment_ids else context.domain
            # 标记完整也可能被模型粘到额外数字上，如 20[year] -> 202015。
            # 对还原后的整段运行同一门禁，不能仅凭标记个数就接受候选。
            needs_repair = not valid_markers or bool(
                validate_translation(segment.source, restored_target, domain)
            )
            if needs_repair:
                # 已实测专用模型会删去重复 SST 的某次标记。仅做一次确定性分段：
                # 标识符在原位置由本地保留，只让模型翻译相邻文本，不再让它复制
                # 标记，也不从失败译文猜测插入位置。各片段保持原序；最终整段仍
                # 经调用方数字/术语/公式门禁。此分支不递归、不切模型。
                parts = re.split("(" + marker_pattern + ")", source_for_translation)
                repaired = []
                for part in parts:
                    if part in sentinels:
                        repaired.append(sentinels[part])
                    elif re.search(r"[A-Za-z]{2,}", part):
                        options = body["translation_options"]
                        fragment_terms = [entry for entry in options["terms"]
                                          if entry["source"] in part
                                          and not re.fullmatch(marker_pattern, entry["source"])]
                        fragment_body = {
                            **body, "messages": [{"role": "user", "content": part.strip()}],
                            "translation_options": {**options, "terms": fragment_terms},
                        }
                        translated_part = self._request_translation(fragment_body)
                        if re.search(returned_marker_pattern, translated_part):
                            raise ValueError("Qwen-MT 分段译文包含意外保护标记")
                        fragment_errors = validate_translation(part, translated_part, None)
                        if fragment_errors:
                            raise ValueError(f"Qwen-MT 分段译文未通过门禁：{fragment_errors}")
                        repaired.append(" " + translated_part.strip() + " ")
                    else:
                        repaired.append(part)
                target = "".join(repaired)
            else:
                target = restored_target
            # Qwen 常把指标名中的 ASCII 连字符排成不换行连字符；指标身份要求
            # ``HDI-P`` 等标记逐字保留，因此只在两侧都是 ASCII 字母或数字时还原。
            target = re.sub(
                r"(?<=[A-Za-z0-9])[\u2010\u2011](?=[A-Za-z0-9])",
                "-",
                target,
            )
            translations.append(Translation(id=segment.id, target=target.strip()))
        return translations

    def _request_translation(self, body: dict) -> str:
        """发送一次官方单条翻译请求；网络错误策略与整段调用保持一致。"""

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        rate_limit_retried = False
        for attempt in range(3):
            # Qwen-MT 默认60 RPM且可能按秒限速；整段和分段共用此实例节奏。
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request_at = time.monotonic() + self.min_request_interval_seconds
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                try:
                    code = json.loads(detail).get("error", {}).get("code")
                except (ValueError, AttributeError):
                    code = None
                if exc.code == 429 and code == "limit_requests" and not rate_limit_retried and attempt < 2:
                    # 仅处理已经观察到的请求限流；计费/凭据/权限错误不重试。
                    # 不无限等待配额窗口；最多等待60秒并重试一次，仍失败交回断点。
                    retry_after = exc.headers.get("Retry-After", "60") if exc.headers else "60"
                    try:
                        wait_seconds = float(retry_after)
                    except ValueError:
                        wait_seconds = 60.0
                    if math.isfinite(wait_seconds) and 0 <= wait_seconds <= 60:
                        rate_limit_retried = True
                        print("Qwen-MT：请求限流，等待后仅重试一次", flush=True)
                        time.sleep(max(1.0, wait_seconds))
                        continue
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
        return target
