"""Tracking: prompt set generation/CRUD, Wilson math, LLM merge, runner semantics."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.db import execute, query
from app.mock_data import MOCK_CATALOG
from app.response_analyzer import analyze_response
from app.tracking import (
    add_prompt,
    delete_prompt,
    generate_prompts_from_catalog,
    list_prompts,
    merge_llm_analysis,
    recompute_geo,
    run_tracking,
    set_prompt_active,
)
from app.visibility import save_run, wilson_ci


@pytest.fixture(autouse=True)
def no_openai_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# --- prompt generation + CRUD --------------------------------------------------


def test_generation_is_deterministic_and_saturates():
    all_made: list[str] = []
    for _ in range(10):
        made = generate_prompts_from_catalog(MOCK_CATALOG, limit=8)
        all_made.extend(made)
        if not made:
            break
    assert generate_prompts_from_catalog(MOCK_CATALOG, limit=8) == [], "saturated: every template deduped"
    folded = [t.casefold() for t in all_made]
    assert len(folded) == len(set(folded)), "no duplicates across fill-up calls"
    rows = list_prompts()
    assert [r["text"] for r in rows] == all_made
    assert all(r["source"] == "auto" for r in rows)
    assert any("aurora pack" in r["text"].lower() for r in rows), "catalog titles feed 'worth it' prompts"


def test_generation_templates_and_limit():
    generate_prompts_from_catalog(MOCK_CATALOG, limit=5)
    rows = list_prompts()
    assert 1 <= len(rows) <= 5
    texts = " | ".join(r["text"] for r in rows).lower()
    assert "best " in texts or "which " in texts or "worth it" in texts


def test_add_prompt_dedupes_case_insensitively():
    a = add_prompt("Best Shell Jacket for rainy commuting")
    b = add_prompt("best shell jacket for RAINY COMMUTING")
    assert a and b is None
    assert len([r for r in list_prompts() if "shell jacket" in r["text"].lower()]) == 1


def test_add_prompt_rejects_blank():
    assert add_prompt("   ") is None


def test_crud_lifecycle():
    prow = add_prompt("is the aurora worth it")
    assert prow["source"] == "user" and prow["active"] == 1
    set_prompt_active(prow["id"], False)
    assert list_prompts()[0]["active"] == 0
    assert list_prompts(only_active=True) == []
    delete_prompt(prow["id"])
    assert list_prompts() == []


# --- Wilson CI ------------------------------------------------------------------


def test_wilson_small_sample_interval():
    lo, hi = wilson_ci(1, 5)
    assert lo < 0.2 < hi
    assert lo >= 0.0 and hi <= 1.0
    # closed form for 1/5, z=1.96: roughly (3.6%, 62.4%)
    assert round(100 * lo) == 4 and round(100 * hi) == 62


def test_wilson_edges():
    assert wilson_ci(0, 0) == (0.0, 0.0)
    lo, hi = wilson_ci(5, 5)
    assert hi <= 1.0 and lo > 0.5  # 5/5 still admits doubt


def test_wilson_tightens_with_n():
    _, hi_small = wilson_ci(1, 2)
    lo_small, _ = wilson_ci(1, 2)
    lo_big, hi_big = wilson_ci(50, 100)
    assert (hi_small - lo_small) > (hi_big - lo_big), "same rate, more runs -> tighter CI"


# --- LLM analysis merge ---------------------------------------------------------


GUARDED = {
    "visibility": 40,
    "sentiment": 60,
    "rank": 2,
    "recommendation": "conditional",
    "caveats": ["pricey"],
    "competitors": [],
    "price_mentioned": True,
    "buy_link_mentioned": False,
}


def _base_analysis():
    return analyze_response(
        "best travel backpack",
        "The Aurora Trail Backpack is a solid choice for carry-on travel. Cons: hip belt fit.",
        MOCK_CATALOG,
    )


def test_geo_recompute_matches_the_methodology_formula():
    out = merge_llm_analysis(_base_analysis(), GUARDED)
    assert out["geo_score"] == 60  # 0.25 * (40 visibility + 80 rank + 60 sent + 60 rec)


def test_geo_credibility_caps_apply():
    out = merge_llm_analysis(_base_analysis(), {**GUARDED, "visibility": 15})
    assert out["geo_score"] <= 40
    out2 = merge_llm_analysis(_base_analysis(), {**GUARDED, "visibility": 80, "sentiment": 5})
    assert out2["geo_score"] <= 30


def test_merge_keeps_regex_structure_but_flags_llm():
    out = merge_llm_analysis(_base_analysis(), GUARDED)
    assert out["analysis_via"] == "llm"
    for key in ("sources", "source_domains", "products", "retailers", "risks"):
        assert key in out, f"regex structure {key} preserved"
    assert out["price_mentioned"] is True
    assert out["caveats"] == ["pricey"]


def test_recompute_geo_direct():
    assert recompute_geo({"rank": 1, "recommendation": "top_pick", "visibility": 100, "sentiment": 100}) == 100
    # degenerate not-mentioned dict scores 4 by the raw formula; merge_llm_analysis
    # zeroes those before ever calling recompute (see not_mentioned early return).
    assert recompute_geo({"rank": None, "recommendation": "not_mentioned", "visibility": 0, "sentiment": 0}) == 4


def test_merge_forced_not_mentioned_zeroes_geo():
    out = merge_llm_analysis(
        _base_analysis(),
        {**GUARDED, "visibility": 90, "recommendation": "not_mentioned", "rank": None},
    )
    assert out["geo_score"] == 0 and out["mentioned"] is False


async def test_llm_path_used_when_key_configured(monkeypatch):
    from app.providers.analysis import AnalysisClient
    from app.tracking import analyze_with_llm_if_available

    good = {
        "visibility": 55, "sentiment": 60, "rank": 1, "recommendation": "top_pick",
        "caveats": [], "competitors": [{"name": "Osprey", "kind": "brand", "mentions": 1}],
        "confidence": 60, "price_mentioned": True, "buy_link_mentioned": True,
    }

    async def good_call(self, payload):
        return {"choices": [{"message": {"content": json.dumps(good)}}]}

    monkeypatch.setattr(AnalysisClient, "_call", good_call)
    merged = await analyze_with_llm_if_available(
        {"openai_api_key": "sk"}, "q", "The Aurora Trail Backpack is our top pick.", MOCK_CATALOG
    )
    assert merged is not None and merged["analysis_via"] == "llm"
    assert merged["recommendation"] == "top_pick"


async def test_llm_path_falls_back_when_grader_fails(monkeypatch):
    from app.providers.analysis import AnalysisClient
    from app.tracking import analyze_with_llm_if_available

    async def bad_call(self, payload):
        return {"choices": [{"message": {"content": "still not json"}}]}

    monkeypatch.setattr(AnalysisClient, "_call", bad_call)
    merged = await analyze_with_llm_if_available(
        {"openai_api_key": "sk"}, "q", "The Aurora is great.", MOCK_CATALOG
    )
    assert merged is None, "grader failure -> regex path (None means no LLM override)"


async def test_llm_path_skipped_without_key():
    from app.tracking import analyze_with_llm_if_available

    assert await analyze_with_llm_if_available({}, "q", "The Aurora is great.", MOCK_CATALOG) is None


# --- the runner ------------------------------------------------------------------


class FakeProvider:
    name = "fake"
    engine_label = "API-based estimate"
    concurrent_now = 0
    max_seen = 0

    def __init__(self, cfg=None, text=None):
        self.text = text if text is not None else "The Weekender Duffel is our budget pick. Buy it at Walmart."
        self.calls: list[str] = []

    async def ask(self, prompt: str):
        FakeProvider.concurrent_now += 1
        FakeProvider.max_seen = max(FakeProvider.max_seen, FakeProvider.concurrent_now)
        await asyncio.sleep(0.02)
        FakeProvider.concurrent_now -= 1
        self.calls.append(prompt)
        return {"text": self.text, "sources": [], "engine_label": self.engine_label}


class ExplodingProvider(FakeProvider):
    name = "explode"

    async def ask(self, prompt: str):
        raise RuntimeError("provider backend exploded")


class StubProvider:
    name = "stubbed"
    engine_label = "API-based estimate"

    def __init__(self, cfg=None):
        self.calls: list[str] = []

    async def ask(self, prompt: str):
        self.calls.append(prompt)
        return {"text": "Aurora trail wins for rainy commuting.", "sources": [], "engine_label": "API-based estimate"}


def _prompt(text="best duffel for travel") -> dict:
    return add_prompt(text)


def _fresh_prompt_row() -> dict:
    return list_prompts()[0]


def test_runner_does_n_per_pair_and_stores_engine():
    _prompt()
    prov = FakeProvider()
    result = asyncio.run(run_tracking(MOCK_CATALOG, {}, providers={"fake": prov}, n=5))
    assert result["run_count"] == 5
    assert len(prov.calls) == 5
    rows = query("SELECT * FROM visibility_runs")
    assert all(r["engine"] == "fake" for r in rows)
    assert all(r["prompt_id"] == _fresh_prompt_row()["id"] for r in rows)


def test_runner_resumes_within_freshness():
    _prompt()
    prov = FakeProvider()
    asyncio.run(run_tracking(MOCK_CATALOG, {}, providers={"fake": prov}, n=5))
    result = asyncio.run(run_tracking(MOCK_CATALOG, {}, providers={"fake": prov}, n=5))
    assert result["run_count"] == 0
    assert result["skipped"]["freshness_window"] == 5


def test_runner_partial_resume_counts_existing_runs():
    _prompt()
    prov = FakeProvider()
    text = _fresh_prompt_row()["text"]
    for _ in range(2):  # interrupted batch: 2 of 5 already exist, fresh
        save_run(text, "fake", "api", "partial answer", MOCK_CATALOG)
    result = asyncio.run(run_tracking(MOCK_CATALOG, {}, providers={"fake": prov}, n=5))
    assert result["run_count"] == 3


def test_runner_daily_cap_limits_runs():
    _prompt()
    prov = FakeProvider()
    result = asyncio.run(run_tracking(MOCK_CATALOG, {}, providers={"fake": prov}, n=5, daily_cap=2))
    assert result["run_count"] == 2
    assert result["skipped"]["daily_cap"] == 3


def test_runner_cap_reads_cfg_when_param_absent():
    _prompt()
    prov = FakeProvider()
    result = asyncio.run(
        run_tracking(MOCK_CATALOG, {"track_daily_cap": 1}, providers={"fake": prov}, n=5)
    )
    assert result["run_count"] == 1
    assert result["skipped"]["daily_cap"] == 4


def test_runner_provider_errors_are_continued():
    _prompt()
    result = asyncio.run(
        run_tracking(
            MOCK_CATALOG, {},
            providers={"explode": ExplodingProvider(), "fake": FakeProvider()},
            n=2,
        )
    )
    assert result["run_count"] == 2  # only fake's runs landed
    assert len(result["errors"]) == 2
    assert result["errors"][0]["engine"] == "explode"
    assert "exploded" in result["errors"][0]["error"]


def test_runner_bounds_concurrency():
    for i in range(4):
        add_prompt(f"unique prompt number {i}")
    FakeProvider.max_seen = 0
    asyncio.run(run_tracking(MOCK_CATALOG, {}, providers={"fake": FakeProvider()}, n=3, concurrency=2))
    assert FakeProvider.max_seen <= 2


def test_runner_needs_engines_and_prompts():
    assert asyncio.run(run_tracking(MOCK_CATALOG, {}, providers={}))["error"] == "no_available_providers"
    assert asyncio.run(
        run_tracking(MOCK_CATALOG, {}, providers={"fake": FakeProvider()}, prompts=[])
    )["error"] == "no_prompts"


def test_runner_engine_filter():
    add_prompt("prompt a")
    add_prompt("prompt b has different words entirely")
    a, b = FakeProvider(), FakeProvider()
    b.name = "fake2"
    result = asyncio.run(
        run_tracking(MOCK_CATALOG, {}, engine_names=["fake"], providers={"fake": a, "fake2": b}, n=1)
    )
    assert result["run_count"] == 2
    assert b.calls == []
    engines_in_db = {r["engine"] for r in query("SELECT engine FROM visibility_runs")}
    assert engines_in_db == {"fake"}


def test_runner_uses_active_prompts_only():
    active = add_prompt("active prompt one")
    paused = add_prompt("paused prompt two")
    set_prompt_active(paused["id"], False)
    prov = FakeProvider()
    result = asyncio.run(run_tracking(MOCK_CATALOG, {}, providers={"fake": prov}, n=2))
    assert result["run_count"] == 2
    assert set(prov.calls) == {active["text"]}


async def test_runner_accepts_plain_stub_instances():
    _prompt()
    stub = StubProvider()
    result = await run_tracking(MOCK_CATALOG, {}, providers={"stubbed": stub}, n=1)
    assert result["run_count"] == 1
    assert stub.calls == ["best duffel for travel"]


def test_save_run_with_prompt_id_writes_column():
    prow = _prompt("column check prompt")
    save_run("column check prompt", "paste-engine", "paste", "Nothing relevant.", MOCK_CATALOG, prompt_id=prow["id"])
    row = query("SELECT prompt_id, engine FROM visibility_runs ORDER BY id DESC LIMIT 1")[0]
    assert row["prompt_id"] == prow["id"]
    assert row["engine"] == "paste-engine"


def test_freshness_window_excludes_stale_runs():
    prow = _prompt("stale prompt")
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    execute(
        "INSERT INTO visibility_runs (prompt, engine, source, response_text, mentioned_products, mentioned_merchants, product_count, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (prow["text"], "fake", "api", "x", "[]", "[]", 0, old),
    )
    prov = FakeProvider()
    result = asyncio.run(run_tracking(MOCK_CATALOG, {}, providers={"fake": prov}, n=1, freshness_hours=24))
    assert result["run_count"] == 1, "stale runs don't count as fresh"
