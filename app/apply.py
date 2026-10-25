"""APPLY pipeline (Phase 5).

One code path for single, bulk and rollback writes:

    dry-run (guards + validators + staleness)
      -> snapshot the live value
      -> GraphQL write
      -> read-back verification
      -> audit log
      -> one-click rollback (single or whole batch)

Hard rules:
- A fix is only written when it is pending, auto-drafted, not judge-rejected,
  still passes the Phase 4 validators (edits included), and the live field still
  matches the value the draft was taken from.
- The snapshot is stored before the write. Read-back failure triggers an
  immediate best-effort restore, so a partial write never silently stays live.
- Rollback refuses when the merchant changed the field after our write (unless
  ``force=True`` is passed by an explicit API caller).
- Bulk runs in chunks and stops early when Shopify throttles, leaving the
  remaining fixes pending instead of hammering the API.
"""

import hashlib
import html
import json
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from .db import execute, query
from .fixer import APPLICABLE_KINDS, APPLY_FIELD
from .suggest import validate_suggestion

BATCH_MAX = 25
_THROTTLE_RE = re.compile(r"throttl|429", re.I)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _load_fix(fix_id: int) -> dict | None:
    rows = query("SELECT * FROM fixes WHERE id=?", (fix_id,))
    return dict(rows[0]) if rows else None


def _draft_state_hash(fix: dict) -> str:
    if fix["kind"] == "faq_generate":
        try:
            return _sha(json.dumps(json.loads(fix["before"] or "[]"), sort_keys=True))
        except ValueError:
            return _sha(fix["before"] or "")
    return _sha(fix["before"] or "")


def _current_remote_hash(client, fix: dict, product: dict | None = None) -> str | None:
    kind = fix["kind"]
    field = APPLY_FIELD.get(kind)
    if field:
        product = product if product is not None else client.get_product(fix["product_id"])
        if product is None:
            return None
        return _sha(product.get(field))
    if kind == "faq_generate":
        current = client.get_product_metafield(fix["product_id"])
        return _sha(json.dumps(current or [], sort_keys=True))
    return None


def _snapshot_value(client, fix: dict, product: dict | None = None) -> Any:
    if fix["kind"] == "faq_generate":
        return client.get_product_metafield(fix["product_id"]) or []
    field = APPLY_FIELD[fix["kind"]]
    product = product if product is not None else client.get_product(fix["product_id"])
    return (product or {}).get(field) or ""


def _encode_snapshot(fix: dict, value: Any) -> str:
    return json.dumps({"field": fix["field"], "value": value}, ensure_ascii=False)


def _decode_snapshot(fix: dict) -> dict | None:
    try:
        snapshot = json.loads(fix["snapshot"] or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict) or "value" not in snapshot:
        return None
    return snapshot


def _normalize_html(value: str | None) -> str:
    return " ".join(html.unescape(value or "").split())


def _matches(fix: dict, value: Any) -> bool:
    if fix["kind"] == "title_rewrite":
        return str(value or "") == (fix["after"] or "")
    if fix["kind"] == "description_append":
        return _normalize_html(value) == _normalize_html(fix["after"])
    if fix["kind"] == "faq_generate":
        try:
            wanted = json.loads(fix["after"] or "[]")
        except ValueError:
            return False
        return _canonical_faq(value) == _canonical_faq(wanted)
    return False


