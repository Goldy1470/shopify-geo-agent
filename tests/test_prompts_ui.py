"""TRACK pages: prompt set CRUD UI, the run route, prompt detail."""

import pytest
from conftest import csrf_from

from app.db import query
from app.tracking import add_prompt


def _post(client, path, data):
    client.get("/prompts")  # prime CSRF cookie
    payload = {"csrf_token": csrf_from(client), **data}
    return client.post(path, data=payload)


def test_prompts_page_renders_with_forms(client):
    r = client.get("/prompts")
    assert r.status_code == 200
    assert 'action="/prompts/add"' in r.text
    assert 'action="/track/run"' in r.text
    assert 'name="csrf_token"' in r.text


def test_add_prompt_via_form(client):
    _post(client, "/prompts/add", {"text": "best duffel for weekend trips"})
    assert "best duffel for weekend trips" in client.get("/prompts").text
    _post(client, "/prompts/add", {"text": "BEST DUFFEL for weekend trips"})
    assert len(query("SELECT id FROM prompts")) == 1, "casefold dedupe at the route layer"


def test_generate_via_form(client):
    _post(client, "/prompts/generate", {"limit": "6"})
    rows = query("SELECT * FROM prompts")
    assert 1 <= len(rows) <= 6
    assert all(r["source"] == "auto" for r in rows)
    assert "Auto-generate" in client.get("/prompts").text


def test_toggle_and_delete_via_forms(client):
    prow = add_prompt("toggle me")
    _post(client, f"/prompts/{prow['id']}/toggle", {"target": "0"})
    assert query("SELECT active FROM prompts")[0]["active"] == 0
    _post(client, f"/prompts/{prow['id']}/toggle", {"target": "1"})
    assert query("SELECT active FROM prompts")[0]["active"] == 1
    _post(client, f"/prompts/{prow['id']}/delete", {})
    assert query("SELECT id FROM prompts") == []


def test_track_run_route_without_keys_shows_error(client):
    # conftest keeps config.json empty -> no provider keys -> graceful error panel
    r = _post(client, "/track/run", {"n": "5"})
    assert r.status_code == 200
    assert "no_available_providers" in r.text


class StubEng:
    name = "stub"
    engine_label = "API-based estimate"

    def __init__(self, cfg=None):
        pass

    async def ask(self, prompt):
        return {"text": "The Aurora Trail Backpack is our top pick at $184. Buy at nimbusoutfitters.com.",
                "sources": [], "engine_label": "API-based estimate"}


def test_track_run_with_stubbed_provider_end_to_end(client, monkeypatch):
    import app.tracking as tracking

    add_prompt("best travel backpack for rainy commuting")
    monkeypatch.setattr(tracking, "available_providers", lambda cfg: {"stub": StubEng})

    r = _post(client, "/track/run", {"n": "5", "engines": "stub"})
    assert r.status_code == 200
    assert "5</b> runs saved" in r.text.replace("<b>", "")

    rows = query("SELECT * FROM visibility_runs")
    assert len(rows) == 5
    assert all(rr["engine"] == "stub" for rr in rows)
    assert all(rr["prompt_id"] is not None for rr in rows)
    # The hit landed: Aurora mentioned in the stub answer.
    # The hit landed: catalog product ids/titles appear in the stored mention JSON.
    assert any('"111"' in rr["mentioned_products"] and "Aurora Pack" in rr["mentioned_products"] for rr in rows)

    # Prompt detail page lists the runs.
    prow = query("SELECT id, text FROM prompts")[0]
    detail = client.get(f"/prompts/{prow['id']}")
    assert detail.status_code == 200
    assert prow["text"] in detail.text
    assert "stub" in detail.text


def test_prompt_detail_404(client):
    assert client.get("/prompts/999").status_code == 404


def test_csrf_enforced_on_tracking_posts(client):
    client.get("/prompts")
    r = client.post("/prompts/add", data={"text": "no csrf"})
    assert r.status_code == 403


@pytest.mark.parametrize("page", ["/prompts", "/visibility"])
def test_nav_links_track_and_visibility(client, page):
    text = client.get(page).text
    assert 'href="/prompts"' in text
