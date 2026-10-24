"""Prompt sets + the tracking runner (Phase 3 TRACK).

Prompts live in the legacy SQLite DB (`prompts` table): user-entered or
auto-generated from the catalog, all editable in the UI. The runner fires
every active prompt against every available provider N times with:

- resumability: a prompt x engine pair with any run inside the freshness
  window is skipped (a finished/interrupted batch isn't redone),
- randomized task order + bounded concurrency (asyncio.Semaphore),
- a daily run cap (cfg "track_daily_cap", default 60) so nobody wakes up to
  a surprise bill,
- per-provider errors are logged and skipped, never fatal.

Answers only ever come from official APIs (app.providers); analysis of each
answer goes through the regex analyzer, upgraded to the ported OneGlanse
LLM grader (guarded) when an OpenAI key is configured.
"""

import asyncio
import logging
import os
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .db import execute, query
from .providers import ProviderError, ProviderUnavailable, available_providers
from .providers.analysis import AnalysisClient, AnalysisFailed, annotate_with_checklist
from .response_analyzer import _product_tokens, analyze_response
from .visibility import save_run

log = logging.getLogger("tracking")

RECOMMENDATION_SCORES = {
    "top_pick": 100,
    "strong_alternative": 80,
    "conditional": 60,
    "mentioned_only": 30,
    "discouraged": 10,
    "not_mentioned": 0,
}
RANK_SCORES = {1: 100, 2: 80, 3: 65, 4: 50}

