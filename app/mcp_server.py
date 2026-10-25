"""Built-in MCP server: the same GEO workflow, driven from Claude/Cursor/etc.

Two transports share one implementation:

- stdio: ``python -m app.mcp_server`` (no network, easiest for desktop clients)
- Streamable HTTP: mounted by ``app.main`` at ``/mcp``

Design rules:
- Tools are plain module-level functions, registered on ``mcp``. Tests call the
  functions directly and also go through an in-memory MCP client.
- Reads are safe. Drafting fixes is safe (queue only). ``apply_fix`` is the one
  write that reaches Shopify, and calling it is the merchant's approval.
- Tokens are accepted by ``connect_shopify`` but never returned, logged, or
  included in error messages.
- The HTTP transport is localhost-only by default; non-loopback requests need
  ``MCP_TOKEN`` (env) or ``mcp_token`` in config.json.
"""

import hmac
import ipaddress
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import db as database
from . import tracking
from .config import load_config, save_config
from .db import execute, query
from .db.repo import get_state
from .fixer import APPLICABLE_KINDS, FIX_TITLES
from .fixer import apply_fix as apply_queued_fix
from .readiness import product_readiness as run_product_readiness
from .scoring import score_product
from .shopify import get_catalog
from .shopify.graphql import ShopifyGraphQLClient, ShopifyRequestError
from .shopify.service import CatalogService
from .suggest import generate_suggestions
from .visibility import visibility_summary as summarize_visibility

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

mcp = MCPServer(
    "shopify-geo-agent",
    title="Shopify GEO Agent",
    version="0.2.0",
    instructions=(
        "Shopify GEO Agent audits and improves a Shopify catalog for AI shopping visibility "
        "(GEO: why AI assistants do or don't recommend a store's products).\n\n"
        "Start with store_status. Mock mode works with zero setup and a sample catalog. To "
        "connect a real store, call shopify_setup_guide, then connect_shopify with the store's "
        "*.myshopify.com domain and an Admin API access token. Never repeat a token back to the "
        "merchant after connecting.\n\n"
        "Diagnosis is read-only (list_products, get_product, product_readiness). Visibility "
        "tracking uses official provider APIs. generate_fixes only QUEUES drafts; nothing goes "
        "live. Call apply_fix only after the merchant explicitly approves that exact fix id. "
        "Never invent product facts: if data is missing, report the gap instead."
    ),
)

_SHOP_HANDLE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TOKEN_MAX = 500


# --- helpers -----------------------------------------------------------------


def _pending_fix_count() -> int:
    try:
        return int(query("SELECT COUNT(*) AS c FROM fixes WHERE status='pending'")[0]["c"])
    except Exception:  # noqa: BLE001 - status must work even before the DB is created
        return 0


def _normalize_shop_domain(raw: str) -> str:
    """Accept ``store``, ``store.myshopify.com`` or a pasted admin URL; return the API domain."""
    domain = (raw or "").strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/")[0].split("?")[0].split("#")[0].rstrip(".")
    if not domain or "@" in domain or ":" in domain:
        raise ValueError("Enter the store's *.myshopify.com domain (not a URL with a port or login).")
    if domain.endswith(".myshopify.com"):
        handle = domain[: -len(".myshopify.com")]
        if _SHOP_HANDLE_RE.match(handle):
            return domain
        raise ValueError("That store handle is not valid. Use the store's *.myshopify.com domain.")
    if "." not in domain and _SHOP_HANDLE_RE.match(domain):
        return f"{domain}.myshopify.com"
    raise ValueError(
        "Use the store's *.myshopify.com domain (Settings -> Domains in Shopify admin). "
        "Custom storefront domains can't be used for Admin API calls."
    )


def _clean_token(raw: str) -> str:
    token = (raw or "").strip()
    if not token:
        raise ValueError("Paste the Admin API access token from the custom app's API credentials.")
    if len(token) > _TOKEN_MAX or any(ch.isspace() for ch in token):
        raise ValueError("That does not look like an Admin API access token. Copy it again.")
    return token


def _shopify_error_message(exc: Exception) -> str:
    text = str(exc)
    if "401" in text or "403" in text or "unauthorized" in text.lower():
        return (
            "Shopify rejected the token (HTTP 401/403). Check that it is the Admin API access "
            "token from this store's custom app and that read_products + write_products are granted."
        )
    return f"Shopify request failed: {text[:300]}"


def _configured_http_token() -> str:
    """Remote MCP access token: env wins, then config.json. Never logged."""
    return (os.environ.get("MCP_TOKEN") or load_config().get("mcp_token") or "").strip()


