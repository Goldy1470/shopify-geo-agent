"""Security regressions: CSRF, SSRF guard, secrets never in HTML, settings stubbing."""

import json

import pytest
from fastapi.testclient import TestClient

import app.accessibility as accessibility
import app.config as config
import app.db as db
from app.main import app


@pytest.fixture()
def tc():
    with TestClient(app) as c:
        yield c


def setup_config(**over):
    cfg = {
        "shop_domain": "", "mode": "mock", "admin_api_token": "", "openai_api_key": "",
        "brand": "Nimbus Outfitters", "site_url": "", "api_version": "2025-10",
    }
    cfg.update(over)
    config.save_config(cfg)
    return cfg


def test_access_token_never_in_settings_html(tc):
    token = "shpat_secret_token_value_12345"
    setup_config(admin_api_token=token)
    r = tc.get("/settings")
    assert r.status_code == 200
    assert token not in r.text
    assert "shpat_secret" not in r.text
    assert "leave blank to keep" in r.text


def test_openai_key_never_in_settings_html(tc):
    key = "sk-live-abcdef1234567890"
    setup_config(openai_api_key=key)
    r = tc.get("/settings")
    assert r.status_code == 200
    assert key not in r.text


def test_settings_post_does_not_blank_saved_token(tc):
    setup_config(admin_api_token="shpat_keep_me_please", openai_api_key="sk_keep")
    tc.get("/settings")  # prime CSRF cookie
    csrf = tc.cookies.get("geo_csrf")
    r = tc.post(
        "/settings",
        data={
            "csrf_token": csrf,
            "shop_domain": "my-shop.myshopify.com",
            "mode": "live",
            "admin_api_token": "",  # user left blank on purpose
            "openai_api_key": "",
            "brand": "Nimbus Outfitters",
            "site_url": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    stored = config.load_config()
    assert stored["admin_api_token"] == "shpat_keep_me_please"
    assert stored["openai_api_key"] == "sk_keep"
    assert stored["mode"] == "live"


def test_settings_erase_secrets_control(tc):
    setup_config(admin_api_token="shpat_one")
    tc.get("/settings")  # prime CSRF cookie
    tc.post(
        "/settings",
        data={"csrf_token": tc.cookies.get("geo_csrf"), "shop_domain": "", "mode": "mock",
              "admin_api_token": "", "openai_api_key": "", "brand": "", "site_url": "",
              "erase_secrets": "on"},
        follow_redirects=False,
    )
    stored = config.load_config()
    assert stored["admin_api_token"] == ""


def test_post_without_csrf_is_rejected(tc):
    r = tc.post("/visibility/paste", data={"prompt": "x", "engine": "ChatGPT", "response_text": "y"})
    assert r.status_code == 403


def test_post_without_csrf_session_cookie_is_rejected_even_with_a_token(tc):
    r = tc.post("/settings", data={"csrf_token": "whatever", "shop_domain": "", "mode": "mock", "brand": "", "site_url": ""})
    assert r.status_code in (403,)


def test_post_with_valid_csrf_passes(tc):
    tc.get("/settings")
    csrf = tc.cookies.get("geo_csrf")
    r = tc.post(
        "/settings",
        data={"csrf_token": csrf, "shop_domain": "", "mode": "mock", "admin_api_token": "", "openai_api_key": "", "brand": "B", "site_url": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_cross_origin_post_is_blocked_by_origin_check(tc):
    tc.get("/settings")
    csrf = tc.cookies.get("geo_csrf")
    r = tc.post(
        "/settings",
        data={"csrf_token": csrf, "shop_domain": "", "mode": "mock", "brand": "", "site_url": ""},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


class _FakeResp:
    def __init__(self, status_code=200, text="", data=None):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": "text/plain"}

    def json(self):
        return {}


def test_ssrf_guard_blocks_private_urls(monkeypatch, tc):
    calls = []

    def _boom(*a, **k):
        calls.append(a)
        raise AssertionError("network fetch must not run for blocked URL")

    monkeypatch.setattr(accessibility.httpx, "get", _boom)
    tc.get("/accessibility")  # prime CSRF cookie
    r = tc.post(
        "/accessibility/run",
        data={"csrf_token": tc.cookies.get("geo_csrf"), "store_url": "http://169.254.169.254/latest/meta-data", "product_url": ""},
    )
    assert r.status_code == 400
    assert not calls


def test_ssrf_guard_blocks_localhost_and_internal(monkeypatch, tc):
    def _boom(*a, **k):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(accessibility.httpx, "get", _boom)
    tc.get("/accessibility")
    for target in ("http://localhost:8500/x", "http://shop.internal/", "http://myhost.local/", "https://127.0.0.1:8000/", "file:///etc/passwd"):
        r = tc.post("/accessibility/run", data={"csrf_token": tc.cookies.get("geo_csrf"), "store_url": target, "product_url": ""})
        assert r.status_code == 400


def test_accessibility_run_allows_public_url(monkeypatch, tc):
    def fake_get(url, *a, **k):
        if url.endswith("/robots.txt"):
            return _FakeResp(200, "User-agent: *\nDisallow:\n")
        return _FakeResp(404, "")

    monkeypatch.setattr(accessibility.httpx, "get", fake_get)
    tc.get("/accessibility")
    r = tc.post("/accessibility/run", data={"csrf_token": tc.cookies.get("geo_csrf"), "store_url": "example-store.com", "product_url": ""}, follow_redirects=False)
    assert r.status_code == 303
    rows = db.query("SELECT url, result FROM accessibility_checks")
    assert rows and json.loads(rows[0]["result"])["allowed_agents"]