def _canonical_faq(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return sorted(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in items)


def _refusal(fix: dict) -> dict | None:
    """Static guards shared by dry-run and apply (no remote calls)."""
    if fix["status"] != "pending":
        return {
            "ok": False,
            "error": "not_pending",
            "status": fix["status"],
            "fix_id": fix["id"],
            "note": "Only pending fixes can be applied or previewed.",
        }
    if fix["kind"] not in APPLICABLE_KINDS:
        return {
            "ok": False,
            "error": "unsupported_kind",
            "fix_id": fix["id"],
            "kind": fix["kind"],
            "note": (
                f"'{fix['kind']}' is a merchant task in this phase; it is not written "
                "automatically. Copy the suggestion into Shopify admin."
            ),
        }
    # Legacy rows predate the auto column (NULL); only an explicit 0 marks a
    # manual task. Manual tasks have no draft and must never be written.
    if fix["auto"] == 0 or not (fix["after"] or "").strip():
        return {
            "ok": False,
            "error": "unsupported_kind",
            "fix_id": fix["id"],
            "kind": fix["kind"],
            "note": "Manual task: there is no auto-draft to write. Copy the note into Shopify admin.",
        }
    try:
        judge_verdict = json.loads(fix["judge"] or "{}").get("verdict")
    except (TypeError, ValueError):
        judge_verdict = None
    if judge_verdict == "reject":
        return {
            "ok": False,
            "error": "judge_rejected",
            "fix_id": fix["id"],
            "note": "The optional LLM judge rejected this draft. Re-draft or edit it by hand.",
        }
    return None


def _plan(client, fix: dict) -> dict:
    """Internal dry-run: returns the write plan plus the raw snapshot value."""
    refusal = _refusal(fix)
    if refusal:
        return refusal
    product = client.get_product(fix["product_id"])
    if product is None:
        return {
            "ok": False,
            "error": "stale",
            "fix_id": fix["id"],
            "note": "Product no longer exists in the catalog.",
        }
    problems = validate_suggestion(
        {
            "kind": fix["kind"],
            "field": fix["field"],
            "before": fix["before"] or "",
            "after": fix["after"] or "",
            "auto": fix["auto"],
        },
        product,
    )
    if problems:
        return {
            "ok": False,
            "error": "invalid_draft",
            "fix_id": fix["id"],
            "problems": problems,
            "note": "Draft failed the publish validators. Edit it or discard it.",
        }
    expected = _draft_state_hash(fix)
    current = _current_remote_hash(client, fix, product)
    if current is None:
        return {
            "ok": False,
            "error": "stale",
            "fix_id": fix["id"],
            "note": "Product no longer exists in the catalog.",
        }
    if current != expected:
        return {
            "ok": False,
            "error": "stale",
            "fix_id": fix["id"],
            "note": "The live field changed since this fix was drafted. Re-draft instead of overwriting.",
        }
    return {
        "ok": True,
        "fix_id": fix["id"],
        "kind": fix["kind"],
        "field": fix["field"],
        "product_id": fix["product_id"],
        "before": fix["before"] or "",
        "after": fix["after"] or "",
        "stale": False,
        "problems": [],
        "_snapshot": _snapshot_value(client, fix, product),
    }


def dry_run_fix(client, fix_id: int) -> dict:
    """Check guards, validators and staleness without writing or auditing."""
    fix = _load_fix(fix_id)
    if fix is None:
        return {"ok": False, "error": "not found"}
    plan = _plan(client, fix)
    if not plan.get("ok"):
        return plan
    return {
        "ok": True,
        "fix_id": fix_id,
        "kind": plan["kind"],
        "field": plan["field"],
        "product_id": plan["product_id"],
        "before": plan["before"],
        "after": plan["after"],
        "stale": False,
        "would_snapshot": True,
        "note": "Ready to apply.",
    }


def _write(client, fix: dict) -> tuple[bool, str]:
    if fix["kind"] == "faq_generate":
        try:
            items = json.loads(fix["after"] or "")
        except (TypeError, ValueError):
            return False, "FAQ draft is not valid JSON."
        if not isinstance(items, list):
            return False, "FAQ draft must be a JSON list."
        result = client.set_product_metafield(fix["product_id"], items)
    else:
        field = APPLY_FIELD.get(fix["kind"])
        if not field:
            return False, "unsupported kind"
        result = client.update_product(fix["product_id"], {field: fix["after"]})
    if isinstance(result, dict) and result.get("ok") is False:
        return False, "; ".join(result.get("errors") or []) or "write failed"
    return True, ""


def _readback_value(client, fix: dict) -> Any:
    product = client.get_product(fix["product_id"])
    if product is None:
        return None
    if fix["kind"] == "faq_generate":
        value = product.get("faq")
        return client.get_product_metafield(fix["product_id"]) if value is None else value
    return product.get(APPLY_FIELD[fix["kind"]])


def _readback_hash(client, fix: dict) -> tuple[bool, str, Any]:
    value = _readback_value(client, fix)
    if fix["kind"] == "faq_generate":
        payload = json.dumps(value or [], sort_keys=True)
    else:
        payload = value if isinstance(value, str) else ""
    return _matches(fix, value), _sha(payload), value


def _restore(client, fix: dict, snapshot: dict) -> tuple[bool, str]:
    """Write the snapshot back and verify it; returns (ok, note)."""
    value = snapshot.get("value")
    if fix["kind"] == "faq_generate":
        result = client.set_product_metafield(fix["product_id"], value or [])
    else:
        field = APPLY_FIELD.get(fix["kind"])
        if not field:
            return False, "unsupported kind"
        result = client.update_product(fix["product_id"], {field: value or ""})
    if isinstance(result, dict) and result.get("ok") is False:
        return False, "; ".join(result.get("errors") or []) or "restore write failed"
    current = _readback_value(client, fix)
    if fix["kind"] == "faq_generate":
        ok = _canonical_faq(current) == _canonical_faq(value)
    else:
        ok = _normalize_html(current if isinstance(current, str) else "") == _normalize_html(
            value if isinstance(value, str) else ""
        )
    return (True, "restored") if ok else (False, "restore read-back did not match the snapshot")


def _audit(
    fix: dict,
    action: str,
    status: str,
    *,
    batch_id: str | None = None,
    before_hash: str = "",
    after_hash: str = "",
    readback_hash: str = "",
    snapshot: str = "",
    error: str = "",
) -> int:
    cursor = execute(
        "INSERT INTO apply_audit (batch_id, fix_id, product_id, kind, field, action, status, "
        "before_hash, after_hash, readback_hash, snapshot, error, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            batch_id or fix.get("batch_id"),
            fix.get("id"),
            fix.get("product_id"),
            fix.get("kind"),
            fix.get("field"),
            action,
            status,
            before_hash,
            after_hash,
            readback_hash,
            snapshot,
            (error or "")[:1000],
            _now(),
        ),
    )
    return int(cursor.lastrowid or 0)


