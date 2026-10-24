"""Async Shopify Admin GraphQL client.

Verified against current docs (2026-10):
- Cost-based throttling: every successful call returns extensions.cost with
  requestedQueryCost / actualQueryCost and throttleStatus (maximumAvailable,
  currentlyAvailable, restoreRate). We wait before overspending instead of
  relying on 429s, and honor THROTTLED errors with backoff.
- 429 responses carry Retry-After; 5xx retry with exponential backoff + jitter.
- Cursor pagination for products pages; Bulk Operations for >1,000 products
  (bulk queries don't draw on the live point bucket, max 5 concurrent).
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from .queries import BULK_RUN_MUTATION, BULK_STATUS_QUERY

log = logging.getLogger("shopify.client")

SleepFn = Callable[[float], Awaitable[None]]
DEFAULT_SLEEP: SleepFn = asyncio.sleep


class ShopifyRequestError(RuntimeError):
    pass


class ThrottleBudget:
    """Tracks the point bucket between calls (docs: restore rate 100/s standard)."""

    def __init__(self) -> None:
        self.maximum_available = 2000.0
        self.currently_available = 2000.0
        self.restore_rate = 100.0

    def update(self, cost_ext: dict | None) -> None:
        if not cost_ext:
            return
        ts = cost_ext.get("throttleStatus") or {}
        self.maximum_available = float(ts.get("maximumAvailable", self.maximum_available))
        self.currently_available = float(ts.get("currentlyAvailable", self.currently_available))
        self.restore_rate = float(ts.get("restoreRate", self.restore_rate))

    def wait_seconds(self, requested: float) -> float:
        if self.currently_available >= requested:
            return 0.0
        need = requested - self.currently_available
        return max(0.0, need / max(self.restore_rate, 1e-9))

    def consume(self, actual: float) -> None:
        self.currently_available -= actual


class ShopifyGraphQLClient:
    MAX_ATTEMPTS = 5
    MAX_BACKOFF = 15.0
    MAX_THROTTLE_WAIT = 5.0

    def __init__(
        self,
        domain: str,
        token: str,
        api_version: str = "2026-10",
        *,
        timeout: float = 30.0,
        sleep: SleepFn | None = None,
    ) -> None:
        self.domain = domain.replace("https://", "").strip("/")
        self.token = token
        self.api_version = api_version
        self.url = f"https://{self.domain}/admin/api/{api_version}/graphql.json"
        self.timeout = timeout
        self._sleep = sleep or DEFAULT_SLEEP
        self._budget = ThrottleBudget()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ShopifyGraphQLClient":
        if self._client is None:
            self._client = self._build_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "X-Shopify-Access-Token": self.token,
                "Content-Type": "application/json",
                "User-Agent": "shopify-geo-agent/0.2",
            },
            timeout=httpx.Timeout(self.timeout),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def request(self, query: str, variables: dict | None = None) -> dict:
        """Execute one GraphQL call with throttling + retries; returns the JSON body."""
        payload: dict[str, Any] = {"query": query, "variables": variables or {}}
        last_error: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            wait = self._budget.wait_seconds(10.0)
            if wait:
                await self._sleep(min(wait, self.MAX_THROTTLE_WAIT))
            try:
                resp = await self._ensure_client().post(self.url, json=payload)
            except httpx.TransportError as exc:
                last_error = exc
                await self._sleep(self._backoff(attempt))
                continue
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else self._backoff(attempt)
                await self._sleep(delay)
                continue
            if resp.status_code >= 500:
                last_error = ShopifyRequestError(f"HTTP {resp.status_code}")
                await self._sleep(self._backoff(attempt, jitter=True))
                continue
            if resp.status_code != 200:
                raise ShopifyRequestError(f"Unexpected HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise ShopifyRequestError("Non-JSON GraphQL response") from exc
            cost_ext = (data.get("extensions") or {}).get("cost") or {}
            self._budget.update(cost_ext)
            self._budget.consume(float(cost_ext.get("actualQueryCost") or 10.0))
            codes = {
                (e.get("extensions") or {}).get("code")
                for e in (data.get("errors") or [])
                if isinstance(e, dict)
            }
            if "THROTTLED" in codes:
                await self._sleep(self._backoff(attempt))
                continue
            return data
        raise ShopifyRequestError(f"GraphQL retries exhausted: {last_error or 'throttled'}")

    def _backoff(self, attempt: int, jitter: bool = False) -> float:
        base = min(2.0 * 2 ** (attempt - 1), self.MAX_BACKOFF)
        return base * (1 + random.random() * 0.25) if jitter else base

    async def paginate(
        self,
        query: str,
        variables: dict | None,
        node_key: str,
        limit: int = 100,
        max_pages: int = 20,
    ) -> list[dict]:
        """Cursor pagination over a connection's pageInfo/nodes."""
        cursor: str | None = None
        items: list[dict] = []
        for _ in range(max_pages):
            data = await self.request(query, {**(variables or {}), "cursor": cursor, "limit": limit})
            conn = (data.get("data") or {}).get(node_key)
            if conn is None:
                raise ShopifyRequestError(f"Missing connection '{node_key}' in response")
            items.extend(conn.get("nodes") or [])
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage") or not page.get("endCursor"):
                break
            cursor = page["endCursor"]
        return items

    async def bulk_query(self, query: str) -> list[str]:
        """Run a bulk operation; returns JSONL lines when the artifact is ready."""
        run = await self.request(BULK_RUN_MUTATION, {"query": query})
        result = ((run.get("data") or {}).get("bulkOperationRunQuery")) or {}
        errs = result.get("userErrors") or []
        if errs:
            raise ShopifyRequestError(f"bulkOperationRunQuery failed: {errs[0].get('message')}")
        for _ in range(120):
            await self._sleep(2.0)
            status = await self.request(BULK_STATUS_QUERY)
            op = ((status.get("data") or {}).get("currentBulkOperation")) or {}
            state = op.get("status")
            if state == "COMPLETED" and op.get("url"):
                return await self._download_jsonl(op["url"])
            if state in ("CANCELED", "EXPIRED", "FAILED") or op.get("errorCode"):
                raise ShopifyRequestError(f"Bulk operation {state}: {op.get('errorCode')}")
        raise ShopifyRequestError("Bulk operation timed out")

    async def _download_jsonl(self, url: str) -> list[str]:
        host = urlparse(url).hostname or ""
        if ".myshopify.com" not in host:
            raise ShopifyRequestError(f"Bulk URL host unexpected: {host}")
        client = self._ensure_client()
        resp = await client.get(url)
        resp.raise_for_status()
        return [ln for ln in (line.strip() for line in resp.text.split("\n")) if ln]
