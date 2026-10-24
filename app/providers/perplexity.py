"""Perplexity Sonar provider (OpenAI-compatible chat with live web retrieval).

Official API: POST https://api.perplexity.ai/chat/completions
- model "sonar" performs live retrieval; citations come back in the payload.
- Key: PERPLEXITY_API_KEY (config or env). No key -> provider unavailable.
"""

import httpx

from .base import AnswerProvider, ProviderError, ProviderUnavailable, sources_entry


class PerplexityProvider(AnswerProvider):
    name = "perplexity"
    url = "https://api.perplexity.ai/chat/completions"
    model = "sonar"

    @classmethod
    def available(cls, cfg: dict) -> bool:
        import os

        return bool(cfg.get("perplexity_api_key") or os.environ.get("PERPLEXITY_API_KEY"))

    async def ask(self, prompt: str, *, lang: str = "en-US") -> dict:
        try:
            key = self._key("perplexity_api_key", required=True)
        except ProviderUnavailable as exc:
            raise ProviderUnavailable(str(exc)) from exc
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0, transport=self._transport) as client:
                resp = await client.post(
                    self.url,
                    json=payload,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"perplexity request failed: {exc}") from exc
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"perplexity payload unusable: {exc}") from exc
        # citations is a flat list of URLs on this API
        sources = [sources_entry("", u) for u in (data.get("citations") or []) if isinstance(u, str)]
        return {"text": text, "sources": sources, "engine_label": self.engine_label}
