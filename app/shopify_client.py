"""Thin wrapper around Shopify Admin REST / GraphQL, with a mock fallback."""

import json

import httpx

from .config import load_config
from .mock_data import MOCK_CATALOG


class ShopifyClient:
    def __init__(self):
        cfg = load_config()
        self.mode = cfg["mode"]
        self.shop_domain = cfg["shop_domain"].replace("https://", "").strip("/")
        self.token = cfg["admin_api_token"]
        self.api_version = cfg["api_version"]
        self._mock = [dict(p) for p in MOCK_CATALOG]

    @property
    def is_live(self) -> bool:
        return self.mode == "live" and bool(self.shop_domain) and bool(self.token)

    def _headers(self):
        return {"X-Shopify-Access-Token": self.token, "Content-Type": "application/json"}

    def get_products(self, limit: int = 50) -> list[dict]:
        if not self.is_live:
            return self._mock
        products = []
        url = f"https://{self.shop_domain}/admin/api/{self.api_version}/products.json?limit={limit}"
        while url:
            resp = httpx.get(url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            page = resp.json()
            products.extend(page.get("products", []))
            url = resp.links.get("next", {}).get("url")
        return products

    def get_product(self, product_id: str) -> dict | None:
        if not self.is_live:
            return next((p for p in self._mock if str(p["id"]) == str(product_id)), None)
        resp = httpx.get(
            f"https://{self.shop_domain}/admin/api/{self.api_version}/products/{product_id}.json",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("product")

    def get_product_metafield(self, product_id: str, namespace: str = "geo_agent", key: str = "faq_json") -> list | None:
        if not self.is_live:
            p = self.get_product(product_id)
            return p.get("faq") if p else None
        resp = httpx.get(
            f"https://{self.shop_domain}/admin/api/{self.api_version}/metafields.json",
            params={"metafield[owner_id]": product_id, "metafield[namespace]": namespace},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        hits = [m for m in resp.json().get("metafields", []) if m.get("key") == key]
        if not hits:
            return None
        try:
            return json.loads(hits[0].get("value", "[]"))
        except ValueError:
            return None

    def set_product_metafield(self, product_id: str, items: list, namespace: str = "geo_agent", key: str = "faq_json") -> None:
        value = json.dumps(items)
        if not self.is_live:
            for p in self._mock:
                if str(p["id"]) == str(product_id):
                    p["faq"] = items
                    return
            raise ValueError("mock product not found")
        existing = httpx.get(
            f"https://{self.shop_domain}/admin/api/{self.api_version}/metafields.json",
            params={"metafield[owner_id]": product_id, "metafield[namespace]": namespace},
            headers=self._headers(),
            timeout=30,
        ).json().get("metafields", [])
        existing_hits = [m for m in existing if m.get("key") == key]
        if existing_hits:
            method, url = httpx.put, f"https://{self.shop_domain}/admin/api/{self.api_version}/metafields/{existing_hits[0]['id']}.json"
            payload = {"metafield": {"id": existing_hits[0]["id"], "value": value, "type": "json_string"}}
        else:
            method, url = httpx.post, f"https://{self.shop_domain}/admin/api/{self.api_version}/products/{product_id}/metafields.json"
            payload = {"metafield": {"namespace": namespace, "key": key, "value": value, "type": "json_string"}}
        resp = method(url, headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()

    def update_product(self, product_id: str, changes: dict) -> dict:
        """changes: {"title": ..., "body_html": ..., "description_parser"?} via REST product update."""
        if not self.is_live:
            for p in self._mock:
                if str(p["id"]) == str(product_id):
                    p.update(changes)
                    return p
            raise ValueError("mock product not found")
        payload = {"product": {"id": int(product_id), **changes}}
        resp = httpx.put(
            f"https://{self.shop_domain}/admin/api/{self.api_version}/products/{product_id}.json",
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("product", {})

    def check_connection(self) -> dict:
        if not self.is_live:
            return {"ok": False, "mode": "mock", "message": "Running in mock mode. Add domain + token in Settings."}
        try:
            resp = httpx.get(
                f"https://{self.shop_domain}/admin/api/{self.api_version}/shop.json",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            name = resp.json().get("shop", {}).get("name", self.shop_domain)
            return {"ok": True, "mode": "live", "message": f"Connected to {name}"}
        except httpx.HTTPError as exc:
            return {"ok": False, "mode": "live", "message": f"Connection failed: {exc}"}