def _restore_after_failed_write(
    client,
    fix: dict,
    snapshot_value: Any,
    snapshot: str,
    batch_id: str | None,
    *,
    error: str,
    error_code: str = "readback_failed",
    readback_hash: str = "",
) -> dict:
    """A write may have landed even when it raised: restore the snapshot, then audit."""
    restored = False
    restore_note = ""
    try:
        restored, restore_note = _restore(client, fix, {"value": snapshot_value})
    except Exception as exc:  # client/network failure during the safety restore
        restore_note = f"restore error: {exc}"
    _audit(
        fix,
        "apply",
        "failed",
        batch_id=batch_id,
        before_hash=_sha(fix.get("before")),
        after_hash=_sha(fix.get("after")),
        readback_hash=readback_hash,
        snapshot=snapshot,
        error=error[:500],
    )
    _audit(
        fix,
        "rollback",
        "ok" if restored else "failed",
        batch_id=batch_id,
        snapshot=snapshot,
        error="" if restored else restore_note[:500],
    )
    execute(
        "UPDATE fixes SET status='failed', snapshot=?, batch_id=?, rolled_back_at=? WHERE id=?",
        (snapshot, batch_id, _now() if restored else None, fix["id"]),
    )
    return {
        "ok": False,
        "error": error_code,
        "fix_id": fix["id"],
        "note": (
            "The write could not be verified; the snapshot was restored."
            if restored
            else "The write could not be verified and the automatic restore failed. "
            "Check the product in Shopify admin."
        ),
        "rolled_back": restored,
    }


def apply_fix(client, fix_id: int, batch_id: str | None = None) -> dict:
    """Apply one approved fix: dry-run, snapshot, write, read-back, audit."""
    fix = _load_fix(fix_id)
    if fix is None:
        return {"ok": False, "error": "not found"}

    plan = _plan(client, fix)
    if not plan.get("ok"):
        if plan.get("error") != "not_pending":
            _audit(
                fix,
                "apply",
                "refused",
                batch_id=batch_id,
                error=plan.get("error", ""),
                before_hash=_sha(fix.get("before")),
                after_hash=_sha(fix.get("after")),
            )
        return plan

    snapshot_value = plan["_snapshot"]
    snapshot = _encode_snapshot(fix, snapshot_value)
    try:
        wrote, write_error = _write(client, fix)
    except Exception as exc:
        return _restore_after_failed_write(
            client,
            fix,
            snapshot_value,
            snapshot,
            batch_id,
            error=f"write error: {exc}",
            error_code="write_failed",
        )
    if not wrote:
        _audit(
            fix,
            "apply",
            "failed",
            batch_id=batch_id,
            before_hash=_sha(fix.get("before")),
            after_hash=_sha(fix.get("after")),
            snapshot=snapshot,
            error=write_error,
        )
        return {
            "ok": False,
            "error": "write_failed",
            "fix_id": fix_id,
            "note": write_error,
        }

    try:
        matched, readback_hash, readback_value = _readback_hash(client, fix)
    except Exception as exc:
        return _restore_after_failed_write(
            client,
            fix,
            snapshot_value,
            snapshot,
            batch_id,
            error=f"read-back error: {exc}",
        )
    if not matched:
        return _restore_after_failed_write(
            client,
            fix,
            snapshot_value,
            snapshot,
            batch_id,
            error=f"read-back mismatch: {readback_value!r}",
            readback_hash=readback_hash,
        )

    execute(
        "UPDATE fixes SET status='applied', applied_at=?, snapshot=?, batch_id=? WHERE id=?",
        (_now(), snapshot, batch_id, fix_id),
    )
    audit_id = _audit(
        fix,
        "apply",
        "ok",
        batch_id=batch_id,
        before_hash=_sha(fix.get("before")),
        after_hash=_sha(fix.get("after")),
        readback_hash=readback_hash,
        snapshot=snapshot,
    )
    return {
        "ok": True,
        "fix_id": fix_id,
        "kind": fix["kind"],
        "field": fix["field"],
        "product_id": fix["product_id"],
        "readback": readback_value,
        "snapshot_saved": True,
        "rollback_available": True,
        "audit_id": audit_id,
        "batch_id": batch_id,
        "note": "Written and verified by read-back.",
    }


