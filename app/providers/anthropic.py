"""Anthropic provider using the official web search server tool.

Official API: POST https://api.anthropic.com/v1/messages
- tools: [{"type": "web_search_20250306", "name": "web_search"}] runs a proto
  server-side search; results arrive as web_search_result blocks plus the
  text answer. Key: ANTHROPIC_API_KEY (config or env).
"""

import httpx

from .base import AnswerProvider, ProviderError, ProviderUnavailable, sources_entry


class AnthropicProvider(AnswerProvider):
    name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"
    model = "claude-sonnet-4-5"
    max_uses = 3  # keep search volume bounded per answer

    @classmethod
    def available(cls, cfg: dict) -> bool:
        import os

        return bool(cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))

    async def ask(self, prompt: str, *, lang: str = "en-US") -> dict:
        try:
            key = self._key("anthropic_api_key", required=True)
        except ProviderUnavailable as exc:
            raise ProviderUnavailable(str(exc)) from exc
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "temperature": 0,
            "tools": [{"type": "web_search_20250306", "name": "web_search", "max_uses": self.max_uses}],
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=90.0, transport=self._transport) as client:
                resp = await client.post(
                    self.url,
                    json=payload,
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                             "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc

        text_parts: list[str] = []
        sources: list[dict] = []
        seen: set[str] = set()
        for block in data.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text") or "")
            elif btype == "web_search_result":
                for r in block.get("content") or []:
                    u = r.get("url") or ""
                    if u and u not in seen:
                        seen.add(u)
                        sources.append(sources_entry(r.get("title") or "", u))
        return {"text": "".join(text_parts), "sources": sources, "engine_label": self.engine_label}
