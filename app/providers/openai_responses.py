"""OpenAI Responses provider with the official web_search tool.

Official API: POST https://api.openai.com/v1/responses
- {"model": "gpt-5-mini" class model, "tools": [{"type": "web_search"}],
  "input": prompt}. Cited URLs arrive as output annotations
  (url_citation). Key: OPENAI_API_KEY (same key serves paste-mode analysis).
"""

import httpx

from .base import AnswerProvider, ProviderError, ProviderUnavailable, sources_entry


class OpenAIResponsesProvider(AnswerProvider):
    name = "openai"
    url = "https://api.openai.com/v1/responses"
    model = "gpt-5-mini"

    @classmethod
    def available(cls, cfg: dict) -> bool:
        import os

        return bool(cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY"))

    async def ask(self, prompt: str, *, lang: str = "en-US") -> dict:
        try:
            key = self._key("openai_api_key", required=True)
        except ProviderUnavailable as exc:
            raise ProviderUnavailable(str(exc)) from exc
        payload = {
            "model": self.model,
            "tools": [{"type": "web_search"}],
            "input": prompt,
            "instructions": (
                "You are an AI shopping assistant answering a shopper question. "
                "Recommend specific products you can verify with web search. "
                "Include prices and where to buy when you can verify them."
            ),
            "text": {"verbosity": "low"},
        }
        try:
            async with httpx.AsyncClient(timeout=90.0, transport=self._transport) as client:
                resp = await client.post(
                    self.url,
                    json=payload,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai responses request failed: {exc}") from exc

        text = ""
        annotations: list[dict] = []
        try:
            for item in data.get("output") or []:
                if item.get("type") == "message":
                    for part in item.get("content") or []:
                        if part.get("type") in ("output_text", "text"):
                            text += part.get("text") or ""
                            annotations.extend(part.get("annotations") or [])
        except (TypeError, AttributeError) as exc:
            raise ProviderError(f"openai payload unusable: {exc}") from exc

        sources = []
        seen: set[str] = set()
        for ann in annotations:
            if ann.get("type") != "url_citation":
                continue
            u = ann.get("url") or ""
            if u and u not in seen:
                seen.add(u)
                sources.append(sources_entry(ann.get("title") or "", u))
        return {"text": text, "sources": sources, "engine_label": self.engine_label}
