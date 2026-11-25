"""Official OpenAI Responses API translation using a session-only API key."""

from __future__ import annotations

import http.client
import json
import re
import sys
import urllib.error
import urllib.request

from .base import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
    build_prompt,
    output_schema,
    parse_payload,
)

_API_HOST = "api.openai.com"
_LOW_EFFORT_REPAIR_MODELS = {"gpt-5.4", "gpt-5.4-2026-03-05"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # 密钥与论文文本只允许发给固定官方端点，不能随重定向传到其他站点。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _responses_opener():
    # macOS 的 urllib 遇到任意环境代理就跳过系统设置。若进程仅配置了
    # WSS 等与 HTTPS 无关的代理，继续遵守 Mac 的 HTTPS 代理与例外规则。
    if sys.platform == "darwin":
        environment = urllib.request.getproxies_environment()
        if environment and environment.keys().isdisjoint({"http", "https", "all", "no"}):
            system = urllib.request.getproxies_macosx_sysconf()
            if system.get("https") and not urllib.request.proxy_bypass_macosx_sysconf(_API_HOST):
                return urllib.request.build_opener(urllib.request.ProxyHandler(system), _NoRedirect())
    return urllib.request.build_opener(_NoRedirect())


class OpenAIAPIProvider(TranslationProvider):
    """One explicit official endpoint; no credential persistence or provider fallback."""

    endpoint = f"https://{_API_HOST}/v1/responses"
    # 真实论文的 30,000 字符批次在输出前中断。沿用实测的小批上限，
    # 逐批校验并保存；超出字符上限的单个逻辑段独立发送。
    max_batch_segments = 10
    max_batch_characters = 4000

    def __init__(self, *, api_key: str, model: str, timeout_seconds: int = 300) -> None:
        if not api_key or not api_key.isascii() or not api_key.isprintable() or " " in api_key:
            raise ValueError("OpenAI API key must be nonempty printable ASCII without whitespace")
        model = model.strip()
        if not model:
            raise ValueError("openai-api requires an explicit model")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def provenance(self) -> dict[str, object]:
        # 清单只记录可复核的模型和固定端点，不记录密钥或请求头。
        return {"provider": "openai-api", "model": self.model, "endpoint": self.endpoint}

    def translate(self, segments: list[Segment], context: TranslationContext) -> list[Translation]:
        # 使用共享提示和 JSON Schema。HTTP 的 output 数组可能同时包含推理消息，
        # 只有完成后的译文 JSON 才交给共享 ID 校验和后续科学内容门禁。
        body = {
            "model": self.model,
            "input": build_prompt(segments, context),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "paperlocale_translations",
                    "strict": True,
                    "schema": output_schema(),
                },
            },
            "store": False,
        }
        # 原有的定向返修仍只请求一次。已实测 GPT-5.4 的低强度推理能
        # 修复部分反复出现的括号和量值锚点错误，让首稿沿用模型默认值。
        if context.repair_feedback and self.model in _LOW_EFFORT_REPAIR_MODELS:
            body["reasoning"] = {"effort": "low"}
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _responses_opener().open(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 服务端正文可能回显凭据或论文内容。仅显示 HTTP 状态、固定提示
            # 及符合常见格式的请求编号，便于向服务方核对这次请求。
            request_id = exc.headers.get("x-request-id", "") if exc.headers else ""
            trace = ""
            if re.fullmatch(r"req_[A-Za-z0-9]{8,64}", request_id) and self._api_key not in request_id:
                trace = f" Request ID: {request_id}."
            if exc.code in {401, 403}:
                guidance = "Check the API key and project permissions."
            elif exc.code == 429:
                guidance = "Check API quota, billing, and rate limits before retrying."
            elif exc.code in {400, 404, 422}:
                guidance = "Check the model name and its Responses/Structured Outputs support."
            elif 300 <= exc.code < 400:
                guidance = "Redirects from the official endpoint are not followed."
            else:
                guidance = "Check OpenAI service availability before retrying."
            exc.close()
            raise RuntimeError(f"OpenAI API HTTP {exc.code}. {guidance}{trace}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            # 超时的计费结果不确定，不自动重发，避免一次操作产生重复收费。
            raise RuntimeError("OpenAI API connection failed or timed out. Check the connection before resuming.") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("OpenAI API returned an invalid JSON response") from None

        if not isinstance(payload, dict):
            raise TypeError("OpenAI API returned a non-object response")
        if payload.get("status") != "completed":
            # incomplete/failed 可能带有半截 JSON；必须拒绝，不能误当成完整译文落盘。
            details = payload.get("incomplete_details")
            if isinstance(details, dict) and details.get("reason") == "max_output_tokens":
                raise ValueError("OpenAI API reached its output limit. Resume with a smaller batch using --max-segments or --max-characters.")
            raise ValueError("OpenAI API response did not complete; no translation was accepted")
        output = payload.get("output")
        if not isinstance(output, list):
            raise TypeError("OpenAI API response is missing output messages")
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                raise TypeError("OpenAI API returned an invalid output item")
            if item.get("type") != "message":
                continue  # reasoning 等非正文输出不参加译文解析。
            if item.get("status") != "completed":
                raise ValueError("OpenAI API returned an incomplete message")
            content = item.get("content")
            if not isinstance(content, list):
                raise TypeError("OpenAI API message is missing content")
            for part in content:
                if not isinstance(part, dict):
                    raise TypeError("OpenAI API returned invalid message content")
                if part.get("type") == "refusal":
                    raise ValueError("OpenAI API refused this translation; no translation was accepted")
                if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise ValueError("OpenAI API returned unexpected translation content")
                texts.append(part["text"])
        if not texts:
            raise ValueError("OpenAI API returned no translation text")
        try:
            translated = json.loads("".join(texts))
        except json.JSONDecodeError:
            raise ValueError("OpenAI API did not return a complete JSON translation") from None
        try:
            return parse_payload(translated, segments)
        except ValueError:
            # 共享 ID 门禁仍执行，但不把服务返回的任意字段回显到错误日志。
            raise ValueError("OpenAI API translation IDs or value types did not match the requested segments") from None
