"""OneGlanse analysisPrompt port: JSON validation, guards, retry behavior."""

import json
from unittest.mock import AsyncMock

import pytest

from app.providers.analysis import (
    AnalysisClient,
    AnalysisFailed,
    AnalysisResult,
    _parse_json,
    annotate_with_checklist,
)

VALID = {
    "visibility": 78,
    "sentiment": 64,
    "rank": 2,
    "recommendation": "strong_alternative",
    "caveats": [" zipper snags", "pricey"],
    "competitors": [{"name": "Osprey", "kind": "brand", "mentions": 2}],
    "confidence": 70,
    "price_mentioned": True,
    "buy_link_mentioned": False,
}


def test_valid_json_yields_model():
    res = _parse_json(json.dumps(VALID))
    assert isinstance(res, AnalysisResult)
    assert res.recommendation == "strong_alternative"
    assert res.rank == 2
    assert len(res.caveats) == 2


def test_fenced_json_block_parses():
    fenced = "```json\n" + json.dumps(VALID) + "\n```"
    assert isinstance(_parse_json(fenced), AnalysisResult)


def test_json_with_prose_wrappers_parses():
    raw = "Sure thing: " + json.dumps(VALID) + " hope that helps"
    assert isinstance(_parse_json(raw), AnalysisResult)


def test_unknown_recommendation_class_rejected():
    bad = {**VALID, "recommendation": "amazing_deal"}
    out = _parse_json(json.dumps(bad))
    assert isinstance(out, str), "must return the error string, not a model"


def test_out_of_range_visibility_rejected():
    out = _parse_json(json.dumps({**VALID, "visibility": 150}))
    assert isinstance(out, str)


def test_guard_forces_not_mentioned_when_tokens_absent():
    res = AnalysisResult.model_validate(VALID)
    guarded = annotate_with_checklist(res, "Great options from Osprey and REI this season.", ["aurora", "trail"])
    assert guarded["recommendation"] == "not_mentioned"
    assert guarded["visibility"] == 0
    assert guarded["rank"] is None
    assert guarded["mentions_brand_in_text"] is False


def test_guard_keeps_claim_when_text_ment():
    res = AnalysisResult.model_validate(VALID)
    guarded = annotate_with_checklist(
        res, "The Aurora Trail Backpack comes second at $184 — a strong pick.", ["aurora", "trail"]
    )
    assert guarded["recommendation"] == "strong_alternative"
    assert guarded["rank"] == 2


def test_guard_caps_caveats_and_competitors():
    fat = {**VALID, "caveats": [f"c{i}" for i in range(12)],
           "competitors": [{"name": f"n{i}", "kind": "brand", "mentions": 1} for i in range(12)]}
    res = AnalysisResult.model_validate(fat)
    guarded = annotate_with_checklist(res, "aurora mentioned", ["aurora"])
    assert len(guarded["caveats"]) <= 6
    assert len(guarded["competitors"]) <= 8


async def test_client_retries_once_on_bad_json(monkeypatch):
    client = AnalysisClient("sk-test")
    calls = {"n": 0}

    async def fake_call(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"choices": [{"message": {"content": "I refuse to emit JSON."}}]}
        return {"choices": [{"message": {"content": json.dumps(VALID)}}]}

    monkeypatch.setattr(client, "_call", fake_call)
    res = await client.analyze("answer text", "best shell?", "Kestrel")
    assert isinstance(res, AnalysisResult)
    assert calls["n"] == 2


async def test_client_gives_up_after_two_failures(monkeypatch):
    client = AnalysisClient("sk-test")

    async def fake_call(payload):
        return {"choices": [{"message": {"content": "still not json"}}]}

    monkeypatch.setattr(client, "_call", fake_call)
    with pytest.raises(AnalysisFailed):
        await client.analyze("answer", "q", "brand")


async def test_system_prompt_shape_is_sent():
    """The wire request must contain the ported system prompt + T=0 + JSON mode."""
    seen = {}

    async def fake_call(payload):
        seen.update(payload)
        return {"choices": [{"message": {"content": json.dumps(VALID)}}]}

    client = AnalysisClient("sk-test")
    client._call = AsyncMock(side_effect=fake_call)
    await client.analyze("text", "q", "brand")
    assert seen["temperature"] == 0
    assert seen["response_format"] == {"type": "json_object"}
    assert "STRICT JSON" in seen["messages"][0]["content"]
    assert "competitors" in seen["messages"][0]["content"]
