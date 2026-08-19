"""使用用户自备密钥的 OpenAI-compatible Chat Completions Provider。"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .base import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
    build_prompt,
    parse_payload,
)


class OpenAICompatibleProvider(TranslationProvider):
    """调用一个明确配置的兼容端点；不保存密钥，也不做静默 Provider 回退。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 300,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        scheme = parsed.scheme.lower()
        if scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("base_url 必须是 http:// 或 https:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url 不得包含用户名或密码")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url 不得包含查询参数或片段")
        if scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程 OpenAI-compatible 接口必须使用 HTTPS")
        if parsed.path.rstrip("/").endswith("/chat/completions"):
            raise ValueError("base_url 不得包含 /chat/completions")
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
        """只记录不含凭据的端点、Provider 和模型身份。"""

        return {
            "provider": "openai-compatible",
            "model": self.model,
            "base_url": self.base_url,
        }

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": build_prompt(segments, context)}],
            "temperature": 0,
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
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"翻译接口返回 HTTP {exc.code}：{detail[-2000:]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接翻译接口：{exc.reason}") from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("兼容接口响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str):
            raise ValueError("兼容接口返回的 message.content 不是字符串")
        try:
            translated_payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("兼容接口没有返回纯 JSON 译文") from exc
        return parse_payload(translated_payload, segments)
