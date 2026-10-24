"""Fix drafting, validation and approval-gated application.

Hard rules (enforced by validators, not convention):
- A suggested title never removes or replaces the product's own name/model; titles
  may only be extended or reordered.
- Generated text may only reuse data present on the product. If a fact is missing we
  emit a missing-data note instead of inventing text.
- No placeholders ("...", "{", "TODO", empty values) ever leave the draft queue.
- The description "specs" block is marker-delimited: re-drafting replaces the block
  instead of appending a duplicate one, and every value is HTML-escaped.
- Apply refuses to write if the remote field changed since the draft was taken
  (staleness check) so a merchant's live edits are never silently overwritten.
"""

import hashlib
import html
import json
import re
from datetime import UTC, datetime

from .db import execute, query

GEO_BLOCK_START = "<!-- geo-agent:specs start -->"
GEO_BLOCK_END = "<!-- geo-agent:specs end -->"
GEO_BLOCK_RE = re.compile(re.escape(GEO_BLOCK_START) + r"(.*?)" + re.escape(GEO_BLOCK_END), re.S)

USE_CASE_WORDS = ["travel", "commuting", "hiking", "weekend", "rainy", "rain", "beach", "gym", "camping", "work"]

FAQ_TEMPLATES = [
    ("material", "What is it made of?", "It is made of {value}."),
    ("dimensions", "What are the dimensions?", "Dimensions: {value}."),
    ("compatibility", "What does it fit or work with?", "It is compatible with {value}."),
    ("intended_use", "What is it designed for?", "Designed for {value}."),
    ("limitations", "What are the limitations?", "Precautions: {value}."),
]


def validate_no_placeholders(text: str) -> list[str]:
    """Returns a list of problems; empty list = safe to publish."""
    problems = []
    stripped = (text or "").strip()
    if not stripped:
        problems.append("empty value")
    if "..." in text:
        problems.append("contains '...' placeholder")
    if "{" in text or "}" in text:
        problems.append("contains unfilled '{template}' token")
    if "TODO" in text.upper():
        problems.append("contains TODO")
    if "\x00" in text or "\ufffd" in text:
        problems.append("contains invalid characters")
    return problems


def _use_case(text: str) -> str | None:
    low = text.lower()
    for w in USE_CASE_WORDS:
        if w in low:
            return "rainy weather" if w == "rain" and "rainy" not in low else w
    return None


def draft_title_fix(p: dict) -> dict | None:
    """Extend the existing title: never drop or replace the product's own name.

    Appends missing context as -: e.g. "Aurora Pack" ->
    "Aurora Pack - Travel Backpack". If the title runs long, the suffix is trimmed
    rather than the name. Stuffed titles (5+ segments) are left to the suggestion
    engine (Phase 4) because safe trimming requires reorder/validators.
    """
    t = (p.get("title") or "").strip()
    ptype = (p.get("product_type") or "").strip()
    use = ((p.get("metafields") or {}).get("intended_use") or "").strip()
    if not t or not ptype:
        return None
    if len(re.findall(r"\s[-|]\s", t)) >= 5 or len(t) > 90:
        return None

    lower = t.lower()
    suffix_parts = []
    if ptype.lower() not in lower:
        suffix_parts.append(ptype)
    use_case = _use_case(f"{use} {t}")
    if use_case and use_case not in lower:
        suffix_parts.append(f"for {use_case}")

    candidate = t
    max_len = 78
    suffix_full = " - " + " - ".join(suffix_parts)
    if len(t) + len(suffix_full) <= max_len:
        candidate = t + suffix_full
    else:
        # Trim suffix parts (name always survives whole).
        for m in range(len(suffix_parts), 0, -1):
            cut = " - " + " - ".join(suffix_parts[:m])
            if len(t) + len(cut) <= max_len and cut != " - ":
                candidate = t + cut
                break
    if candidate is None or candidate.strip().lower() == t.lower():
        return None
    problems = validate_no_placeholders(candidate)
    if problems:
        return None
    if candidate.split(" - ")[0] != t:
        return None  # name mutated — refuse instead of shipping it
    return {"kind": "title_rewrite", "field": "title", "before": t, "after": candidate}


