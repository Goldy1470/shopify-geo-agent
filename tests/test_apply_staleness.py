"""Approval flow regressions: staleness check refuses to overwrite merchant edits.

Uses a fake client that simulates the storefront: draft, merchant edit, apply → refused.
"""

from datetime import UTC, datetime

from app.db import execute, query
from app.fixer import apply_fix, draft_description_fix, draft_title_fix


class FakeClient:
    """Minimal stand-in for the Shopify client with persistent state."""

    def __init__(self, product: dict):
        self.product = dict(product)
        self.writes = []

    def get_product(self, product_id):
        return {**self.product}

    def get_product_metafield(self, product_id, namespace="geo_agent", key="faq_json"):
        return self.product.get("faq") or []

    def set_product_metafield(self, product_id, items):
        self.writes.append(("faq", items))
        self.product["faq"] = items

    def update_product(self, product_id, changes):
        self.writes.append(("product", changes))
        self.product.update(changes)
        return {**self.product}


DRAFTABLE = {
    "id": 555,
    "title": "Sundown Shorts",
    "handle": "sundown-shorts",
    "product_type": "Shorts",
    "vendor": "Nimbus Outfitters",
    "body_html": "<p>Casual shorts.</p>",
    "faq": [],
    "metafields": {"material": "cotton twill", "dimensions": "7 in inseam", "compatibility": "Standard sizing", "intended_use": "Beach and gym", "limitations": "Not for water sports"},
}


def _seed_fix(product: dict, fix: dict) -> int:
    execute(
        "INSERT INTO fixes (product_id, kind, field, before, after, status, created_at) VALUES (?,?,?,?,?,'pending',?)",
        (
            str(product["id"]), fix["kind"], fix["field"], fix["before"], fix["after"],
            datetime.now(UTC).isoformat(),
        ),
    )
    return query("SELECT last_insert_rowid() AS id")[0]["id"]


def _draft_title_with_state(product: dict, remote_title: str) -> dict:
    fix = draft_title_fix({**product, "title": remote_title})
    return fix


def test_apply_title_applies_when_unchanged():
    fix = _draft_title_with_state(DRAFTABLE, "Sundown Shorts")
    assert fix is not None, fixture_hint(fix)
    fc = FakeClient({**DRAFTABLE})
    fix_id = _seed_fix(DRAFTABLE, fix)
    result = apply_fix(fc, fix_id)
    assert result["ok"], result
    assert fc.product["title"] == fix["after"]


def fixture_hint(fix):
    return f"title fix should draft for DRAFTABLE (got {fix})"


def test_apply_refuses_when_remote_title_changed():
    fix = _draft_title_with_state(DRAFTABLE, "Sundown Shorts")
    assert fix is not None
    remote = dict(DRAFTABLE)
    remote["title"] = "Merchant Edited Title Happened"
    fc = FakeClient(remote)  # remote differs from the draft snapshot
    fix_id = _seed_fix(DRAFTABLE, fix)
    result = apply_fix(fc, fix_id)
    assert result["ok"] is False
    assert result["error"] == "stale"
    assert fc.writes == [], "stale write must never hit the store"


def test_apply_status_recorded():
    fix = draft_description_fix(DRAFTABLE)
    assert fix is not None
    fc = FakeClient({**DRAFTABLE})
    fix_id = _seed_fix(DRAFTABLE, fix)
    assert apply_fix(fc, fix_id)["ok"]
    status, = query("SELECT status FROM fixes WHERE id=?", (fix_id,))[0]
    assert status == "applied"