# Use-case stems (scoring.USE_CASE_WORDS) -> natural phrases for prompts.
USE_CASE_PHRASES = {
    "travel": "travel",
    "commut": "commuting",
    "hik": "hiking",
    "beach": "beach days",
    "gym": "the gym",
    "weekend": "weekend trips",
    "rain": "rainy days",
    "camping": "camping trips",
    "school": "school",
    "carrying": "carrying gear",
    "everyday": "everyday use",
    "daily": "daily carry",
    "work": "work",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- prompt CRUD -------------------------------------------------------------


def list_prompts(only_active: bool = False) -> list[dict]:
    sql = "SELECT * FROM prompts"
    if only_active:
        sql += " WHERE active=1"
    rows = query(sql + " ORDER BY id")
    return [dict(r) for r in rows]


def get_prompt(prompt_id: int) -> dict | None:
    rows = query("SELECT * FROM prompts WHERE id=?", (prompt_id,))
    return dict(rows[0]) if rows else None


def add_prompt(text: str, source: str = "user") -> dict | None:
    """Insert a prompt; near-duplicates (casefold match) are ignored."""
    text = (text or "").strip()
    if not text:
        return None
    existing = {r["text"].casefold() for r in query("SELECT text FROM prompts")}
    if text.casefold() in existing:
        return None
    execute("INSERT INTO prompts (text, source, active, created_at) VALUES (?,?,1,?)", (text, source, _now()))
    return get_prompt(query("SELECT last_insert_rowid() AS id")[0]["id"])


def set_prompt_active(prompt_id: int, active: bool) -> None:
    execute("UPDATE prompts SET active=? WHERE id=?", (1 if active else 0, prompt_id))


def delete_prompt(prompt_id: int) -> None:
    execute("DELETE FROM prompts WHERE id=?", (prompt_id,))


# --- auto-generated prompts ---------------------------------------------------


def _use_for(p: dict) -> str:
    """Pick a natural use-case phrase from tags/title/body (deterministic)."""
    tags = " ".join(p.get("tags") or []) if isinstance(p.get("tags"), list) else (p.get("tags") or "")
    meta = p.get("metafields") or {}
    intended = str(meta.get("intended_use") or "")
    hay = f"{tags} {intended} {p.get('title') or ''} {p.get('body_html') or ''}".lower()
    for stem, phrase in USE_CASE_PHRASES.items():
        if stem in hay:
            return phrase
    return "everyday use"


def generate_prompts_from_catalog(products: list[dict], limit: int = 10) -> list[str]:
    """Deterministic prompt set from the catalog, deduped vs. existing rows."""
    existing = {r["text"].casefold() for r in query("SELECT text FROM prompts")}
    made: list[str] = []
    for p in products:
        if len(made) >= limit:
            break
        ptype = ((p.get("product_type") or "").strip().lower()) or "gear"
        vendor = (p.get("vendor") or "").strip()
        title = (p.get("title") or "").strip()
        use = _use_for(p)
        price_max = p.get("price_max")
        candidates = [
            f"best {ptype} for {use}",
            f"which {ptype} do you recommend for {use}",
            f"is the {title} worth it",
            f"{vendor} {ptype} review" if vendor else None,
            f"best {ptype} under ${int(price_max)}" if price_max else None,
        ]
        for cand in candidates:
            if not cand or len(made) >= limit:
                continue
            key = cand.casefold()
            if key in existing:
                continue
            existing.add(key)
            made.append(cand)
    for text in made:
        execute("INSERT INTO prompts (text, source, active, created_at) VALUES (?,?,1,?)", (text, "auto", _now()))
    return made


# --- LLM analysis merge -------------------------------------------------------


def _own_tokens(products: list[dict]) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for p in products:
        for tok in _product_tokens(p):
            low = tok.lower()
            if tok and low not in seen:
                seen.add(low)
                tokens.append(tok)
    return tokens


def recompute_geo(analysis: dict) -> int:
    """Same formula + credibility caps as response_analyzer._analyze_product."""
    rank = analysis.get("rank")
    rank_score = 15 if rank is None else RANK_SCORES.get(int(rank), 40)
    rec_score = RECOMMENDATION_SCORES[analysis.get("recommendation") or "not_mentioned"]
    geo = round(0.25 * (analysis.get("visibility") or 0) + 0.25 * rank_score
                + 0.25 * (analysis.get("sentiment") or 0) + 0.25 * rec_score)
    if (analysis.get("visibility") or 0) < 20:
        geo = min(geo, 40)
    if (analysis.get("sentiment") or 0) <= 20:
        geo = min(geo, 30)
    return geo


def merge_llm_analysis(base: dict, guarded: dict) -> dict:
    """Regex analysis base + guarded LLM verdict over it (keeps sources/risks)."""
    out = dict(base)
    out["visibility"] = int(guarded.get("visibility") or 0)
    out["sentiment"] = int(guarded.get("sentiment") or 0)
    out["rank"] = guarded.get("rank")
    out["recommendation"] = guarded.get("recommendation") or "not_mentioned"
    out["caveats"] = guarded.get("caveats") or []
    out["competitors"] = guarded.get("competitors") or base.get("competitors") or []
    out["price_mentioned"] = bool(guarded.get("price_mentioned"))
    out["buy_link_mentioned"] = bool(guarded.get("buy_link_mentioned"))
    out["analysis_via"] = "llm"
    if out["recommendation"] == "not_mentioned":
        out["mentioned"] = False
        out["visibility"] = 0
        out["geo_score"] = 0
        return out
    out["geo_score"] = recompute_geo(out)
    return out


async def analyze_with_llm_if_available(cfg: dict, prompt: str, response_text: str, products: list[dict]) -> dict | None:
    """LLM-graded analysis when an OpenAI key exists; None = regex fallback."""
    key = cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
    if not key or not response_text.strip():
        return None
    base = analyze_response(prompt, response_text, products)
    client = AnalysisClient(key)
    brand_context = "; ".join(
        f"{(p.get('vendor') or '').strip()} {(p.get('title') or '').strip()}".strip() for p in products[:6]
    )
    try:
        result = await client.analyze(response_text, prompt, brand_context)
    except (AnalysisFailed, httpx.HTTPError) as exc:
        log.info("LLM analysis unavailable (%s); regex fallback", exc)
        return None
    guarded = annotate_with_checklist(result, response_text, _own_tokens(products))
    return merge_llm_analysis(base, guarded)


# --- the runner ----------------------------------------------------------------


def available_engines(cfg: dict) -> list[str]:
    """Engine names with keys configured (UI helper; runner skips the rest)."""
    return sorted(available_providers(cfg).keys())


async def run_tracking(
    products: list[dict],
    cfg: dict,
    engine_names: list[str] | None = None,
    prompts: list[dict] | None = None,
    n: int = 5,
    freshness_hours: float = 24,
    daily_cap: int | None = None,
    concurrency: int = 2,
    providers: dict[str, Any] | None = None,
) -> dict:
    """Run every prompt x engine pair N times (resumable, bounded, capped)."""
    provs = providers if providers is not None else {name: cls(cfg) for name, cls in available_providers(cfg).items()}
    if engine_names:
        provs = {k: v for k, v in provs.items() if k in engine_names}
    if not provs:
        return {"error": "no_available_providers", "runs": []}

    prompt_rows = prompts if prompts is not None else list_prompts(only_active=True)
    if not prompt_rows:
        return {"error": "no_prompts", "runs": []}

    # Resumability: pairs with any run inside the freshness window are done.
    cutoff = (datetime.now(UTC) - timedelta(hours=freshness_hours)).isoformat()
    fresh = {
        (r["prompt"], r["engine"]): r["c"]
        for r in query(
            "SELECT prompt, engine, COUNT(*) AS c FROM visibility_runs "
            "WHERE created_at >= ? GROUP BY prompt, engine",
            (cutoff,),
        )
    }

    daily_cap = int(cfg.get("track_daily_cap", 60)) if daily_cap is None else int(daily_cap)
    today_prefix = datetime.now(UTC).date().isoformat() + "T"
    done_today = query("SELECT COUNT(*) AS c FROM visibility_runs WHERE created_at >= ?", (today_prefix,))[0]["c"]
    budget = max(0, daily_cap - int(done_today))

    runnable: list[tuple[dict, Any]] = []
    skipped_fresh = 0
    for prow in prompt_rows:
        for ename, prov in provs.items():
            remaining = n - min(n, fresh.get((prow["text"], ename), 0))
            if remaining <= 0:
                skipped_fresh += n - remaining
                continue
            runnable.extend((prow, prov) for _ in range(remaining))

    random.shuffle(runnable)
    skipped_cap = max(0, len(runnable) - budget)
    runnable = runnable[:budget]

    sem = asyncio.Semaphore(concurrency)
    errors: list[dict] = []
    runs: list[dict] = []

    async def run_one(prow: dict, prov: Any) -> None:
        async with sem:
            try:
                answer = await prov.ask(prow["text"])
            except (ProviderError, ProviderUnavailable) as exc:
                errors.append({"engine": getattr(prov, "name", "?"), "error": str(exc)[:200]})
                return
            except Exception as exc:  # provider adapters can raise transport errors
                errors.append({"engine": getattr(prov, "name", "?"), "error": str(exc)[:200]})
                return
            analysis = await analyze_with_llm_if_available(cfg, prow["text"], answer.get("text", ""), products)
            run = save_run(
                prow["text"], prov.name, "api", answer.get("text", ""), products,
                prompt_id=prow.get("id"), analysis=analysis,
            )
            runs.append(run)

    if runnable:
        await asyncio.gather(*(run_one(p, e) for p, e in runnable))

    return {
        "runs": runs,
        "run_count": len(runs),
        "requested": len(runnable) + skipped_cap,
        "skipped": {"freshness_window": skipped_fresh, "daily_cap": skipped_cap},
        "errors": errors,
        "engines": list(provs.keys()),
        "prompt_count": len(prompt_rows),
        "n": n,
    }