# --- connection tools --------------------------------------------------------


@mcp.tool(title="Store status", annotations=READ)
def store_status() -> dict[str, Any]:
    """Show connection mode, catalog size, prompts, pending fixes and last sync. Read-only."""
    cfg = load_config()
    catalog = get_catalog()
    products = catalog.list_products()
    return {
        "mode": catalog.mode,
        "configured_mode": cfg.get("mode", "mock"),
        "connected": bool(cfg.get("shop_domain") and cfg.get("admin_api_token")),
        "shop_domain": cfg.get("shop_domain", ""),
        "token_configured": bool(cfg.get("admin_api_token")),
        "api_version": cfg.get("api_version", ""),
        "brand": cfg.get("brand", ""),
        "product_count": len(products),
        "last_sync": get_state("last_sync"),
        "prompt_count": len(tracking.list_prompts()),
        "active_prompt_count": len(tracking.list_prompts(only_active=True)),
        "pending_fixes": _pending_fix_count(),
        "available_engines": tracking.available_engines(cfg),
        "http_access_requires_token": bool(_configured_http_token()),
        "hint": (
            "Mock mode: sample catalog, no setup needed. Run shopify_setup_guide to connect a real store."
            if catalog.mode == "mock"
            else "Live store connected. Run sync_catalog if the product count looks stale."
        ),
    }


@mcp.tool(title="Shopify setup guide", annotations=READ)
def shopify_setup_guide() -> dict[str, Any]:
    """Step-by-step custom-app setup for a real Shopify store. Read-only, no token needed."""
    cfg = load_config()
    return {
        "api_version": cfg.get("api_version", ""),
        "required_scopes": ["read_products", "write_products"],
        "why_shopify_requires_this": (
            "Shopify only lets an app write to a store after the merchant grants an Admin API "
            "token. No MCP server can bypass that authorization step."
        ),
        "steps": [
            "In Shopify admin, open Settings -> Apps and sales channels -> Develop apps.",
            "Click Create an app and name it (for example, GEO Agent).",
            "Open Configuration -> Admin API integration and enable read_products and write_products.",
            "Click Save, then Install app (Shopify shows the scopes being granted).",
            "Open API credentials and reveal the Admin API access token (starts with shpat_).",
            "Copy the token and the store's *.myshopify.com domain (Settings -> Domains).",
            'Call connect_shopify(shop_domain="your-store.myshopify.com", admin_api_token="shpat_...").',
        ],
        "where_the_token_goes": (
            "It is written to config.json on this machine and used only for Shopify Admin API "
            "calls. It is never returned by a tool, written to logs, or placed in HTML."
        ),
        "revoking_access": (
            "disconnect_shopify removes the local copy. To revoke the grant itself, delete the "
            "custom app in Shopify admin (Settings -> Apps and sales channels -> Develop apps)."
        ),
        "next_tool": "connect_shopify",
    }


@mcp.tool(title="Connect Shopify store", annotations=WRITE)
async def connect_shopify(shop_domain: str, admin_api_token: str, sync: bool = True) -> dict[str, Any]:
    """Validate a Shopify domain + Admin API token, save them locally, and sync the catalog.

    Call only after the merchant provides their own token. The token is never echoed back.
    """
    cfg = load_config()
    try:
        domain = _normalize_shop_domain(shop_domain)
        token = _clean_token(admin_api_token)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        async with ShopifyGraphQLClient(domain, token, cfg.get("api_version", "2026-10")) as client:
            payload = await client.request("{ shop { name myshopifyDomain } }")
    except ShopifyRequestError as exc:
        return {"ok": False, "error": _shopify_error_message(exc)}
    except Exception as exc:  # noqa: BLE001 - tool boundary: never leak a traceback/token
        return {"ok": False, "error": f"Could not reach Shopify ({type(exc).__name__}). Check the domain and network."}

    errors = payload.get("errors") or []
    if errors:
        message = errors[0].get("message") if isinstance(errors[0], dict) else "unknown error"
        return {"ok": False, "error": f"Shopify rejected the query: {str(message)[:200]}"}
    shop = ((payload.get("data") or {}).get("shop")) or {}
    if not shop:
        return {"ok": False, "error": "Shopify returned no shop for this token. Check the token and store domain."}
    canonical = str(shop.get("myshopifyDomain") or domain).lower()

    try:
        save_config({"shop_domain": canonical, "admin_api_token": token, "mode": "live"})
    except OSError:
        return {"ok": False, "error": "Validated with Shopify, but could not write config.json. Check file permissions."}

    result: dict[str, Any] = {
        "ok": True,
        "shop": shop.get("name") or "",
        "shop_domain": canonical,
        "mode": "live",
        "message": "Connected. The token is stored locally and will not be shown again.",
        "next": "Run sync_catalog or list_products to inspect the catalog.",
    }
    if sync:
        service = CatalogService(
            mode="live",
            domain=canonical,
            token=token,
            api_version=cfg.get("api_version", "2026-10"),
        )
        stats = await service.sync_async()
        result["sync"] = stats
        if stats.get("error"):
            result["warnings"] = [
                "Connected, but the first catalog sync failed. The token may lack read_products. "
                "Fix the scopes, then call sync_catalog."
            ]
    return result


