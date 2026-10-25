"""Phase 5 APPLY pipeline: dry-run, snapshots, read-back, audit, rollback, bulk."""

import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime

from app.apply import (
    apply_batch,
    apply_fix,
    audit_rows,
    bulk_discard,
    dry_run_fix,
    recent_batches,
    rollback_batch,
    rollback_fix,
    update_draft,
)
from app.db import execute, legacy, query


def product(pid="777", title="Harbor Tote", **over):
    p = {
        "id": pid,
        "title": title,
        "handle": f"product-{pid}",
        "product_type": "Tote",
        "vendor": "Nimbus Outfitters",
        "body_html": "<p>Canvas tote for errands.</p>",
        "seo_title": "",
        "seo_description": "",
        "images": [],
        "variants": [],
        "metafields": {
            "material": "canvas",
            "dimensions": "16 in",
            "compatibility": "Everyday",
            "intended_use": "Errands",
            "limitations": "Not waterproof",
        },
        "faq": [],
        "reviews": {"count": 0},
    }
    p.update(over)
    return p


class Store:
    """Stateful stand-in for CatalogService, including fault injection."""

    def __init__(self, products=None):
        self.products = {str(p["id"]): deepcopy(p) for p in (products or [product()])}
        self.writes: list[tuple] = []
        self.throttle_after: int | None = None
        self.corrupt_once: dict | None = None
        self.fail_writes = False
        self.raise_write_once = False
        self.raise_readback_after_write = False
        self.raise_restore = False

    def _fail_next(self) -> bool:
        if self.fail_writes:
            return True
        return self.throttle_after is not None and len(self.writes) >= self.throttle_after

    def get_product(self, product_id):
        if self.raise_readback_after_write and self.writes:
            self.raise_readback_after_write = False
            raise RuntimeError("read-back network down")
        found = self.products.get(str(product_id))
        return None if found is None else deepcopy(found)

    def get_product_metafield(self, product_id, namespace="geo_agent", key="faq_json"):
        found = self.products.get(str(product_id))
        return None if found is None else deepcopy(found.get("faq") or [])

    def set_product_metafield(self, product_id, items):
        found = self.products.get(str(product_id))
        fail = self._fail_next()
        self.writes.append((str(product_id), "faq", deepcopy(items)))
        if found is None:
            return {"ok": False, "errors": ["not found"]}
        if fail:
            return {"ok": False, "errors": ["THROTTLED"]}
        found["faq"] = deepcopy(items)
        if self.corrupt_once:
            found.update(self.corrupt_once)
            self.corrupt_once = None
        return {"ok": True}

    def update_product(self, product_id, changes):
        found = self.products.get(str(product_id))
        if self.raise_write_once:
            self.raise_write_once = False
            raise RuntimeError("write network down")
        if self.raise_restore and self.writes:
            raise RuntimeError("restore network down")
        fail = self._fail_next()
        self.writes.append((str(product_id), deepcopy(changes)))
        if found is None:
            return {"ok": False, "errors": ["not found"]}
        if fail:
            return {"ok": False, "errors": ["THROTTLED"]}
        found.update(changes)
        if self.corrupt_once:
            found.update(self.corrupt_once)
            self.corrupt_once = None
        return {"ok": True}


def _seed(
    kind,
    field,
    before,
    after,
    *,
    product_id="777",
    auto=1,
    status="pending",
    judge=None,
    snapshot=None,
    batch_id=None,
):
    execute(
        "INSERT INTO fixes (product_id, kind, field, before, after, status, created_at, auto, "
        "judge, snapshot, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(product_id),
            kind,
            field,
            before,
            after,
            status,
            datetime.now(UTC).isoformat(),
            auto,
            judge,
            snapshot,
            batch_id,
        ),
    )
    return int(query("SELECT last_insert_rowid() AS id")[0]["id"])


def _title_fix(pid="777", before="Harbor Tote", after="Harbor Tote - Tote"):
    return _seed("title_rewrite", "title", before, after, product_id=pid)


# --- dry-run -----------------------------------------------------------------


