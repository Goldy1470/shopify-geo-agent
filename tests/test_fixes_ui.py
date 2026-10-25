"""Phase 4 fix-queue UI: prioritized cards, conditional approve, web apply."""

from conftest import csrf_from

from app.db import query


def _generate(client):
    client.get("/fixes")  # prime the CSRF cookie
    return client.post(
        "/fixes/generate",
        data={"csrf_token": csrf_from(client)},
        follow_redirects=True,
    )


def test_generate_renders_prioritized_cards(client):
    response = _generate(client)
    assert response.status_code == 200
    text = response.text
    assert "impact" in text
    assert "auto-draft" in text
    assert "manual task" in text
    assert "Extend title" in text
    assert "Suggested Product JSON-LD Liquid snippet" in text
    assert "Approve &amp; apply" in text
    assert "Generate suggestions for all products" in text


def test_manual_task_has_no_approve_button(client):
    _generate(client)
    manual = query(
        "SELECT id FROM fixes WHERE status='pending' AND auto=0 ORDER BY id LIMIT 1"
    )[0]
    text = client.get("/fixes").text
    assert f"applyFix({manual['id']}" not in text


def test_web_apply_refuses_manual_task(client):
    _generate(client)
    manual = query(
        "SELECT id FROM fixes WHERE status='pending' AND auto=0 ORDER BY id LIMIT 1"
    )[0]
    response = client.post(
        f"/fixes/{manual['id']}/approve",
        headers={"X-CSRF-Token": csrf_from(client)},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"] == "unsupported_kind"
    status = query("SELECT status FROM fixes WHERE id=?", (manual["id"],))[0]["status"]
    assert status == "pending"


def test_web_apply_title_fix(client):
    _generate(client)
    fix = query(
        "SELECT * FROM fixes WHERE status='pending' AND kind='title_rewrite' AND auto=1 "
        "ORDER BY impact DESC LIMIT 1"
    )[0]
    response = client.post(
        f"/fixes/{fix['id']}/approve",
        headers={"X-CSRF-Token": csrf_from(client)},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    status = query("SELECT status FROM fixes WHERE id=?", (fix["id"],))[0]["status"]
    assert status == "applied"


def test_discard_keeps_manual_task_out_of_queue(client):
    _generate(client)
    manual = query(
        "SELECT id FROM fixes WHERE status='pending' AND auto=0 ORDER BY id LIMIT 1"
    )[0]
    client.get("/fixes")
    response = client.post(
        f"/fixes/{manual['id']}/discard",
        data={"csrf_token": csrf_from(client)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    status = query("SELECT status FROM fixes WHERE id=?", (manual["id"],))[0]["status"]
    assert status == "discarded"
    assert f"applyFix({manual['id']}" not in response.text
