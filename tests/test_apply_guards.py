"""Phase 4 apply guards: manual/unsupported kinds, judge reject, write failures."""

import json
from datetime import UTC, datetime

from app.db import execute, query
from app.fixer import apply_fix

PRODUCT = {
    "id": 777,
    "title": "Harbor Tote",
    "handle": "harbor-tote",
    "product_type": "Tote",
    "vendor": "Nimbus Outfitters",
    "body_html": "<p>Canvas tote for errands.</p>",
    "faq": [],
    "metafields": {"material": "canvas", "dimensions": "16 in", "compatibility": "Everyday", "intended_use": "Errands", "limitations": "Not waterproof"},
}


class Client:
    def __init__(self, product=PRODUCT, write_result=None):
        self.product = None if product is None else dict(product)
        self.write_result = write_result
        self.writes = []

    def get_product(self, product_id):
        return None if self.product is None else {**self.product}

    def get_product_metafield(self, product_id, namespace="geo_agent", key="faq_json"):
        return self.product.get("faq") or []

    def set_product_metafield(self, product_id, items):
        self.writes.append(("faq", items))
        self.product["faq"] = items
        return self.write_result

    def update_product(self, product_id, changes):
        self.writes.append(("product", changes))
        self.product.update(changes)
        return self.write_result or {**self.product}


def _seed(kind, field, before, after, *, auto=1, judge=None, status="pending"):
    execute(
        "INSERT INTO fixes (product_id, kind, field, before, after, status, created_at, auto, judge) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (str(PRODUCT["id"]), kind, field, before, after, status,
         datetime.now(UTC).isoformat(), auto, judge),
    )
    return query("SELECT last_insert_rowid() AS id")[0]["id"]


def test_apply_refuses_manual_and_unsupported_kinds():
    manual = _seed("title_rewrite", "title", "Harbor Tote", "", auto=0)
    result = apply_fix(Client(), manual)
    assert result["ok"] is False
    assert result["error"] == "unsupported_kind"

    seo = _seed("seo_fill_title", "seo_title", "", "Harbor Tote", auto=1)
    result = apply_fix(Client(), seo)
    assert result["ok"] is False
    assert result["error"] == "unsupported_kind"


def test_apply_refuses_judge_rejected_and_not_pending():
    rejected = _seed(
        "title_rewrite", "title", "Harbor Tote", "Harbor Tote - Tote",
        judge=json.dumps({"verdict": "reject", "reason": "invented"}),
    )
    result = apply_fix(Client(), rejected)
    assert result["ok"] is False
    assert result["error"] == "judge_rejected"

    discarded = _seed("title_rewrite", "title", "Harbor Tote", "Harbor Tote - Tote", status="discarded")
    result = apply_fix(Client(), discarded)
    assert result["ok"] is False
    assert result["error"] == "not_pending"


def test_apply_refuses_missing_product():
    fix = _seed("title_rewrite", "title", "Harbor Tote", "Harbor Tote - Tote")
    result = apply_fix(Client(product=None), fix)
    assert result["ok"] is False
    assert result["error"] == "stale"


def test_apply_not_found_and_bad_judge_json():
    assert apply_fix(Client(), 99999) == {"ok": False, "error": "not found"}

    fix = _seed(
        "title_rewrite", "title", "Harbor Tote", "Harbor Tote - Tote", judge="not-json"
    )
    result = apply_fix(Client(), fix)
    assert result["ok"] is True, "unreadable judge metadata must not block a valid draft"


def test_apply_faq_with_invalid_before_falls_back_to_raw_hash():
    fix = _seed("faq_generate", "faq", "not json", json.dumps([{"q": "q", "a": "a"}]))
    result = apply_fix(Client(), fix)
    assert result["ok"] is False
    assert result["error"] == "stale"


def test_apply_faq_writes_metafield():
    before = json.dumps(PRODUCT["faq"])
    after = json.dumps([{"q": "What is it made of?", "a": "It is made of canvas."}])
    fix = _seed("faq_generate", "faq", before, after)
    client = Client()
    result = apply_fix(client, fix)
    assert result["ok"] is True
    assert client.product["faq"] == json.loads(after)
    status = query("SELECT status FROM fixes WHERE id=?", (fix,))[0]["status"]
    assert status == "applied"


def test_apply_faq_rejects_invalid_json():
    fix = _seed("faq_generate", "faq", "[]", "not json")
    result = apply_fix(Client(), fix)
    assert result["ok"] is False
    assert result["error"] == "invalid_after"

    not_list = _seed("faq_generate", "faq", "[]", '{"q": "x"}')
    result = apply_fix(Client(), not_list)
    assert result["ok"] is False
    assert result["error"] == "invalid_after"


def test_apply_surfaces_write_failures():
    fix = _seed("title_rewrite", "title", "Harbor Tote", "Harbor Tote - Tote")
    result = apply_fix(Client(write_result={"ok": False, "errors": ["API down"]}), fix)
    assert result["ok"] is False
    assert result["error"] == "write_failed"
    assert "API down" in result["note"]

    faq_fix = _seed("faq_generate", "faq", "[]", json.dumps([{"q": "q", "a": "a"}]))
    result = apply_fix(Client(write_result={"ok": False, "errors": ["metafield cap"]}), faq_fix)
    assert result["ok"] is False
    assert result["error"] == "write_failed"


def test_apply_title_still_uses_staleness_hash():
    fix = {"kind": "title_rewrite", "field": "title", "before": "Harbor Tote",
           "after": "Harbor Tote - Canvas Tote"}
    fix_id = _seed(fix["kind"], fix["field"], fix["before"], fix["after"])
    remote = {**PRODUCT, "title": "Merchant edited"}
    result = apply_fix(Client(remote), fix_id)
    assert result["ok"] is False
    assert result["error"] == "stale"
