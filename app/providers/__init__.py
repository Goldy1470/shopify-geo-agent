"""app.providers — the 4 official-API answer providers + registry.

Answers about the catalog come only from these (or paste mode). No key in
config/env -> provider unavailable -> runner skips it silently.
"""

from .anthropic import AnthropicProvider
from .base import AnswerProvider, ProviderError, ProviderUnavailable
from .gemini import GeminiProvider
from .openai_responses import OpenAIResponsesProvider
from .perplexity import PerplexityProvider

PROVIDERS: dict[str, type[AnswerProvider]] = {
    p.name: p for p in (OpenAIResponsesProvider, PerplexityProvider, GeminiProvider, AnthropicProvider)
}


def available_providers(cfg: dict) -> dict[str, type[AnswerProvider]]:
    return {name: cls for name, cls in PROVIDERS.items() if cls.available(cfg)}


__all__ = [
    "AnthropicProvider",
    "AnswerProvider",
    "GeminiProvider",
    "OpenAIResponsesProvider",
    "PROVIDERS",
    "PerplexityProvider",
    "ProviderError",
    "ProviderUnavailable",
    "available_providers",
]