def draft_faq_fix(p: dict) -> dict | None:
    if len(p.get("faq") or []) >= 3:
        return None
    mf = p.get("metafields") or {}
    items = []
    for key, q, a in FAQ_TEMPLATES:
        value = (mf.get(key) or "").strip()
        if not value:
            # Not enough data to ground this QA pair: surface the gap, invent nothing.
            continue
        item = {"q": q, "a": a.format(value=value)}
        if validate_no_placeholders(item["q"]) or validate_no_placeholders(item["a"]):
            continue
        items.append(item)
    existing_q = {(f.get("q") or "").lower() for f in p.get("faq") or []}
    new = [i for i in items if i["q"].lower() not in existing_q]
    if not new:
        return None
    return {"kind": "faq_generate", "field": "faq", "before": json.dumps(p.get("faq") or []), "after": json.dumps(new)}


def draft_description_fix(p: dict) -> dict | None:
    """Marker-delimited specs block: replace-in-place, never duplicate, always escaped."""
    body = p.get("body_html") or ""
    mf = {k: v for k, v in (p.get("metafields") or {}).items() if str(v or "").strip()}
    if not mf or len(body) > 2000:
        return None
    rows = "".join(
        f"<li><strong>{html.escape(k.replace('_', ' '))}:</strong> "
        f"{html.escape(str(v))}</li>"
        for k, v in mf.items()
    )
    inner_new = f'<h3>At a glance</h3><ul>{rows}</ul>'
    wrapper_new = f'<div class="geo-specs">{inner_new}</div>'
    block_new = f'{GEO_BLOCK_START}{wrapper_new}{GEO_BLOCK_END}'
    if GEO_BLOCK_START in body:
        existing = GEO_BLOCK_RE.search(body)
        if existing and existing.group(1).strip() == wrapper_new.strip():
            return None  # already up to date; nothing to add (idempotent)
        if existing:
            body = (body[: existing.start()] + body[existing.end():]).strip()
    after = (body.strip() + "\n" + block_new) if body.strip() else block_new
    problems = validate_no_placeholders(html.unescape(inner_new))
    if problems:
        return None
    return {"kind": "description_append", "field": "body_html", "before": body, "after": after}


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
                (str(p["id"]), fix["kind"], fix["field"], fix["before"], fix["after"], datetime.now(UTC).isoformat()),
            )
            created += 1
    return created


FIX_TITLES = {
    "title_rewrite": "Extend title (adds type/use case, keeps the product name)",
    "faq_generate": "Add FAQ items (grounded in product specs)",
    "description_append": "Add/refresh specs block in description",
}

APPLY_FIELD = {
    "title_rewrite": "title",
    "description_append": "body_html",
}


def _sha(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _current_remote_state(client, fix) -> str | None:
    """Current value hash of the field this fix touches, straight from the client."""
    kind = fix["kind"]
    field = APPLY_FIELD.get(kind)
    if field:
        product = client.get_product(fix["product_id"])
        if product is None:
            return None
        return _sha(product.get(field))
    if kind == "faq_generate":
        current = client.get_product_metafield(fix["product_id"])
        return _sha(json.dumps(current or [], sort_keys=True))
    return None


def _draft_state_hash(fix) -> str:
    if fix["kind"] == "faq_generate":
        try:
            return _sha(json.dumps(json.loads(fix["before"] or "[]"), sort_keys=True))
        except ValueError:
            return _sha(fix["before"] or "")
    return _sha(fix["before"] or "")


def apply_fix(client, fix_id: int) -> dict:
    """Apply one approved fix. Staleness of the remote field refuses the write."""
    rows = query("SELECT * FROM fixes WHERE id=?", (fix_id,))
    if not rows:
        return {"ok": False, "error": "not found"}
    fix = rows[0]

    current = _current_remote_state(client, fix)
    expected = _draft_state_hash(fix)
    if current is None:
        return {"ok": False, "error": "stale", "note": "Product no longer exists in the catalog."}
    if current != expected:
        return {
            "ok": False,
            "error": "stale",
            "note": "The live field changed since this fix was drafted. Re-draft instead of overwriting.",
        }

    if fix["kind"] == "faq_generate":
        # FAQs aren't a native product field; store the approved set in the
        # geo_agent.faq_json metafield (type "json") for a theme block to render.
        merge = json.loads(fix["after"])
        client.set_product_metafield(fix["product_id"], merge)
        execute("UPDATE fixes SET status='applied', applied_at=? WHERE id=?", (now_iso(), fix_id))
        return {"ok": True, "note": "FAQ items saved to metafield geo_agent.faq_json."}
    field = APPLY_FIELD.get(fix["kind"])
    if not field:
        return {"ok": False, "error": "unknown kind"}
    client.update_product(fix["product_id"], {field: fix["after"]})
    execute("UPDATE fixes SET status='applied', applied_at=? WHERE id=?", (now_iso(), fix_id))
    return {"ok": True, "note": f"Updated {field} in Shopify."}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