def test_dry_run_checks_guards_validators_and_staleness_without_writing():
    store = Store()
    fix = _title_fix()
    plan = dry_run_fix(store, fix)
    assert plan["ok"] is True
    assert plan["would_snapshot"] is True
    assert store.writes == []

    stale = _seed("title_rewrite", "title", "Old title", "Old title - Tote")
    result = dry_run_fix(store, stale)
    assert result["error"] == "stale"

    invalid = _seed("title_rewrite", "title", "Harbor Tote", "Completely Different Product")
    result = dry_run_fix(store, invalid)
    assert result["error"] == "invalid_draft"
    assert result["problems"]


def test_dry_run_reports_manual_and_missing_fixes():
    manual = _seed("alt_text", "image:1", "", "Harbor Tote — product image 1 of 1", auto=0)
    assert dry_run_fix(Store(), manual)["error"] == "unsupported_kind"
    assert dry_run_fix(Store(), 99999)["error"] == "not found"


# --- single apply ------------------------------------------------------------


def test_apply_snapshots_writes_reads_back_and_audits():
    store = Store()
    fix = _title_fix()
    result = apply_fix(store, fix)
    assert result["ok"] is True, result
    assert result["readback"] == "Harbor Tote - Tote"
    assert result["snapshot_saved"] is True
    assert result["rollback_available"] is True

    row = query("SELECT * FROM fixes WHERE id=?", (fix,))[0]
    assert row["status"] == "applied"
    assert json.loads(row["snapshot"])["value"] == "Harbor Tote"
    assert store.products["777"]["title"] == "Harbor Tote - Tote"

    audit = query("SELECT * FROM apply_audit WHERE fix_id=? ORDER BY id", (fix,))[0]
    assert audit["action"] == "apply"
    assert audit["status"] == "ok"
    assert audit["readback_hash"]
    assert result["audit_id"] == audit["id"]


def test_apply_faq_writes_metafield_and_audits():
    store = Store()
    after = json.dumps([{"q": "What is it made of?", "a": "It is made of canvas."}])
    fix = _seed("faq_generate", "faq", "[]", after)
    result = apply_fix(store, fix)
    assert result["ok"] is True, result
    assert store.products["777"]["faq"] == json.loads(after)
    assert query("SELECT status FROM fixes WHERE id=?", (fix,))[0]["status"] == "applied"


def test_readback_mismatch_auto_restores_snapshot():
    store = Store()
    store.corrupt_once = {"title": "Corrupted read-back"}
    fix = _title_fix()
    result = apply_fix(store, fix)
    assert result["ok"] is False
    assert result["error"] == "readback_failed"
    assert result["rolled_back"] is True
    assert store.products["777"]["title"] == "Harbor Tote"

    row = query("SELECT * FROM fixes WHERE id=?", (fix,))[0]
    assert row["status"] == "failed"
    actions = [
        (a["action"], a["status"])
        for a in query("SELECT * FROM apply_audit WHERE fix_id=? ORDER BY id", (fix,))
    ]
    assert actions == [("apply", "failed"), ("rollback", "ok")]


def test_write_exception_restores_snapshot_and_audits():
    store = Store()
    store.raise_write_once = True
    fix = _title_fix()
    result = apply_fix(store, fix)
    assert result["ok"] is False
    assert result["error"] == "write_failed"
    assert result["rolled_back"] is True
    assert store.products["777"]["title"] == "Harbor Tote"
    assert query("SELECT status FROM fixes WHERE id=?", (fix,))[0]["status"] == "failed"

    actions = [
        (a["action"], a["status"])
        for a in query("SELECT * FROM apply_audit WHERE fix_id=? ORDER BY id", (fix,))
    ]
    assert actions == [("apply", "failed"), ("rollback", "ok")]


def test_readback_exception_restores_snapshot():
    store = Store()
    store.raise_readback_after_write = True
    fix = _title_fix()
    result = apply_fix(store, fix)
    assert result["ok"] is False
    assert result["error"] == "readback_failed"
    assert result["rolled_back"] is True
    assert store.products["777"]["title"] == "Harbor Tote"
    assert query("SELECT status FROM fixes WHERE id=?", (fix,))[0]["status"] == "failed"