@mcp.tool(title="Disconnect Shopify store", annotations=WRITE)
def disconnect_shopify() -> dict[str, Any]:
    """Remove the locally stored token and return to mock mode. Does not revoke the app in Shopify."""
    save_config({"shop_domain": "", "admin_api_token": "", "mode": "mock"})
    return {
        "ok": True,
        "mode": "mock",
        "message": "Local Shopify token erased; the app is back in mock mode.",
        "revoke_in_shopify": (
            "To revoke the authorization itself, delete the custom app in Shopify admin "
            "(Settings -> Apps and sales channels -> Develop apps)."
        ),
    }


@mcp.tool(title="Sync catalog", annotations=WRITE)
async def sync_catalog() -> dict[str, Any]:
    """Pull the live Shopify catalog into the local mirror (mock mode just confirms its seed)."""
    catalog = get_catalog()
    stats = await catalog.sync_async()
    if stats.get("error"):
        return {"ok": False, **stats}
    return {"ok": True, **stats, "product_count": len(catalog.list_products())}


# --- diagnosis tools ---------------------------------------------------------


@mcp.tool(title="List products", annotations=READ)
def list_products(limit: int = 50) -> dict[str, Any]:
    """List catalog products with GEO score, grade and issue count, worst first. Read-only."""
    catalog = get_catalog()
    products = catalog.list_products()
    rows = []
    for product in products:
        scoring = score_product(product)
        rows.append(
            {
                "id": product["id"],
                "title": product.get("title", ""),
                "handle": product.get("handle", ""),
                "product_type": product.get("product_type", ""),
                "vendor": product.get("vendor", ""),
                "status": product.get("status", ""),
                "price_min": product.get("price_min"),
                "price_max": product.get("price_max"),
                "score": scoring["score"],
                "grade": scoring["grade"],
                "issue_count": len(scoring["issues"]),
            }
        )
    rows.sort(key=lambda row: row["score"])
    limit = min(max(int(limit), 1), 200)
    return {"mode": catalog.mode, "count": len(rows), "products": rows[:limit]}


@mcp.tool(title="Get product", annotations=READ)
def get_product(product_id: str) -> dict[str, Any]:
    """Full product record + GEO scoring + readiness checks for one product id/handle. Read-only."""
    catalog = get_catalog()
    product = catalog.get_product(str(product_id))
    if not product:
        return {"ok": False, "error": "not_found", "product_id": str(product_id)}
    return {
        "ok": True,
        "mode": catalog.mode,
        "product": product,
        "scoring": score_product(product),
        "readiness": run_product_readiness(
            product,
            check_jsonld=catalog.mode == "live",
            store_domain=load_config().get("shop_domain", ""),
        ),
    }


@mcp.tool(title="Product readiness", annotations=READ)
def product_readiness(product_id: str) -> dict[str, Any]:
    """Data-quality readiness checks (taxonomy, GTIN, SEO, variants, price, JSON-LD). Read-only."""
    catalog = get_catalog()
    product = catalog.get_product(str(product_id))
    if not product:
        return {"ok": False, "error": "not_found", "product_id": str(product_id)}
    return {
        "ok": True,
        "mode": catalog.mode,
        "product_id": product["id"],
        "title": product.get("title", ""),
        "readiness": run_product_readiness(
            product,
            check_jsonld=catalog.mode == "live",
            store_domain=load_config().get("shop_domain", ""),
        ),
    }


# --- tracking tools ----------------------------------------------------------


@mcp.tool(title="List prompts", annotations=READ)
def list_prompts(only_active: bool = False) -> dict[str, Any]:
    """List the shopping prompts used for AI visibility tracking. Read-only."""
    prompts = tracking.list_prompts(only_active=only_active)
    return {"count": len(prompts), "prompts": prompts}


