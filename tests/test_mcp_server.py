"""MCP server tests: tool surface, connect flow, fixes, HTTP guard + mount."""

import json

import pytest
from conftest import csrf_from
from mcp import Client

import app.mcp_server as mcp_server
from app import db, tracking
from app.config import load_config, save_config
from app.shopify.graphql import ShopifyRequestError

EXPECTED_TOOLS = {
    "store_status",
    "shopify_setup_guide",
    "connect_shopify",
    "disconnect_shopify",
    "sync_catalog",
    "list_products",
    "get_product",
    "product_readiness",
    "list_prompts",
    "add_prompt",
    "run_tracking",
    "visibility_summary",
    "generate_fixes",
    "list_fixes",
    "apply_fix",
    "dry_run_fix",
    "apply_fixes",
    "rollback_fix",
    "rollback_batch",
    "apply_audit",
    "discard_fix",
}


class FakeGraphQL:
    """Stand-in for ShopifyGraphQLClient: records calls, returns a canned shop."""

    calls: list[dict] = []
    error: Exception | None = None

    def __init__(self, domain, token, api_version="2026-10", **kwargs):
        self.domain = domain
        self.token = token
        self.api_version = api_version
        FakeGraphQL.calls.append(
            {"domain": domain, "token": token, "api_version": api_version}
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def request(self, query, variables=None):
        if FakeGraphQL.error:
            raise FakeGraphQL.error
        return {
            "data": {
                "shop": {
                    "name": "Aurora Outfitters",
                    "myshopifyDomain": "aurora-outfitters.myshopify.com",
                }
            }
        }


@pytest.fixture()
def fake_shopify(monkeypatch):
    FakeGraphQL.calls = []
    FakeGraphQL.error = None
    monkeypatch.setattr(mcp_server, "ShopifyGraphQLClient", FakeGraphQL)
    return FakeGraphQL


@pytest.fixture()
def seeded():
    db.init_db()
    db.create_all()
    return mcp_server.store_status()


# --- tool surface ------------------------------------------------------------


async def test_tool_surface_and_annotations():
    async with Client(mcp_server.mcp, raise_exceptions=True) as client:
        result = await client.list_tools()
    by_name = {tool.name: tool for tool in result.tools}
    assert set(by_name) >= EXPECTED_TOOLS
    assert by_name["store_status"].annotations.read_only_hint is True
    assert by_name["apply_fix"].annotations.read_only_hint is False
    assert by_name["apply_fix"].annotations.destructive_hint is False
    assert by_name["dry_run_fix"].annotations.read_only_hint is True
    assert by_name["apply_audit"].annotations.read_only_hint is True


async def test_in_memory_client_calls_a_tool(seeded):
    async with Client(mcp_server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool("store_status", {})
    assert result.is_error is False
    assert result.structured_content["mode"] == "mock"
    assert "token_configured" in result.content[0].text


# --- status / guide / connection --------------------------------------------


def test_store_status_mock_mode(seeded):
    status = seeded
    assert status["mode"] == "mock"
    assert status["connected"] is False
    assert status["product_count"] == 5
    assert status["api_version"] == "2026-10"
    assert status["pending_fixes"] == 0


def test_setup_guide_mentions_scopes_and_next_step(seeded):
    guide = mcp_server.shopify_setup_guide()
    assert guide["required_scopes"] == ["read_products", "write_products"]
    assert any("connect_shopify" in step for step in guide["steps"])
    assert guide["next_tool"] == "connect_shopify"


async def test_connect_shopify_saves_config_and_never_echoes_token(seeded, fake_shopify):
    token = "shpat_supersecret_value"
    result = await mcp_server.connect_shopify(
        "https://aurora-outfitters.myshopify.com/admin", token, sync=False
    )
    assert result["ok"] is True
    assert result["shop_domain"] == "aurora-outfitters.myshopify.com"
    assert result["shop"] == "Aurora Outfitters"
    assert token not in json.dumps(result)
    assert fake_shopify.calls[0]["token"] == token

    cfg = load_config()
    assert cfg["admin_api_token"] == token
    assert cfg["shop_domain"] == "aurora-outfitters.myshopify.com"
    assert cfg["mode"] == "live"


async def test_connect_shopify_accepts_bare_handle(seeded, fake_shopify):
    result = await mcp_server.connect_shopify("aurora-outfitters", "shpat_abc123", sync=False)
    assert result["ok"] is True
    assert fake_shopify.calls[0]["domain"] == "aurora-outfitters.myshopify.com"


async def test_connect_shopify_rejects_bad_domain(seeded, fake_shopify):
    result = await mcp_server.connect_shopify("https://example.com", "shpat_abc123", sync=False)
    assert result["ok"] is False
    assert "myshopify.com" in result["error"]
    assert fake_shopify.calls == []


async def test_connect_shopify_rejects_bad_token_without_echo(seeded, fake_shopify):
    fake_shopify.error = ShopifyRequestError("Unexpected HTTP 401: unauthorized")
    token = "shpat_do_not_leak"
    result = await mcp_server.connect_shopify("aurora.myshopify.com", token, sync=False)
    assert result["ok"] is False
    assert "401" in result["error"]
    assert token not in json.dumps(result)
    assert load_config()["admin_api_token"] == ""


async def test_connect_shopify_syncs_catalog(seeded, fake_shopify, monkeypatch):
    captured: dict = {}

    class FakeCatalog:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def sync_async(self):
            return {"mode": "live", "created": 5, "updated": 0, "unchanged": 0}

    monkeypatch.setattr(mcp_server, "CatalogService", FakeCatalog)
    result = await mcp_server.connect_shopify("aurora.myshopify.com", "shpat_abc123")
    assert result["ok"] is True
    assert result["sync"]["created"] == 5
    assert captured["mode"] == "live"
    assert captured["domain"] == "aurora-outfitters.myshopify.com"
    assert captured["token"] == "shpat_abc123"
    assert "shpat_abc123" not in json.dumps(result)


async def test_disconnect_clears_token(seeded):
    save_config({"shop_domain": "aurora.myshopify.com", "admin_api_token": "shpat_x", "mode": "live"})
    result = mcp_server.disconnect_shopify()
    assert result["ok"] is True
    assert result["mode"] == "mock"
    assert "shpat_x" not in json.dumps(result)
    cfg = load_config()
    assert cfg["admin_api_token"] == ""
    assert cfg["shop_domain"] == ""
    assert cfg["mode"] == "mock"


async def test_sync_catalog_mock_mode(seeded):
    result = await mcp_server.sync_catalog()
    assert result["ok"] is True
    assert result["mode"] == "mock"
    assert result["product_count"] == 5


# --- diagnosis ---------------------------------------------------------------


def test_list_products_sorts_worst_first(seeded):
    listing = mcp_server.list_products(limit=3)
    assert listing["count"] == 5
    assert len(listing["products"]) == 3
    scores = [p["score"] for p in listing["products"]]
    assert scores == sorted(scores)
    assert {"id", "title", "score", "grade", "issue_count"} <= set(listing["products"][0])


def test_get_product_includes_scoring_and_readiness(seeded):
    result = mcp_server.get_product("111")
    assert result["ok"] is True
    assert result["product"]["title"] == "Aurora Pack"
    assert result["scoring"]["score"] == 54
    assert result["readiness"]["checks"]


def test_get_product_not_found(seeded):
    assert mcp_server.get_product("does-not-exist") == {
        "ok": False,
        "error": "not_found",
        "product_id": "does-not-exist",
    }


def test_product_readiness_mock_does_not_fetch(seeded):
    result = mcp_server.product_readiness("111")
    assert result["ok"] is True
    assert result["readiness"]["counts"]
    assert result["mode"] == "mock"


# --- prompts / tracking ------------------------------------------------------


def test_add_prompt_and_dedupe(seeded):
    first = mcp_server.add_prompt("best waterproof travel backpack")
    assert first["ok"] is True
    assert first["created"] is True
    assert first["prompt"]["text"] == "best waterproof travel backpack"

    duplicate = mcp_server.add_prompt("  best waterproof TRAVEL backpack ")
    assert duplicate["ok"] is True
    assert duplicate["created"] is False

    empty = mcp_server.add_prompt("   ")
    assert empty["ok"] is False
    assert mcp_server.list_prompts()["count"] == 1


async def test_run_tracking_wires_limits_and_prompt_ids(seeded, monkeypatch):
    tracking.add_prompt("prompt one")
    tracking.add_prompt("prompt two")
    captured: dict = {}

    async def fake_run(products, cfg, engine_names=None, prompts=None, n=5, freshness_hours=24):
        captured.update(
            {
                "engine_names": engine_names,
                "prompts": prompts,
                "n": n,
                "freshness_hours": freshness_hours,
            }
        )
        return {"runs": [], "run_count": 0}

    monkeypatch.setattr(mcp_server.tracking, "run_tracking", fake_run)
    result = await mcp_server.run_tracking(
        engines=["openai"], n=99, prompt_ids=[1], freshness_hours=-5
    )
    assert captured["n"] == 5
    assert captured["engine_names"] == ["openai"]
    assert captured["freshness_hours"] == 0
    assert len(captured["prompts"]) == 1
    assert result["run_count"] == 0
    assert result["engines_available"] == []


async def test_run_tracking_unknown_prompt_ids(seeded):
    result = await mcp_server.run_tracking(prompt_ids=[999])
    assert result["error"] == "no_matching_prompts"


def test_visibility_summary_empty(seeded):
    summary = mcp_server.visibility_summary()
    assert summary["total_runs"] == 0
    assert summary["runs"] == []
    assert summary["mode"] == "mock"


# --- fixes -------------------------------------------------------------------


def test_generate_apply_and_list_fix(seeded):
    generated = mcp_server.generate_fixes()
    assert generated["ok"] is True
    assert generated["created"] >= 1
    assert generated["pending"] == generated["created"]

    pending = mcp_server.list_fixes()
    assert pending["count"] >= 1
    assert pending["counts"]["pending"] == pending["count"]
    impacts = [f["impact"] or 0 for f in pending["fixes"]]
    assert impacts == sorted(impacts, reverse=True), "pending fixes are impact-ordered"
    title_fix = next(
        f for f in pending["fixes"]
        if f["kind"] == "title_rewrite" and f["applicable"] and f["auto"]
    )
    assert title_fix["title"].startswith("Extend title")
    assert title_fix["product_title"]
    assert title_fix["impact"] > 0
    assert title_fix["severity"] in {"low", "medium", "high"}

    before = mcp_server.get_product(title_fix["product_id"])["product"]["title"]
    applied = mcp_server.apply_fix(title_fix["id"])
    assert applied["ok"] is True, applied
    after = mcp_server.get_product(title_fix["product_id"])["product"]["title"]
    assert after != before
    assert before in after  # the product name is never removed

    applied_list = mcp_server.list_fixes(status="applied")
    assert any(f["id"] == title_fix["id"] for f in applied_list["fixes"])


def test_manual_suggestion_is_never_applied(seeded):
    mcp_server.generate_fixes()
    pending = mcp_server.list_fixes()["fixes"]
    manual = next(f for f in pending if not f["auto"] and f["after"] == "")
    assert manual["applicable"] is False
    refused = mcp_server.apply_fix(manual["id"])
    assert refused["ok"] is False
    assert refused["error"] == "unsupported_kind"
    still_pending = mcp_server.list_fixes()["fixes"]
    assert any(f["id"] == manual["id"] for f in still_pending)


def test_discard_fix_and_refuse_apply(seeded):
    mcp_server.generate_fixes()
    fix = mcp_server.list_fixes()["fixes"][0]
    discarded = mcp_server.discard_fix(fix["id"])
    assert discarded["ok"] is True
    refused = mcp_server.apply_fix(fix["id"])
    assert refused["ok"] is False
    assert refused["error"] == "not_pending"
    assert mcp_server.discard_fix(fix["id"])["ok"] is False


def test_apply_fix_not_found(seeded):
    result = mcp_server.apply_fix(99999)
    assert result == {"ok": False, "error": "not_found", "fix_id": 99999}


def test_list_fixes_rejects_unknown_status(seeded):
    assert mcp_server.list_fixes(status="bogus")["ok"] is False


def test_dry_run_apply_rollback_and_audit_tools(seeded):
    mcp_server.generate_fixes()
    pending = mcp_server.list_fixes()["fixes"]
    fix = next(f for f in pending if f["applicable"] and f["kind"] == "title_rewrite")

    plan = mcp_server.dry_run_fix(fix["id"])
    assert plan["ok"] is True
    assert plan["would_snapshot"] is True

    applied = mcp_server.apply_fix(fix["id"])
    assert applied["ok"] is True, applied
    assert applied["audit_id"]
    assert applied["rollback_available"] is True
    assert mcp_server.list_fixes(status="applied")["count"] >= 1

    rolled = mcp_server.rollback_fix(fix["id"])
    assert rolled["ok"] is True, rolled
    assert mcp_server.list_fixes(status="rolled_back")["count"] == 1

    audit = mcp_server.apply_audit(limit=10)
    assert audit["count"] >= 2
    assert audit["rows"][0]["action"] == "rollback"
    assert any(row["action"] == "apply" and row["status"] == "ok" for row in audit["rows"])


def test_bulk_apply_and_batch_rollback_tools(seeded):
    mcp_server.generate_fixes()
    fixes = [f for f in mcp_server.list_fixes()["fixes"] if f["applicable"]][:2]
    assert len(fixes) == 2

    applied = mcp_server.apply_fixes([f["id"] for f in fixes])
    assert applied["ok"] is True, applied
    assert applied["applied"] == 2
    assert applied["batch_id"]

    rolled = mcp_server.rollback_batch(applied["batch_id"])
    assert rolled["ok"] is True, rolled
    assert rolled["rolled_back"] == 2
    audit = mcp_server.apply_audit(limit=20, batch_id=applied["batch_id"])
    assert audit["count"] >= 4
    assert {row["action"] for row in audit["rows"]} == {"apply", "rollback"}


# --- HTTP transport ----------------------------------------------------------


def _initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }


def _mcp_headers(token: str | None = None) -> dict:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def test_mcp_token_never_rendered_in_settings_html(seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    save_config({"mcp_token": "super-secret-mcp-token"})
    with TestClient(app) as tc:
        response = tc.get("/settings")
    assert response.status_code == 200
    assert "super-secret-mcp-token" not in response.text
    assert "leave blank to keep" in response.text


def test_settings_keeps_blank_mcp_token_and_erase_clears_it(seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    save_config({"mcp_token": "keep-me"})
    with TestClient(app) as tc:
        tc.get("/settings")
        csrf = csrf_from(tc)
        form = {
            "csrf_token": csrf,
            "shop_domain": "",
            "mode": "mock",
            "admin_api_token": "",
            "openai_api_key": "",
            "mcp_token": "",
            "brand": "",
            "site_url": "",
        }
        tc.post("/settings", data=form)
        assert load_config()["mcp_token"] == "keep-me"
        tc.post("/settings", data={**form, "erase_secrets": "on"})
        assert load_config()["mcp_token"] == ""


def test_mcp_http_mount_accepts_loopback(seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1:8000") as tc:
        response = tc.post("/mcp", json=_initialize_payload(), headers=_mcp_headers())
    assert response.status_code == 200
    assert "serverInfo" in response.text


def test_mcp_http_guard_rejects_non_local_without_token(seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://mcp.example.com") as tc:
        response = tc.post("/mcp", json=_initialize_payload(), headers=_mcp_headers())
    assert response.status_code == 403
    assert "bearer" in response.json()["error"].lower()


def test_mcp_http_guard_accepts_bearer_token(seeded, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("MCP_TOKEN", raising=False)
    save_config({"mcp_token": "secret-mcp-token"})
    with TestClient(app, base_url="http://mcp.example.com") as tc:
        bad = tc.post("/mcp", json=_initialize_payload(), headers=_mcp_headers("wrong"))
        good = tc.post(
            "/mcp", json=_initialize_payload(), headers=_mcp_headers("secret-mcp-token")
        )
    assert bad.status_code == 403
    assert good.status_code == 200
    assert "serverInfo" in good.text


def test_mcp_http_guard_blocks_cross_origin_browser_request(seeded):
    from fastapi.testclient import TestClient

    from app.main import app

    headers = _mcp_headers()
    headers["Origin"] = "https://evil.example.com"
    with TestClient(app, base_url="http://127.0.0.1:8000") as tc:
        response = tc.post("/mcp", json=_initialize_payload(), headers=headers)
    assert response.status_code == 403