def test_failed_restore_marks_fix_failed_and_reports():
    store = Store()
    store.corrupt_once = {"title": "Corrupted read-back"}
    store.raise_restore = True
    fix = _title_fix()
    result = apply_fix(store, fix)
    assert result["ok"] is False
    assert result["error"] == "readback_failed"
    assert result["rolled_back"] is False
    assert store.products["777"]["title"] == "Corrupted read-back"

    row = query("SELECT * FROM fixes WHERE id=?", (fix,))[0]
    assert row["status"] == "failed"
    assert row["rolled_back_at"] is None
    audits = query("SELECT * FROM apply_audit WHERE fix_id=? ORDER BY id", (fix,))
    assert [(a["action"], a["status"]) for a in audits] == [
        ("apply", "failed"),
        ("rollback", "failed"),
    ]
    assert "restore error" in audits[1]["error"]


def test_write_failure_leaves_fix_pending_and_audits():
    store = Store()
    store.fail_writes = True
    fix = _title_fix()
    result = apply_fix(store, fix)
    assert result["error"] == "write_failed"
    assert query("SELECT status FROM fixes WHERE id=?", (fix,))[0]["status"] == "pending"
    audit = query("SELECT * FROM apply_audit WHERE fix_id=? ORDER BY id", (fix,))[0]
    assert audit["status"] == "failed"


# --- rollback ----------------------------------------------------------------


def test_rollback_restores_snapshot_and_refuses_second_time():
    store = Store()
    fix = _title_fix()
    assert apply_fix(store, fix)["ok"] is True

    rolled = rollback_fix(store, fix)
    assert rolled["ok"] is True
    assert rolled["restored"] == "Harbor Tote"
    assert store.products["777"]["title"] == "Harbor Tote"
    assert query("SELECT status FROM fixes WHERE id=?", (fix,))[0]["status"] == "rolled_back"

    again = rollback_fix(store, fix)
    assert again["error"] == "not_applied"
    audit = query("SELECT action, status FROM apply_audit WHERE fix_id=? ORDER BY id", (fix,))
    assert ("rollback", "ok") in [(a["action"], a["status"]) for a in audit]


def test_rollback_refuses_newer_merchant_edit_unless_forced():
    store = Store()
    fix = _title_fix()
    assert apply_fix(store, fix)["ok"] is True
    store.products["777"]["title"] = "Merchant edit"

    refused = rollback_fix(store, fix)
    assert refused["error"] == "changed_since_apply"
    assert store.products["777"]["title"] == "Merchant edit"

    forced = rollback_fix(store, fix, force=True)
    assert forced["ok"] is True
    assert store.products["777"]["title"] == "Harbor Tote"


# --- edit before approve -----------------------------------------------------


def test_edit_before_approve_revalidates_and_clears_judge():
    store = Store()
    fix = _title_fix()
    execute(
        "UPDATE fixes SET judge=? WHERE id=?",
        (json.dumps({"verdict": "approve", "reason": "ok"}), fix),
    )

    bad = update_draft(store, fix, "Completely Different Product")
    assert bad["error"] == "invalid_draft"
    assert query("SELECT after FROM fixes WHERE id=?", (fix,))[0]["after"] == "Harbor Tote - Tote"

    good = update_draft(store, fix, "Harbor Tote - Canvas Tote")
    assert good["ok"] is True
    row = query("SELECT after, judge, notes FROM fixes WHERE id=?", (fix,))[0]
    assert row["after"] == "Harbor Tote - Canvas Tote"
    assert row["judge"] is None
    assert "Edited before approval" in row["notes"]

    applied = apply_fix(store, fix)
    assert applied["ok"] is True, applied
    assert store.products["777"]["title"] == "Harbor Tote - Canvas Tote"


def test_edit_refuses_empty_and_manual_tasks():
    store = Store()
    fix = _title_fix()
    assert update_draft(store, fix, "   ")["error"] == "invalid_draft"
    manual = _seed("alt_text", "image:1", "", "x", auto=0)
    assert update_draft(store, manual, "Harbor Tote — product image 1 of 1")["error"] == "unsupported_kind"


# --- bulk --------------------------------------------------------------------


