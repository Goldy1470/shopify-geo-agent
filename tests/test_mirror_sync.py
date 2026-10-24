"""CatalogService: live sync into the mirror (stubbed transport) + persistent mock store."""

import json
from pathlib import Path

import pytest

import app.shopify.service as service_mod
from app.db.repo import get_product, list_products
from app.shopify.service import MOCK_SEEDED_KEY, CatalogService, get_state

FIXTURES = Path(__file__).parent / "fixtures" / "graphql"


def _nodes_from_fixtures(*names: str) -> list[dict]:
    nodes = []
    for name in names:
        body = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        nodes.extend(body["data"]["products"]["nodes"])
    return nodes


class StubGraphQL:
    """Async-context-stub standing in for ShopifyGraphQLClient inside the service."""

    saved = {}

    def __init__(self, domain, token, api_version="2026-10", **kw):
        StubGraphQL.saved["last"] = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def request(self, query, variables=None):
        if "productCount" in query:
            return {
                "data": {"shop": {"name": "Timberline Supply"}, "productCount": {"count": 3}},
            }
        raise AssertionError(f"unexpected query: {query[:80]}")

    async def paginate(self, query, variables, node_key, limit=100, max_pages=20):
        return _nodes_from_fixtures("products_page1.json", "products_page2.json")

    async def bulk_query(self, query):
        lines = [json.dumps({"__typename": "Product", **n}) for n in _nodes_from_fixtures("products_page1.json")]
        return lines


@pytest.fixture()
def live_service(monkeypatch):
    monkeypatch.setattr(service_mod, "ShopifyGraphQLClient", StubGraphQL)
    return CatalogService("live", domain="timberline.example.com", token="shpat_x")


async def test_live_sync_ingests_mirror(live_service):
    stats = await live_service.sync_async()
    assert stats["created"] == 3
    products = list_products()
    assert {p["id"] for p in products} == {
        "8012345678901", "8012345678902", "8012345678903",
    }
    kestrel = get_product("8012345678901")
    assert kestrel["title"] == "Kestrel Alpine Shell Jacket"
    assert kestrel["reviews"]["count"] == 914 and kestrel["reviews"]["source"] == "yotpo"
    assert kestrel["faq"] == []
    assert get_state("last_sync")


async def test_sync_is_idempotent_by_content_hash(live_service):
    await live_service.sync_async()
    stats = await live_service.sync_async()
    assert stats["created"] == 0 and stats["updated"] == 0
    assert stats["unchanged"] == 3


async def test_sync_detects_remote_changes(live_service):
    await live_service.sync_async()
    before = list_products()
    # third page brings a re-titled Kestrel node on the next sync
    original = StubGraphQL.paginate

    async def paginate_with_change(self, query, variables, node_key, limit=100, max_pages=20):
        nodes = _nodes_from_fixtures("products_page1.json", "products_page2.json")
        for n in nodes:
            if n["id"].endswith("78901"):
                n["title"] = "Kestrel Alpine Shell Jacket 2.0"
        return nodes

    StubGraphQL.paginate = paginate_with_change
    try:
        stats = await live_service.sync_async()
    finally:
        StubGraphQL.paginate = original
    assert stats["updated"] == 1
    after = {p["id"]: p for p in list_products()}
    assert after["8012345678901"]["title"] == "Kestrel Alpine Shell Jacket 2.0"
    assert any(p["title"] == "Kestrel Alpine Shell Jacket" for p in before)


def test_mock_seed_and_persistence():
    svc = CatalogService("mock")
    products = svc.list_products()
    assert [p["id"] for p in products] == ["111", "222", "333", "444", "555"], "stable example ids"
    assert get_state(MOCK_SEEDED_KEY) == "1"
    resynced = CatalogService("mock")
    assert resynced.list_products() == products, "seed must not duplicate on re-init"


def test_mock_write_reads_back_and_persists():
    svc = CatalogService("mock")
    result = svc.update_product("111", {"title": "Aurora Pack - Travel Backpack"})
    assert result["ok"] is True
    fresh = CatalogService("mock")  # a brand-new instance sees the write
    assert fresh.get_product("111")["title"] == "Aurora Pack - Travel Backpack"
    assert fresh.get_product("222")["title"] == "Drift"


def test_mock_faq_metafield_write():
    svc = CatalogService("mock")
    faq = [{"q": "Is it waterproof?", "a": "Yes - laminated shell."}]
    assert svc.set_product_metafield("111", faq)["ok"] is True
    fresh = CatalogService("mock")
    assert fresh.get_product("111")["faq"] == faq
