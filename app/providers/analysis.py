"""LLM analysis of an AI-answered shopping prompt (response grading).

Ported from OneGlanse's prompts/analysisPrompt.ts + types/analysis.ts
(MIT, Aryaman Todkar; see NOTICE), with commerce adaptations: per-product
focus, price/buy-link checklist, competitor capture.

Contract:
- The analysis call is a plain LLM completion (JSON mode, temperature 0) - it
  grades text we already have from retrieval providers or paste mode.
- Output validated with Pydantic; on invalid JSON we retry once, then fall
  back to the offline regex analyzer.
- Deterministic checklist guards run AFTER the model: claims are re-checked
  against the raw text so a hallucinated "mentioned" verdict can't pass.
"""

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger("analysis")

RECOMMENDATION_CLASSES = (
    "top_pick",
    "strong_alternative",
    "conditional",
    "mentioned_only",
    "discouraged",
    "not_mentioned",
)


class Competitor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    kind: str = "brand"  # brand | retailer
    mentions: int = Field(default=0, ge=0)


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    visibility: int = Field(ge=0, le=100)
    sentiment: int = Field(ge=0, le=100)
    rank: int | None = Field(default=None, ge=1)
    recommendation: str
    caveats: list[str] = []
    competitors: list[Competitor] = []
    confidence: int = Field(default=50, ge=0, le=100)
    price_mentioned: bool = False
    buy_link_mentioned: bool = False

    @field_validator("recommendation")
    @classmethod
    def _known_class(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in RECOMMENDATION_CLASSES:
            raise ValueError(f"unknown recommendation class: {v}")
        return v


ANALYSIS_SYSTEM_PROMPT = """You grade an AI assistant's answer to one shopper question. \
Return STRICT JSON only (no markdown, no commentary).

The JSON schema:
{
 "visibility": 0-100,        // how prominently the brand/product appears
 "sentiment": 0-100,          // 0 = strongly negative, 50 = neutral, 100 = strongly positive
 "rank": 1-based position of the product among recommendations or null,
 "recommendation": "top_pick" | "strong_alternative" | "conditional" | "mentioned_only" | "discouraged" | "not_mentioned",
 "caveats": ["short quotes of caveats the assistant attached to the product"],
 "competitors": [{"name": str, "kind": "brand"|"retailer", "mentions": int}],
 "confidence": 0-100,
 "price_mentioned": bool,     // the answer includes a concrete price for the product
 "buy_link_mentioned": bool   // the answer includes a concrete store/buy path
}

Rules:
- Grade ONLY what the text actually says; do not use outside knowledge.
- If the brand/product is absent, visibility=0, sentiment=0, rank=null,
  recommendation="not_mentioned".
- A #1 slot with no evaluative language is NOT a top pick - it is
  "mentioned_only" at most.
- competitors = other shops/brands recommended in the same answer.
"""


def annotate_with_checklist(result: AnalysisResult, text: str, own_tokens: list[str]) -> dict:
    """Re-derive the mention verdict from the text where the model may have lied."""
    out = result.model_dump()
    low = text.lower()
    actually_mentioned = any(tok in low for tok in own_tokens) if own_tokens else False
    if not actually_mentioned:
        out["rank"] = None
        out["recommendation"] = "not_mentioned"
        out["visibility"] = 0
        out["sentiment"] = 0
    out["mentions_brand_in_text"] = actually_mentioned
    # caveats capped: analysis must stay compact
    out["caveats"] = [c for c in (out["caveats"] or []) if c][:6]
    out["competitors"] = out["competitors"][:8]
    return out


class AnalysisClient:
    """OpenAI chat-completions JSON analysis (temperature 0, one retry)."""

    url = "https://api.openai.com/v1/chat/completions"
    model = "gpt-5-mini"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def analyze(self, response_text: str, question: str, brand_context: str) -> AnalysisResult:
        user_prompt = (
            f"Shopper question:\n{question}\n\nBrand/product to grade: {brand_context}\n\n"
            f"Assistant answer to grade:\n<<<\n{response_text}\n>>>"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        last_error = ""
        for _attempt in range(2):
            data = await self._call(payload)
            raw = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            result = _parse_json(raw)
            if isinstance(result, AnalysisResult):
                return result
            last_error = str(result)[:200]
            payload["messages"] = payload["messages"][:1] + [
                {"role": "user", "content": user_prompt + "\n\nYour previous output was invalid JSON: "
                 + last_error + " Return ONLY the JSON object."}
            ]
        log.warning("analysis JSON failed twice; last error: %s", last_error)
        raise AnalysisFailed(last_error or "unparseable")

    async def _call(self, payload: dict) -> dict:
        import httpx as _httpx

        async with _httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                self.url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()


class AnalysisFailed(RuntimeError):
    pass


def _parse_json(raw: str) -> AnalysisResult | str:
    """Parse model output; returns AnalysisResult or an error string."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        return AnalysisResult.model_validate(json.loads(cleaned))
    except (ValueError, ValidationError) as exc:
        return str(exc)[:300]