def test_bulk_partial_failure_and_batch_rollback():
    store = Store([product("777"), product("778", "Trail Pack")])
    good = _title_fix("777")
    bad = _seed(
        "title_rewrite", "title", "Ghost", "Ghost - Tote", product_id="999"
    )
    result = apply_batch(store, [good, bad])
    assert result["ok"] is False
    assert result["status"] == "partial"
    assert result["applied"] == 1
    assert result["failed"] == 1
    assert store.products["777"]["title"] == "Harbor Tote - Tote"

    batch = query("SELECT * FROM apply_batches WHERE id=?", (result["batch_id"],))[0]
    assert batch["status"] == "partial"
    assert query("SELECT batch_id FROM fixes WHERE id=?", (good,))[0]["batch_id"] == result["batch_id"]

    rolled = rollback_batch(store, result["batch_id"])
    assert rolled["ok"] is True
    assert rolled["rolled_back"] == 1
    assert store.products["777"]["title"] == "Harbor Tote"
    assert query("SELECT status FROM apply_batches WHERE id=?", (result["batch_id"],))[0]["status"] == "rolled_back"


def test_bulk_stops_early_on_throttle():
    store = Store([product("777"), product("778", "Trail Pack"), product("779", "City Sling")])
    store.throttle_after = 1
    ids = [
        _title_fix("777", "Harbor Tote", "Harbor Tote - Tote"),
        _title_fix("778", "Trail Pack", "Trail Pack - Tote"),
        _title_fix("779", "City Sling", "City Sling - Tote"),
    ]
    result = apply_batch(store, ids, batch_size=1)
    assert result["stopped"] == "rate_limited"
    assert result["applied"] == 1
    assert result["failed"] == 1
    assert store.products["777"]["title"] == "Harbor Tote - Tote"
    assert store.products["778"]["title"] == "Trail Pack"
    assert store.products["779"]["title"] == "City Sling"
    assert query("SELECT status FROM fixes WHERE id=?", (ids[2],))[0]["status"] == "pending"
    assert query("SELECT COUNT(*) c FROM apply_audit WHERE fix_id=?", (ids[2],))[0]["c"] == 0


def test_bulk_discard_only_touches_pending():
    pending = _title_fix("777")
    applied = _title_fix("778", "Trail Pack", "Trail Pack - Tote")
    execute("UPDATE fixes SET status='applied' WHERE id=?", (applied,))
    result = bulk_discard([pending, applied, 99999])
    assert result["discarded"] == 1
    assert query("SELECT status FROM fixes WHERE id=?", (pending,))[0]["status"] == "discarded"
    assert query("SELECT status FROM fixes WHERE id=?", (applied,))[0]["status"] == "applied"
    assert bulk_discard([])["ok"] is False


def test_batches_and_audit_are_listed_newest_first():
    store = Store([product("777"), product("778", "Trail Pack")])
    first = _title_fix("777")
    second = _title_fix("778", "Trail Pack", "Trail Pack - Tote")
    assert apply_fix(store, first)["ok"] is True
    assert apply_fix(store, second)["ok"] is True
    rows = audit_rows(10)
    assert [r["fix_id"] for r in rows] == [second, first]
    assert audit_rows(10, batch_id="nope") == []
    assert recent_batches(5) == []


def test_init_db_migrates_pre_phase5_database(tmp_path, monkeypatch):
    old_path = tmp_path / "old.sqlite"
    old = sqlite3.connect(old_path)
    old.executescript(
        """
        CREATE TABLE fixes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT, kind TEXT, field TEXT, before TEXT, after TEXT,
            status TEXT DEFAULT 'pending', created_at TEXT, applied_at TEXT,
            impact REAL, severity TEXT, auto INTEGER, notes TEXT, judge TEXT
        );
        """
    )
    old.commit()
    old.close()

    monkeypatch.setattr(legacy, "DB_PATH", str(old_path))
    legacy._local.conn = None
    legacy.init_db()

    check = sqlite3.connect(old_path)
    try:
        columns = {row[1] for row in check.execute("PRAGMA table_info(fixes)")}
        tables = {
            row[0]
            for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0]
            for row in check.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        check.close()
    assert {"snapshot", "batch_id", "rolled_back_at"} <= columns
    assert {"apply_batches", "apply_audit"} <= tables
    assert {"idx_fixes_batch", "idx_apply_audit_created"} <= indexes