def rollback_fix(client, fix_id: int, *, force: bool = False) -> dict:
    """Restore the pre-apply snapshot for one applied fix."""
    fix = _load_fix(fix_id)
    if fix is None:
        return {"ok": False, "error": "not found"}
    if fix["status"] != "applied":
        return {
            "ok": False,
            "error": "not_applied",
            "fix_id": fix_id,
            "status": fix["status"],
            "note": "Only applied fixes can be rolled back.",
        }
    snapshot = _decode_snapshot(fix)
    if snapshot is None:
        return {
            "ok": False,
            "error": "no_snapshot",
            "fix_id": fix_id,
            "note": "No snapshot was stored for this fix, so it cannot be rolled back safely.",
        }
    if not force:
        current = _readback_value(client, fix)
        if not _matches(fix, current):
            return {
                "ok": False,
                "error": "changed_since_apply",
                "fix_id": fix_id,
                "note": "The live field changed after this fix was applied; refusing to overwrite "
                "the newer edit. Use force only after checking Shopify admin.",
            }
    restored, restore_note = _restore(client, fix, snapshot)
    _audit(
        fix,
        "rollback",
        "ok" if restored else "failed",
        before_hash=_sha(fix.get("before")),
        after_hash=_sha(fix.get("after")),
        snapshot=fix["snapshot"],
        error="" if restored else restore_note,
    )
    if not restored:
        return {
            "ok": False,
            "error": "rollback_failed",
            "fix_id": fix_id,
            "note": restore_note,
        }
    execute(
        "UPDATE fixes SET status='rolled_back', rolled_back_at=? WHERE id=?",
        (_now(), fix_id),
    )
    return {
        "ok": True,
        "fix_id": fix_id,
        "product_id": fix["product_id"],
        "kind": fix["kind"],
        "restored": snapshot.get("value"),
        "note": "Snapshot restored and verified by read-back.",
    }


def _rate_limited(results: list[dict]) -> bool:
    for result in results:
        text = f"{result.get('error') or ''} {result.get('note') or ''}"
        if _THROTTLE_RE.search(text):
            return True
    return False


def apply_batch(
    client,
    fix_ids: list[int],
    *,
    batch_size: int = 10,
    pause: float = 0.0,
) -> dict:
    """Apply approved fixes in chunks; stop early if Shopify throttles us."""
    ids = list(dict.fromkeys(int(fix_id) for fix_id in fix_ids))
    if not ids:
        return {"ok": False, "error": "no_fixes", "note": "Select at least one fix."}
    batch_id = uuid.uuid4().hex[:12]
    execute(
        "INSERT INTO apply_batches (id, created_at, status, total, applied, failed, note) "
        "VALUES (?,?,?,?,0,0,'')",
        (batch_id, _now(), "running", len(ids)),
    )
    size = max(1, min(int(batch_size or 10), BATCH_MAX))
    results: list[dict] = []
    applied = failed = 0
    stopped: str | None = None
    for start in range(0, len(ids), size):
        chunk = ids[start : start + size]
        chunk_results = []
        for fix_id in chunk:
            result = apply_fix(client, fix_id, batch_id=batch_id)
            chunk_results.append(result)
            results.append(result)
            if result.get("ok"):
                applied += 1
            else:
                failed += 1
        if _rate_limited(chunk_results) and start + size < len(ids):
            stopped = "rate_limited"
            break
        if pause and start + size < len(ids):
            time.sleep(pause)
    status = "applied" if applied == len(ids) else ("partial" if applied else "failed")
    note = "stopped early after a Shopify throttle; remaining fixes stay pending" if stopped else ""
    execute(
        "UPDATE apply_batches SET finished_at=?, status=?, applied=?, failed=?, note=? WHERE id=?",
        (_now(), status, applied, failed, note, batch_id),
    )
    return {
        "ok": failed == 0 and stopped is None,
        "batch_id": batch_id,
        "status": status,
        "total": len(ids),
        "applied": applied,
        "failed": failed,
        "stopped": stopped,
        "note": note,
        "results": results,
    }


