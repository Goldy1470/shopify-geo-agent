"""Deterministic fix drafting, with an approval queue before anything is pushed.

Fix types:
- title_rewrite: rebuild the title to "Type for Use Case - Vendor" pattern
  derived from product_type + intended_use metafield (no LLM, pure rules).
- description_append: append a specs section derived from attributes.
- faq_generate: produce FAQ items from attributes (dimensions/material/compat).
"""

import json
import re
from datetime import datetime, timezone

from .db import execute, query

FAQ_TEMPLATES = [
    ("material", "What is it made of?", "It is made of {value}."),
    ("dimensions", "What are the dimensions?", "Dimensions: {value}."),
    ("compatibility", "Is it compatible with ...?", "Compatibility: {value}."),
    ("intended_use", "What is it designed for?", "Designed for {value}."),
    ("limitations", "What are the limitations?", "Precautions: {value}."),
]


def draft_title_fix(p: dict) -> dict | None:
    t = (p.get("title") or "").strip()
    use = (p.get("metafields") or {}).get("intended_use") or ""
    ptype = p.get("product_type") or ""
    if not t or not ptype:
        return None
    use_case = None
    for w in ("travel", "commuting", "hiking", "weekend", "rain", "beach", "gym"):
        if w in (use + " " + t).lower():
            use_case = w
            break
    if not re.search(r"for\s+\w", t.lower()) and use_case:
        candidate = f"{ptype} for {use_case} trips - {p.get('vendor')}".replace(" for rain trips", " for rainy weather")
        if candidate.strip() and candidate.strip().lower() != t.lower() and len(candidate) <= 70:
            return {"kind": "title_rewrite", "field": "title", "before": t, "after": candidate}
    if len(t) < 20 and ptype:
        candidate = f"{ptype} - {t}".strip(" -")
        if candidate.lower() != t.lower() and len(candidate) <= 70:
            return {"kind": "title_rewrite", "field": "title", "before": t, "after": candidate}
    if len(re.findall(r"\s[-|]\s", t)) >= 5 and ptype:
        candidate = f"{ptype} - {t.split(' - ')[0]}".strip(" |-")[: 70]
        return {"kind": "title_rewrite", "field": "title", "before": t, "after": candidate}
    return None

def draft_faq_fix(p: dict) -> dict | None:
    if len(p.get("faq") or []) >= 3:
        return None
    mf = p.get("metafields") or {}
    items = [{"q": q, "a": a.format(value=mf[k])} for k, q, a in FAQ_TEMPLATES if mf.get(k)]
    existing_q = {(f.get("q") or "").lower() for f in p.get("faq") or []}
    new = [i for i in items if i["q"].lower() not in existing_q]
    if not new:
        return None
    return {"kind": "faq_generate", "field": "faq", "before": json.dumps(p.get("faq") or []), "after": json.dumps(new)}


def draft_description_fix(p: dict) -> dict | None:
    body = p.get("body_html") or ""
    if len(body) > 1200:
        return None
    mf = p.get("metafields") or {}
    if not mf:
        return None
    rows = "".join(
        f"<li><strong>{k.replace('_', ' ').title()}:</strong> {v}</li>"
        for k, v in mf.items() if v
    )
    addition = f'<div class="geo-specs"><h3>At a glance</h3><ul>{rows}</ul></div>'
    return {"kind": "description_append", "field": "body_html", "before": body, "after": body + addition}


def generate_fixes(products: list[dict]) -> int:
    """Draft fixes for all products into the queue. Returns count created."""
    created = 0
    for p in products:
        for fix in (draft_title_fix(p), draft_faq_fix(p), draft_description_fix(p)):
            if not fix:
                continue
            exists = query(
                "SELECT id FROM fixes WHERE product_id=? AND kind=? AND status='pending'",
                (str(p["id"]), fix["kind"]),
            )
            if exists:
                continue
            execute(
                "INSERT INTO fixes (product_id, kind, field, before, after, status, created_at) VALUES (?,?,?,?,?, 'pending', ?)",
                (
                    str(p["id"]), fix["kind"], fix["field"],
                    fix["before"], fix["after"], datetime.now(timezone.utc).isoformat(),
                ),
            )
            created += 1
    return created


FIX_TITLES = {
    "title_rewrite": "Rewrite title (descriptive, keywords + use case)",
    "faq_generate": "Add FAQ section (from product specs)",
    "description_append": "Append specs checklist to description",
}

APPLY_FIELD = {
    "title_rewrite": "title",
    "description_append": "body_html",
}


def apply_fix(client, fix_id: int) -> dict:
    rows = query("SELECT * FROM fixes WHERE id=?", (fix_id,))
    if not rows:
        return {"ok": False, "error": "not found"}
    fix = rows[0]

    if fix["kind"] == "faq_generate":
        # FAQ entries aren't a native REST product field. Write the merged set
        # to a namespaced metafield (geo_agent.faq_json) a theme block can render;
        # FAQ apps keep their own storage, so this also records the approved state.
        current = client.get_product_metafield(fix["product_id"]) or []
        merge = json.loads(fix["after"])
        existing_q = {(e.get("q") or "").lower() for e in current}
        merged = current + [m for m in merge if m["q"].lower() not in existing_q]
        client.set_product_metafield(fix["product_id"], merged)
        execute("UPDATE fixes SET status='applied', applied_at=? WHERE id=?", (now_iso(), fix_id))
        return {"ok": True, "note": "FAQ items approved and saved to metafield geo_agent.faq_json."}
    field = APPLY_FIELD.get(fix["kind"])
    if not field:
        return {"ok": False, "error": "unknown kind"}
    client.update_product(fix["product_id"], {field: fix["after"]})
    execute("UPDATE fixes SET status='applied', applied_at=? WHERE id=?", (now_iso(), fix_id))
    return {"ok": True, "note": f"Updated {field} in Shopify."}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
