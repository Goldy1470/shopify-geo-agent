"""Phase 5 APPLY UI: dry-run, edit, bulk apply/discard, rollback, audit."""

from urllib.parse import parse_qs, urlparse

from conftest import csrf_from

from app.db import query
from app.shopify import get_catalog


def _generate(client):
    client.get("/fixes")
    return client.post(
        "/fixes/generate",
        data={"csrf_token": csrf_from(client)},
        follow_redirects=True,
    )


def _applicable_fixes(limit=2):
    return query(
        "SELECT id, product_id, kind FROM fixes WHERE status='pending' AND auto=1 "
        "AND kind IN ('title_rewrite','description_append','faq_generate') "
        "ORDER BY id LIMIT ?",
        (limit,),
    )


def _field_value(catalog, product_id, kind):
    product = catalog.get_product(product_id) or {}
    if kind == "title_rewrite":
        return product.get("title")
    if kind == "description_append":
        return product.get("body_html")
    return product.get("faq")


def _post_form(client, path, data):
    client.get("/fixes")
    payload = {"csrf_token": csrf_from(client), **data}
    return client.post(path, data=payload, follow_redirects=True)


def test_dry_run_endpoint_reports_ready_without_writing(client):
    _generate(client)
    fix = _applicable_fixes(1)[0]
    response = client.get(f"/fixes/{fix['id']}/dry-run")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["would_snapshot"] is True
    assert query("SELECT status FROM fixes WHERE id=?", (fix["id"],))[0]["status"] == "pending"


def test_edit_before_approve_rejects_invalid_and_saves_valid(client):
    _generate(client)
    fix = next(row for row in _applicable_fixes(10) if row["kind"] == "title_rewrite")
    before = query("SELECT before FROM fixes WHERE id=?", (fix["id"],))[0]["before"]

    rejected = _post_form(client, f"/fixes/{fix['id']}/edit", {"after": "Completely Different Product"})
    assert "drops the product" in rejected.text
    assert query("SELECT after FROM fixes WHERE id=?", (fix["id"],))[0]["after"] != (
        "Completely Different Product"
    )

    edited = f"{before} - Canvas Tote"
    saved = _post_form(client, f"/fixes/{fix['id']}/edit", {"after": edited})
    assert "Draft saved." in saved.text
    assert query("SELECT after FROM fixes WHERE id=?", (fix["id"],))[0]["after"] == edited


def test_bulk_apply_then_rollback_batch(client):
    _generate(client)
    fixes = _applicable_fixes(2)
    assert len(fixes) == 2
    catalog = get_catalog()
    originals = {
        row["id"]: _field_value(catalog, row["product_id"], row["kind"]) for row in fixes
    }

    applied = _post_form(
        client,
        "/fixes/bulk",
        {"action": "apply", "fix_ids": [row["id"] for row in fixes]},
    )
    assert "Applied 2" in applied.text
    batch_id = parse_qs(urlparse(str(applied.url)).query)["batch"][0]
    for row in fixes:
        assert _field_value(catalog, row["product_id"], row["kind"]) != originals[row["id"]]
        assert query("SELECT status FROM fixes WHERE id=?", (row["id"],))[0]["status"] == "applied"

    rolled = _post_form(client, f"/fixes/batch/{batch_id}/rollback", {})
    assert "Rolled back 2" in rolled.text
    for row in fixes:
        assert _field_value(catalog, row["product_id"], row["kind"]) == originals[row["id"]]
        assert query("SELECT status FROM fixes WHERE id=?", (row["id"],))[0]["status"] == "rolled_back"


def test_bulk_discard_selected(client):
    _generate(client)
    fixes = _applicable_fixes(2)
    response = _post_form(
        client,
        "/fixes/bulk",
        {"action": "discard", "fix_ids": [row["id"] for row in fixes]},
    )
    assert "Discarded 2" in response.text
    for row in fixes:
        assert query("SELECT status FROM fixes WHERE id=?", (row["id"],))[0]["status"] == "discarded"


def test_single_rollback_route_and_history_button(client):
    _generate(client)
    fix = _applicable_fixes(1)[0]
    catalog = get_catalog()
    original = _field_value(catalog, fix["product_id"], fix["kind"])

    client.get("/fixes")
    approved = client.post(
        f"/fixes/{fix['id']}/approve", headers={"X-CSRF-Token": csrf_from(client)}
    )
    assert approved.json()["ok"] is True
    page = client.get("/fixes")
    assert f'action="/fixes/{fix["id"]}/rollback"' in page.text

    rolled = _post_form(client, f"/fixes/{fix['id']}/rollback", {})
    assert "Rolled back 1" in rolled.text
    assert _field_value(catalog, fix["product_id"], fix["kind"]) == original
    assert query("SELECT status FROM fixes WHERE id=?", (fix["id"],))[0]["status"] == "rolled_back"


def test_audit_page_and_queue_section(client):
    _generate(client)
    fix = _applicable_fixes(1)[0]
    client.get("/fixes")
    client.post(f"/fixes/{fix['id']}/approve", headers={"X-CSRF-Token": csrf_from(client)})

    page = client.get("/fixes")
    assert "Audit log" in page.text
    assert "Recently applied" in page.text

    audit = client.get("/fixes/audit")
    assert audit.status_code == 200
    assert "Apply audit" in audit.text
    assert "apply" in audit.text
    assert "ok" in audit.text
