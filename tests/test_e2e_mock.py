"""End-to-end mock-mode loop: sync -> score -> track -> suggest -> apply -> rollback.

No keys, no network: the tracking provider is a deterministic fake and every
write lands in the mock store's mirror. The loop must end with the catalog in
exactly the state it started in.
"""

import asyncio

import pytest

from app import tracking
from app.apply import apply_fix, audit_rows, rollback_fix
from app.db import query
from app.scoring import score_product
from app.shopify import get_catalog
from app.suggest import generate_suggestions

pytestmark = pytest.mark.e2e


class FakeProvider:
    name = "mockai"

    async def ask(self, prompt: str) -> dict:
        return {
            "text": "For rainy trips the Aurora Pack is our top pick. The Drift Jacket is a solid budget alternative.",
            "sources": [],
        }


def test_mock_loop_track_suggest_apply_readback_rollback():
    catalog = get_catalog()
    products = catalog.list_products()
    assert len(products) >= 3
    original_titles = {str(p["id"]): p["title"] for p in products}

    # DIAGNOSE
    scored = [score_product(p) for p in products]
    assert all(0 <= item["score"] <= 100 for item in scored)
    assert any(item["issues"] for item in scored)

    # TRACK (fake provider, same runner path as live engines)
    tracking.add_prompt("best waterproof travel backpack")
    tracked = asyncio.run(
        tracking.run_tracking(
            products,
            cfg={},
            providers={"mockai": FakeProvider()},
            n=1,
            daily_cap=10,
            concurrency=1,
        )
    )
    assert tracked.get("error") is None, tracked
    assert len(tracked["runs"]) == 1
    mentioned = {str(p["product_id"]) for p in tracked["runs"][0]["products"]}
    assert "111" in mentioned
    assert query("SELECT COUNT(*) c FROM visibility_runs")[0]["c"] == 1

    # SUGGEST
    inserted = generate_suggestions(products, cfg={})
    title_fix = next(
        suggestion
        for suggestion in inserted
        if suggestion["kind"] == "title_rewrite" and suggestion["product_id"] == "111"
    )
    assert title_fix["after"].startswith(original_titles["111"])

    # APPLY: dry-run, snapshot, write, read-back, audit
    applied = apply_fix(catalog, title_fix["id"])
    assert applied["ok"] is True, applied
    after_title = catalog.get_product("111")["title"]
    assert after_title == title_fix["after"]
    assert after_title != original_titles["111"]
    assert original_titles["111"] in after_title
    audit = audit_rows(5)
    assert audit[0]["action"] == "apply"
    assert audit[0]["status"] == "ok"
    assert query("SELECT status FROM fixes WHERE id=?", (title_fix["id"],))[0]["status"] == "applied"

    # ROLLBACK: the catalog returns to its exact starting state
    rolled = rollback_fix(catalog, title_fix["id"])
    assert rolled["ok"] is True, rolled
    assert catalog.get_product("111")["title"] == original_titles["111"]
    assert query("SELECT status FROM fixes WHERE id=?", (title_fix["id"],))[0]["status"] == "rolled_back"
    assert audit_rows(5)[0]["action"] == "rollback"

    # Regression guard: no pending suggestion was duplicated by the re-suggest pass
    pending = query("SELECT id FROM fixes WHERE status='pending'")
    assert all(row["id"] != title_fix["id"] for row in pending)
