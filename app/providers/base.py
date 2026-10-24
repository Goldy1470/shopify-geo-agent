"""Provider contract for AI shopping answers.

Every answer a model gives about the catalog MUST come from an official API
(OpenAI Responses + web_search, Perplexity Sonar, Gemini with Search
grounding, Anthropic web search server tool). No scraping, no browser mode.

Providers:
- are constructed from config (api keys live in config.json / env only),
- report availability (no key -> not available, runner skips silently),
- return a normalized ProviderAnswer with sources for citing "API-based estimate".
"""

from abc import ABC, abstractmethod
from typing import Any


class ProviderError(RuntimeError):
    pass


class ProviderUnavailable(RuntimeError):
    pass


class AnswerProvider(ABC):
    """One retrieval-backed engine. Implementations must be async."""

    name: str = "unset"
    engine_label: str = "API-based estimate"

    def __init__(self, cfg: dict, transport=None) -> None:
        self.cfg = cfg
        self._transport = transport  # injectable httpx transport (tests/proxies)

    @classmethod
    @abstractmethod
    def available(cls, cfg: dict) -> bool:
        """Whether the required API key/config is present."""

    @abstractmethod
    async def ask(self, prompt: str, *, lang: str = "en-US") -> dict:
        """Run one prompt; return {text, sources, engine_label, raw_len}.

        Never raises on empty answers - returns text ''; provider/network
        failures raise ProviderError so the runner can log-and-continue.
        """

    def _key(self, *names: str, required: bool = True) -> str:
        """Fetch a config value from config dict falling back to env."""
        import os

        for n in names:
            value = self.cfg.get(n)
            if value:
                return str(value)
            value = os.environ.get(n.upper())
            if value:
                return value
        if required:
            raise ProviderUnavailable(f"{self.__class__.__name__}: missing config key {names[0]}")
        return ""

    def answer_contract(self, data: dict) -> dict:
        """Normalize the provider payload -> {text, sources}."""
        return data


def sources_entry(title: str, url: str) -> dict[str, Any]:
    return {"title": title or "", "url": url or ""}
