"""CatalogService: the one shared gateway to product data + writes.

Mode logic:
- live: async GraphQL client -> mirror (content-hash upserts); writes are
  GraphQL mutations with read-back verification.
- mock: persistent DB-backed store seeded from the sample catalog; writes land
  in the same mirror rows and read back like a real store (fixes the old
  per-request mock that silently discarded approved changes).

Every read/write targets the mirror so scoring, suggestion drafting, and
apply-time staleness checks all agree on a single source of truth.

Threading: reads and mock writes are plain sync. Live writes run the async
GraphQL client via asyncio.run() from sync route handlers (FastAPI threadpool
workers have no running loop). The async core is exposed for background jobs.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import load_config
from app.db.models import Product
from app.db.repo import (
    content_hash,
    create_all,
    get_state,
    set_state,
    upsert_product,
)
from app.db.repo import (
    get_product as repo_get_product,
)
from app.db.repo import (
    list_products as repo_list_products,
)
from app.db.session import get_engine

from .graphql import ShopifyGraphQLClient, ShopifyRequestError
from .normalize import node_to_canonical, strip_internal
from .queries import (
    METAFIELDS_SET_MUTATION,
    PRODUCT_UPDATE_MUTATION,
    PRODUCTS_BULK_QUERY,
    PRODUCTS_PAGE_QUERY,
    SINGLE_PRODUCT_QUERY,
)

log = logging.getLogger("shopify.service")

MOCK_SEEDED_KEY = "mock_seeded_v1"
BULK_PRODUCT_THRESHOLD = 1000  # docs: use Bulk Operations for large catalogs
JSON_METAFIELD_CAP = 128_000  # 2026-04+ cap on JSON metafield writes


class CatalogService:
    """Shared, process-wide service; construct via get_catalog()."""

    def __init__(self, mode: str, domain: str = "", token: str = "", api_version: str = "2026-10") -> None:
        self.mode = "live" if (mode == "live" and domain and token) else "mock"
        self.domain = domain
        self.token = token
        self.api_version = api_version
        create_all()

    # ---------------- reads ----------------

    def get_product(self, product_id: str) -> dict | None:
        return repo_get_product(str(product_id))

    def list_products(self) -> list[dict]:
        self._ensure_readiness()
        return repo_list_products()

    def _ensure_readiness(self) -> None:
        create_all()
        if self.mode == "mock":
            self._ensure_mock_seed()

    # ---------------- sync (async core) ----------------

    async def sync_async(self, full: bool = False) -> dict:
        """Sync the live catalog into the mirror; mock mode just confirms its seed."""
        self._ensure_readiness()
        if self.mode != "live":
            return {"mode": "mock", "products": len(repo_list_products())}
        try:
            return await self._sync_live(full)
        except ShopifyRequestError as exc:
            stats = {"mode": "live", "error": str(exc)}
            log.warning("Sync failed: %s", exc)
            return stats

    def sync(self, full: bool = False) -> dict:
        """Sync facade for sync callers (routes without a running loop)."""
        return asyncio.run(self.sync_async(full))

    async def _sync_live(self, full: bool) -> dict:
        async with ShopifyGraphQLClient(self.domain, self.token, self.api_version) as client:
            probe = await client.request("{ shop { name } productCount { count } }")
            name = ((probe.get("data") or {}).get("shop") or {}).get("name", "")
            product_count = int(((probe.get("data") or {}).get("productCount") or {}).get("count") or 0)

        stats: dict = {"mode": "live", "shop": name, "product_count": product_count, "created": 0, "updated": 0, "unchanged": 0}
        if product_count > BULK_PRODUCT_THRESHOLD or (full and product_count > BULK_PRODUCT_THRESHOLD / 3):
            async with ShopifyGraphQLClient(self.domain, self.token, self.api_version) as client:
                lines = await client.bulk_query(PRODUCTS_BULK_QUERY)
            nodes: list[dict] = []
            for line in lines:
                payload = json.loads(line)
                if payload.get("id") and str(payload.get("__typename", "Product")) == "Product":
                    nodes.append(payload)
            self._ingest_nodes(nodes, stats)
        else:
            async with ShopifyGraphQLClient(self.domain, self.token, self.api_version) as client:
                nodes = await client.paginate(PRODUCTS_PAGE_QUERY, {}, "products", limit=100)
            self._ingest_nodes(nodes, stats)
        set_state("last_sync", datetime.now(UTC).isoformat())
        log.info("sync done: %s", stats)
        return stats

    def _ingest_nodes(self, nodes: list[dict], stats: dict) -> None:
        created = updated = unchanged = 0
        with Session(get_engine()) as session:
            for node in nodes:
                canonical = strip_internal(node_to_canonical(node))
                existing = session.scalar(select(Product).where(Product.gid == canonical["gid"]))
                prior_hash = existing.content_hash if existing else None
                incoming_hash = content_hash(canonical)
                row = upsert_product(session, canonical)
                if prior_hash is None:
                    created += 1
                elif prior_hash != incoming_hash and prior_hash != row.content_hash:
                    updated += 1
                else:
                    unchanged += 1
            session.commit()
        stats.update({"created": created, "updated": updated, "unchanged": unchanged})

    # ---------------- mock persistence ----------------

    def _ensure_mock_seed(self) -> None:
        create_all()
        if get_state(MOCK_SEEDED_KEY):
            return
        from app.mock_data import MOCK_CATALOG

        for p in MOCK_CATALOG:
            canonical = mock_to_canonical(p)
            with Session(get_engine()) as session:
                upsert_product(session, canonical)
                session.commit()
        set_state(MOCK_SEEDED_KEY, "1")

    def reseed_mock(self) -> None:
        set_state(MOCK_SEEDED_KEY, "")
        self._ensure_mock_seed()

    # ---------------- writes ----------------

    async def update_product_fields_async(self, product_id: str, fields: dict[str, Any]) -> dict:
        """Write title/body_html/faq with read-back verification.

        Returns {"ok": bool, "errors": [...], "readback": canonical | None}.
        """
        if self.mode != "live":
            return self._mock_write(product_id, fields)
        errors: list[str] = []
        pid = f"gid://shopify/Product/{product_id}"
        async with ShopifyGraphQLClient(self.domain, self.token, self.api_version) as client:
            if fields.get("title") or fields.get("body_html"):
                input_: dict[str, Any] = {"id": pid}
                if fields.get("title"):
                    input_["title"] = fields["title"]
                if fields.get("body_html"):
                    input_["descriptionHtml"] = fields["body_html"]
                result = await client.request(PRODUCT_UPDATE_MUTATION, {"input": input_})
                errors.extend(_collect_user_errors(result, "productUpdate"))
            faq = fields.get("faq")
            if faq is not None:
                payload = json.dumps(faq, ensure_ascii=False)
                if len(payload) > JSON_METAFIELD_CAP:
                    errors.append(f"FAQ payload {len(payload)} bytes exceeds the {JSON_METAFIELD_CAP//1000}KB JSON metafield cap")
                else:
                    result = await client.request(
                        METAFIELDS_SET_MUTATION,
                        {
                            "metafields": [
                                {
                                    "ownerId": pid,
                                    "namespace": "geo_agent",
                                    "key": "faq_json",
                                    "type": "json",
                                    "value": payload,
                                }
                            ]
                        },
                    )
                    errors.extend(_collect_user_errors(result, "metafieldsSet"))
        errors = [e for e in errors if e]
        if errors:
            return {"ok": False, "errors": errors, "readback": None}
        readback = await self._refetch_async(product_id)
        if readback:
            self._write_canonical(readback)
        else:
            errors.append("read-back verification failed: product not returned")
            return {"ok": False, "errors": errors, "readback": None}
        return {"ok": True, "errors": [], "readback": readback}

    async def _refetch_async(self, product_id: str) -> dict | None:
        async with ShopifyGraphQLClient(self.domain, self.token, self.api_version) as client:
            data = await client.request(SINGLE_PRODUCT_QUERY, {"id": f"gid://shopify/Product/{product_id}"})
        node = (data.get("data") or {}).get("product")
        return node_to_canonical(node) if node else None

    def _write_canonical(self, canonical: dict) -> None:
        """Upsert a canonical dict into the mirror (keeps hashes fresh)."""
        clean = {k: v for k, v in canonical.items() if not k.startswith("_")}
        with Session(get_engine()) as session:
            upsert_product(session, clean)
            session.commit()

    def _mock_write(self, product_id: str, fields: dict[str, str | list]) -> dict:
        """Persistent mock store: writes land in the mirror and read back."""
        self._ensure_readiness()
        current = self.get_product(str(product_id))
        if current is None:
            return {"ok": False, "errors": ["not found"], "readback": None}
        merged = dict(current)
        if fields.get("title"):
            merged["title"] = str(fields["title"])
        if fields.get("body_html"):
            merged["body_html"] = str(fields["body_html"])
        if fields.get("faq") is not None:
            merged["faq"] = fields["faq"]
        merged.pop("content_hash", None)
        with Session(get_engine()) as session:
            upsert_product(session, merged)
            session.commit()
        return {"ok": True, "errors": [], "readback": self.get_product(str(product_id))}

    # ---------------- sync facade (fixer compatibility) ----------------

    def update_product(self, product_id: str, changes: dict) -> dict:
        """Sync write used by fixer.apply_fix(): title / body_html / faq."""
        fields = {}
        if changes.get("title"):
            fields["title"] = changes["title"]
        if changes.get("body_html"):
            fields["body_html"] = changes["body_html"]
        if "faq" in changes and changes["faq"] is not None:
            fields["faq"] = changes["faq"]
        return asyncio.run(self.update_product_fields_async(str(product_id), fields))

    def set_product_metafield(self, product_id: str, faq: list) -> dict:
        return asyncio.run(self.update_product_fields_async(str(product_id), {"faq": faq}))

    def get_product_metafield(self, product_id: str, namespace: str = "geo_agent", key: str = "faq_json") -> list | None:
        product = self.get_product(str(product_id))
        return (product or {}).get("faq")


def _collect_user_errors(result: dict, key: str) -> list[str]:
    return [
        f"{ue.get('field')}: {ue.get('message')}"
        for ue in (((result.get("data") or {}).get(key) or {}).get("userErrors") or [])
    ]


def mock_to_canonical(p: dict) -> dict:
    tags = p.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t]
    images = [
        {"src": i.get("src", ""), "alt": i.get("alt", ""), "position": pos, "is_video": str(i.get("src", "")).lower().endswith((".mp4", ".mov", ".webm"))}
        for pos, i in enumerate(p.get("images") or [], start=1)
    ]
    return {
        "id": str(p["id"]),
        "gid": p.get("gid") or f"gid://shopify/Product/{p['id']}",
        "title": p.get("title", ""),
        "handle": p.get("handle", ""),
        "product_type": p.get("product_type", ""),
        "vendor": p.get("vendor", ""),
        "status": p.get("status", "active"),
        "tags": tags,
        "body_html": p.get("body_html", ""),
        "seo_title": p.get("seo_title", "") or "",
        "seo_description": p.get("seo_description", "") or "",
        "price_min": p.get("price_min"),
        "price_max": p.get("price_max"),
        "images": images,
        "variants": p.get("variants") or [],
        "metafields": p.get("metafields") or {},
        "faq": p.get("faq") or [],
        "reviews": p.get("reviews") or {},
        "updated_at": p.get("updated_at", ""),
    }


def get_catalog() -> CatalogService:
    cfg = load_config()
    return CatalogService(
        mode=cfg.get("mode", "mock"),
        domain=cfg.get("shop_domain", ""),
        token=cfg.get("admin_api_token", ""),
        api_version=cfg.get("api_version", "2026-10"),
    )
