"""Gemini provider with the official Google Search grounding tool.

Official API: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=...
- tool: {"google_search": {}} activates Search grounding; sources arrive in
  groundingMetadata.groundingChunks. Key: GEMINI_API_KEY (config or env).
"""

import httpx

from .base import AnswerProvider, ProviderError, ProviderUnavailable, sources_entry


class GeminiProvider(AnswerProvider):
    name = "gemini"
    model = "gemini-2.5-flash"

    @classmethod
    def available(cls, cfg: dict) -> bool:
        import os

        return bool(cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY"))

    async def ask(self, prompt: str, *, lang: str = "en-US") -> dict:
        try:
            key = self._key("gemini_api_key", required=True)
        except ProviderUnavailable as exc:
            raise ProviderUnavailable(str(exc)) from exc
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload: dict = {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0},
        }
        try:
            async with httpx.AsyncClient(timeout=90.0, transport=self._transport) as client:
                resp = await client.post(f"{url}?key={key}", json=payload,
                                         headers={"Content-Type": "application/json"})
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"gemini request failed: {exc}") from exc

        parts = ((data.get("candidates") or [{}])[0]
                 .get("content", {}).get("parts") or [])
        text = "".join(p.get("text") or "" for p in parts if isinstance(p, dict))
        if resp.status_code != 200:  # pragma: no cover - raise_for_status already guards
            raise ProviderError(f"gemini HTTP {resp.status_code}")

        sources = []
        seen: set[str] = set()
        grounding = (((data.get("candidates") or [{}])[0]
                      .get("groundingMetadata") or {}).get("groundingChunks") or [])
        for chunk in grounding:
            web = (chunk or {}).get("web") or {}
            u = web.get("uri") or ""
            if u and u not in seen:
                seen.add(u)
                sources.append(sources_entry(web.get("title") or "", u))
        return {"text": text, "sources": sources, "engine_label": self.engine_label}