def rollback_batch(client, batch_id: str) -> dict:
    """Roll back every still-applied fix in a batch, newest first."""
    rows = query(
        "SELECT id FROM fixes WHERE batch_id=? AND status='applied' "
        "ORDER BY applied_at DESC, id DESC",
        (batch_id,),
    )
    if not rows:
        return {
            "ok": False,
            "error": "nothing_to_rollback",
            "batch_id": batch_id,
            "note": "No applied fixes remain in this batch.",
        }
    results = []
    rolled = failed = 0
    for row in rows:
        result = rollback_fix(client, int(row["id"]))
        results.append(result)
        if result.get("ok"):
            rolled += 1
        else:
            failed += 1
    status = "rolled_back" if failed == 0 else "rollback_partial"
    execute(
        "UPDATE apply_batches SET status=?, note=? WHERE id=?",
        (status, f"rolled back {rolled}/{len(rows)}", batch_id),
    )
    return {
        "ok": failed == 0,
        "batch_id": batch_id,
        "status": status,
        "rolled_back": rolled,
        "failed": failed,
        "results": results,
    }


def bulk_discard(fix_ids: list[int]) -> dict:
    ids = list(dict.fromkeys(int(fix_id) for fix_id in fix_ids))
    if not ids:
        return {"ok": False, "error": "no_fixes", "discarded": 0}
    placeholders = ",".join("?" for _ in ids)
    cursor = execute(
        f"UPDATE fixes SET status='discarded' WHERE status='pending' AND id IN ({placeholders})",
        tuple(ids),
    )
    return {"ok": True, "discarded": int(cursor.rowcount or 0)}


def update_draft(client, fix_id: int, after: str) -> dict:
    """Edit-before-approve: store a merchant edit only if it still validates."""
    fix = _load_fix(fix_id)
    if fix is None:
        return {"ok": False, "error": "not found"}
    refusal = _refusal(fix)
    if refusal:
        return refusal
    new_after = (after or "").strip()
    if not new_after:
        return {"ok": False, "error": "invalid_draft", "problems": ["empty value"]}
    product = client.get_product(fix["product_id"])
    if product is None:
        return {"ok": False, "error": "stale", "note": "Product no longer exists in the catalog."}
    problems = validate_suggestion(
        {
            "kind": fix["kind"],
            "field": fix["field"],
            "before": fix["before"] or "",
            "after": new_after,
            "auto": fix["auto"],
        },
        product,
    )
    if problems:
        return {"ok": False, "error": "invalid_draft", "problems": problems}
    notes = (fix.get("notes") or "").strip()
    if "Edited before approval" not in notes:
        notes = f"{notes} Edited before approval.".strip()
    execute(
        "UPDATE fixes SET after=?, judge=NULL, notes=? WHERE id=?",
        (new_after, notes, fix_id),
    )
    return {"ok": True, "fix_id": fix_id, "after": new_after, "note": "Draft saved."}


def recent_batches(limit: int = 10) -> list[dict]:
    return [
        dict(row)
        for row in query(
            "SELECT * FROM apply_batches ORDER BY created_at DESC LIMIT ?", (max(1, int(limit)),)
        )
    ]


def audit_rows(limit: int = 50, batch_id: str | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    if batch_id:
        rows = query(
            "SELECT * FROM apply_audit WHERE batch_id=? ORDER BY id DESC LIMIT ?",
            (batch_id, limit),
        )
    else:
        rows = query("SELECT * FROM apply_audit ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(row) for row in rows]


def batch_fixes(batch_id: str) -> list[dict]:
    return [
        dict(row)
        for row in query(
            "SELECT * FROM fixes WHERE batch_id=? ORDER BY id", (batch_id,)
        )
    ]
