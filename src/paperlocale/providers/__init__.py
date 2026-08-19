"""PaperLocale 翻译 Provider。

每个 Provider 只负责结构化片段翻译，不允许绕过内容门禁或直接渲染 PDF。
"""

from .base import Segment, Translation, TranslationContext, TranslationProvider
from .codex_local import REASONING_EFFORTS, CodexLocalProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = [
    "REASONING_EFFORTS",
    "CodexLocalProvider",
    "OpenAICompatibleProvider",
    "Segment",
    "Translation",
    "TranslationContext",
    "TranslationProvider",
]
