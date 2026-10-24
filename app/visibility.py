"""AI visibility monitoring.

Personal agents (Muse, Instinct) return no referral data and don't identify
themselves, so this module works with what you can check directly:
1. Manual paste mode - you run your category prompts through ChatGPT, Muse,
   Instagram shopping, Instinct, etc., and paste the response; we detect and
   position your products/brand within it.
2. Optional OpenAI API mode - if an OPENAI key is configured, run prompts
   automatically against the ChatGPT API.

Outputs are stored per run and rolled up into mention rates + position stats.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from .db import execute, query
from .response_analyzer import _product_tokens, _token_patterns, analyze_response


def _token_match(tokens: list[str], patterns, text: str) -> tuple[bool, int]:
    """Whether a product's title is mentioned and the first match position.

    Word-boundary regexes. >=2 distinct title tokens always match; a single token found
    counts only when it is the title's longest token (or the title is single-word). So
    generic 'a duffel bag' no longer summons 'Weekender Duffel - 45L - ...'.
    """
    matches = []
    for tok, rx in zip(tokens, patterns, strict=False):
        m = rx.search(text)
        if m:
            matches.append((tok, m.start()))
    if not matches:
        return False, -1
    if len(matches) >= 2:
        return True, min(start for _, start in matches)
    tok, start = matches[0]
    if len(tokens) == 1:
        return True, start
    return tok == max(tokens, key=len), start


def extract_mentions(response_text: str, products: list[dict]) -> dict:
    """Mention detection via analyzer token semantics (word boundaries).

    Regression fix replaces the old any-single-word matching that flagged products on
    generic category words.
    """
    product_hits = []
    for p in products:
        tokens = _product_tokens(p)
        if not tokens:
            continue
        hit, idx = _token_match(tokens, _token_patterns(tokens), response_text)
        if hit:
            product_hits.append({"product_id": str(p["id"]), "title": p.get("title", ""), "position": idx})
    product_hits.sort(key=lambda h: h["position"])

    brand_hits = []
    for p in products:
        vendor = (p.get("vendor") or "").strip()
        if vendor and vendor.lower() not in brand_hits and re.search(re.escape(vendor), response_text, re.I):
            brand_hits.append(vendor)

    merchants = re.findall(
        r"\b(Shopify|Amazon|Walmart|Target|Best Buy|Wayfair|Etsy|Klarna|Instacart)\b", response_text, re.I
    )
    merchants = sorted({m.capitalize() for m in merchants})
    # Your own store shows up when the response links your products to your brand/DTC site.
    return {
        "products": product_hits,
        "brand": brand_hits,
        "merchants": merchants,
        "looks_like_shopping": bool(re.search(r"\$|price|buy|add to cart", response_text, re.I)),
    }


def save_run(prompt: str, engine: str, source: str, response_text: str, products: list[dict]) -> dict:
    mentions = extract_mentions(response_text, products)
    analysis = analyze_response(prompt, response_text, products)
    execute(
        "INSERT INTO visibility_runs (prompt, engine, source, response_text, mentioned_products, mentioned_merchants, product_count, created_at, analysis_json, sources_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            prompt, engine, source, response_text,
            json.dumps(mentions["products"]), json.dumps(mentions["merchants"]),
            len(mentions["products"]), datetime.now(UTC).isoformat(),
            json.dumps(analysis), json.dumps(analysis["sources"]),
        ),
    )
    return {"prompt": prompt, "engine": engine, "source": source, "analysis": analysis, **mentions}


def run_prompt_via_openai(prompt: str, api_key: str, products: list[dict]) -> dict:
    """Run one shopping prompt against the OpenAI API (if configured)."""
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-search-preview",
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return save_run(prompt, "ChatGPT", "api", text, products)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return {"error": str(exc)}


def visibility_summary(products: list[dict]) -> dict:
    runs = query("SELECT * FROM visibility_runs ORDER BY created_at DESC LIMIT 100")
    prompts = []
    geo_scores = []
    first_ranks = []
    rec_counts: dict[str, int] = {}
    domain_totals: dict[str, int] = {}
    competitor_totals: dict[str, int] = {}
    total_sources = 0
    for r in runs:
        analysis = None
        try:
            analysis = json.loads(r["analysis_json"]) if r["analysis_json"] else None
        except ValueError:
            analysis = None
        sources: list[Any] = []
        try:
            sources = json.loads(r["sources_json"]) if r["sources_json"] else []
        except ValueError:
            sources = []
        prompts.append({
            "id": r["id"],
            "prompt": r["prompt"],
            "engine": r["engine"],
            "source": r["source"],
            "hits": json.loads(r["mentioned_products"]),
            "merchants": json.loads(r["mentioned_merchants"] or "[]"),
            "created_at": r["created_at"],
            "response_text": r["response_text"],
            "analysis": analysis,
            "sources": sources,
        })
        if analysis:
            geo_scores.append(analysis.get("geo_score") or 0)
            for p in analysis.get("products", [])[:1]:
                if p.get("rank"):
                    first_ranks.append(p["rank"])
            rec = analysis.get("recommendation")
            if rec:
                rec_counts[rec] = rec_counts.get(rec, 0) + 1
            for d in analysis.get("source_domains", []):
                domain_totals[d["domain"]] = domain_totals.get(d["domain"], 0) + d["count"]
                total_sources += d["count"]
            for c in analysis.get("competitors", []):
                competitor_totals[c["name"]] = competitor_totals.get(c["name"], 0) + 1
    total_runs = len(runs)
    runs_with_hit = sum(1 for p in prompts if p["hits"])
    brand_name = (products[0].get("vendor") if products else "") or ""
    top3: dict[str, int] = {}
    for p in prompts:
        first_hit = p["hits"][0]["title"] if p["hits"] else None
        if first_hit:
            top3[first_hit] = top3.get(first_hit, 0) + 1
    return {
        "runs": prompts[:30],
        "total_runs": total_runs,
        "mention_rate": round(100 * runs_with_hit / total_runs) if total_runs else 0,
        "brand": brand_name,
        "product_winners": sorted(top3.items(), key=lambda kv: -kv[1])[:5],
        # Analyzer rollups (OneGlanse-style)
        "avg_geo": round(sum(geo_scores) / len(geo_scores)) if geo_scores else None,
        "avg_rank": round(sum(first_ranks) / len(first_ranks), 1) if first_ranks else None,
        "rec_counts": sorted(rec_counts.items(), key=lambda kv: -kv[1]),
        "domain_stats": [
            {"domain": d, "count": n, "share": round(100 * n / total_sources) if total_sources else 0}
            for d, n in sorted(domain_totals.items(), key=lambda kv: -kv[1])[:8]
        ],
        "competitor_stats": sorted(competitor_totals.items(), key=lambda kv: -kv[1])[:8],
    }