@mcp.tool(title="Add prompt", annotations=WRITE)
def add_prompt(text: str) -> dict[str, Any]:
    """Add one shopping prompt (duplicates are ignored). Writes only to the local prompts table."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {"ok": False, "error": "Prompt text is empty."}
    created = tracking.add_prompt(cleaned)
    return {
        "ok": True,
        "created": bool(created),
        "prompt": created,
        "note": "" if created else "An equivalent prompt already exists.",
    }


@mcp.tool(title="Run AI visibility tracking", annotations=WRITE)
async def run_tracking(
    engines: list[str] | None = None,
    n: int = 5,
    prompt_ids: list[int] | None = None,
    freshness_hours: float = 24,
) -> dict[str, Any]:
    """Run active prompts against configured AI provider APIs (resumable, daily-capped).

    Answers come only from official provider APIs; configure provider keys in Settings first.
    """
    cfg = load_config()
    products = get_catalog().list_products()
    prompts = None
    if prompt_ids:
        wanted = {int(pid) for pid in prompt_ids}
        prompts = [p for p in tracking.list_prompts(only_active=True) if int(p["id"]) in wanted]
        if not prompts:
            return {"error": "no_matching_prompts", "runs": []}
    result = await tracking.run_tracking(
        products,
        cfg,
        engine_names=[e for e in engines if e] if engines else None,
        prompts=prompts,
        n=min(max(int(n), 1), 5),
        freshness_hours=max(0.0, float(freshness_hours)),
    )
    return {"engines_available": tracking.available_engines(cfg), **result}


@mcp.tool(title="Visibility summary", annotations=READ)
def visibility_summary(include_responses: bool = False, limit: int = 10) -> dict[str, Any]:
    """Mention rate, Wilson CIs, per-prompt stats, sources, competitors and daily trends. Read-only."""
    catalog = get_catalog()
    summary = summarize_visibility(catalog.list_products())
    summary["mode"] = catalog.mode
    limit = min(max(int(limit), 1), 30)
    summary["runs"] = summary["runs"][:limit]
    if not include_responses:
        for run in summary["runs"]:
            run.pop("response_text", None)
    return summary


# --- fix tools ---------------------------------------------------------------


@mcp.tool(title="Generate fix drafts", annotations=WRITE)
def generate_fixes() -> dict[str, Any]:
    """Prioritized title/FAQ/description drafts plus manual tasks. Does NOT write to Shopify."""
    catalog = get_catalog()
    inserted = generate_suggestions(catalog.list_products())
    return {
        "ok": True,
        "created": len(inserted),
        "pending": _pending_fix_count(),
        "note": "Suggestions are queued only, ordered by impact. Review them, then call apply_fix "
                "for ids the merchant approves. Manual tasks cannot be auto-applied in this phase.",
    }


@mcp.tool(title="List fixes", annotations=READ)
def list_fixes(status: str = "pending", limit: int = 50, full: bool = False) -> dict[str, Any]:
    """List queued/applied/discarded fixes with impact, before/after previews. Read-only."""
    if status not in {"pending", "applied", "discarded"}:
        return {"ok": False, "error": "status must be pending, applied or discarded"}
    order = (
        "COALESCE(impact, 0) DESC, created_at DESC, id DESC"
        if status == "pending"
        else "created_at DESC, id DESC"
    )
    rows = query(f"SELECT * FROM fixes WHERE status=? ORDER BY {order}", (status,))
    catalog = get_catalog()
    titles: dict[str, str] = {}
    limit = min(max(int(limit), 1), 200)
    fixes = []
    for row in rows[:limit]:
        fix = dict(row)
        product_id = str(fix.get("product_id") or "")
        if product_id not in titles:
            product = catalog.get_product(product_id)
            titles[product_id] = (product or {}).get("title") or product_id
        fix["product_title"] = titles[product_id]
        fix["title"] = FIX_TITLES.get(fix.get("kind") or "", fix.get("kind") or "fix")
        fix["applicable"] = bool(
            fix.get("auto") and fix.get("kind") in APPLICABLE_KINDS and (fix.get("after") or "").strip()
        )
        if not full:
            for field in ("before", "after"):
                value = fix.get(field) or ""
                if len(value) > 600:
                    fix[field] = value[:600] + "... (truncated; pass full=true to see all)"
        fixes.append(fix)
    counts = {
        row["status"]: int(row["c"])
        for row in query("SELECT status, COUNT(*) AS c FROM fixes GROUP BY status")
    }
    return {"status": status, "count": len(rows), "counts": counts, "fixes": fixes}


@mcp.tool(title="Apply approved fix", annotations=WRITE)
def apply_fix(fix_id: int) -> dict[str, Any]:
    """Write one approved fix to Shopify (or the mock store). Refuses stale drafts.

    Only call after the merchant explicitly approves this exact fix id.
    """
    rows = query("SELECT * FROM fixes WHERE id=?", (fix_id,))
    if not rows:
        return {"ok": False, "error": "not_found", "fix_id": fix_id}
    fix = dict(rows[0])
    if fix.get("status") != "pending":
        return {"ok": False, "error": "not_pending", "status": fix.get("status"), "fix_id": fix_id}
    if fix.get("kind") not in APPLICABLE_KINDS:
        return {
            "ok": False,
            "error": "unsupported_kind",
            "fix_id": fix_id,
            "kind": fix.get("kind"),
            "note": "Manual task in this phase: copy the suggestion into Shopify admin.",
        }
    result = apply_queued_fix(get_catalog(), fix_id)
    payload = {
        "ok": bool(result.get("ok")),
        "fix_id": fix_id,
        "kind": fix.get("kind"),
        "product_id": fix.get("product_id"),
        "result": result,
    }
    if not payload["ok"]:
        payload["error"] = result.get("error")
        payload["note"] = result.get("note")
    return payload


@mcp.tool(title="Discard fix", annotations=WRITE)
def discard_fix(fix_id: int) -> dict[str, Any]:
    """Discard a queued fix without touching the store."""
    cursor = execute("UPDATE fixes SET status='discarded' WHERE id=? AND status='pending'", (fix_id,))
    if cursor.rowcount == 0:
        return {"ok": False, "error": "not_found_or_not_pending", "fix_id": fix_id}
    return {"ok": True, "fix_id": fix_id, "status": "discarded"}


# --- HTTP transport ----------------------------------------------------------


def _is_loopback_name(host: str | None) -> bool:
    if not host:
        return False
    candidate = host.strip().strip("[]").lower()
    if candidate == "localhost" or candidate.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _is_loopback_client(host: str | None) -> bool:
    """ASGI client address check. Unknown/non-IP means an in-process or Unix-socket caller."""
    if not host:
        return True
    candidate = host.strip().strip("[]")
    if candidate in {"testclient", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return True


def _host_name(host_header: str) -> str | None:
    try:
        return urlsplit("//" + host_header).hostname
    except ValueError:
        return None


class MCPAccessGuard:
    """ASGI guard for the HTTP transport.

    Allowed: loopback clients addressing a loopback Host (Claude Desktop, Cursor,
    local curl). Everything else must present ``Authorization: Bearer <MCP_TOKEN>``.
    The token is compared in constant time and never logged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        origin = headers.get("origin", "")
        client_host = (scope.get("client") or (None, None))[0]

        local = _is_loopback_client(client_host) and _is_loopback_name(_host_name(host))
        if local and origin and not _is_loopback_name(_host_name(origin)):
            local = False

        if local:
            await self.app(scope, receive, send)
            return

        token = _configured_http_token()
        auth = headers.get("authorization", "")
        if token and auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {
                "error": "MCP HTTP access requires a bearer token for non-local requests.",
                "hint": "Set MCP_TOKEN (or mcp_token in config.json) and send "
                "Authorization: Bearer <token>. Local clients can use 127.0.0.1.",
            },
            status_code=403,
        )
        await response(scope, receive, send)


class MCPHttpEndpoint:
    """Mountable ASGI endpoint that builds a fresh MCP HTTP app per host lifespan.

    The SDK session manager can only be started once per instance. Rebuilding the
    transport app inside each lifespan keeps the FastAPI app restartable (tests,
    reloads) without leaking a dead session manager.
    """

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self._app: MCPAccessGuard | None = None

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        from mcp.server.transport_security import TransportSecuritySettings

        http_app = self.server.streamable_http_app(
            streamable_http_path="/mcp",
            # The guard above enforces loopback-or-bearer; the SDK's Host allowlist
            # cannot express "any host, but only with a token".
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        self._app = MCPAccessGuard(http_app)
        try:
            async with self.server.session_manager.run():
                yield
        finally:
            self._app = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        if self._app is None:
            response = JSONResponse(
                {"error": "MCP HTTP endpoint is not running. Start the web app with uvicorn/run.py."},
                status_code=503,
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


http_endpoint = MCPHttpEndpoint(mcp)


def main() -> None:
    """stdio entry point: python -m app.mcp_server."""
    database.init_db()
    database.create_all()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
