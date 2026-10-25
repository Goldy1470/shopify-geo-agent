import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as database
from . import tracking
from .accessibility import run_full_check
from .config import load_config, save_config
from .core.security import (
    CSRF_COOKIE,
    CSRF_FORM_FIELD,
    CSRF_HEADER,
    csrf_ok,
    new_csrf_token,
    normalize_public_http_url,
    origin_matches_host,
)
from .db import execute
from .fixer import APPLICABLE_KINDS, FIX_TITLES, apply_fix
from .mcp_server import http_endpoint as mcp_http_endpoint
from .readiness import product_readiness
from .scoring import WEIGHTS, score_product
from .scoring_config import load_scoring_config
from .shopify import get_catalog
from .shopify_client import ShopifyClient
from .suggest import generate_suggestions
from .visibility import run_prompt_via_openai, save_run, visibility_summary

templates = Jinja2Templates(directory="app/templates")


def _allowed_count(result_json: str) -> int:
    try:
        return len(json.loads(result_json).get("allowed_agents", []))
    except ValueError:
        return 0


def _blocked_list(result_json: str) -> str:
    try:
        return ", ".join(json.loads(result_json).get("blocked_agents", [])) or "none"
    except ValueError:
        return "?"


def _llms(result_json: str) -> bool:
    try:
        return bool(json.loads(result_json).get("llms_txt"))
    except ValueError:
        return False


def _from_json(v):
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return []


templates.env.filters["allowed_count"] = _allowed_count
templates.env.filters["blocked_list"] = _blocked_list
templates.env.filters["llms"] = _llms
templates.env.filters["from_json"] = _from_json


async def csrf_guard(request: Request):
    """Dependency for every state-changing route: Origin/Referer check + double-submit token.

    Fetch-based callers send the cookie value as the X-CSRF-Token header; forms send the
    hidden csrf_token field. 403s surface as JSON so the JS approve buttons can show them.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not origin_matches_host(
        request.headers.get("host", ""),
        request.headers.get("origin"),
        request.headers.get("referer"),
    ):
        raise HTTPException(status_code=403, detail="Cross-origin request blocked")
    cookie_token = request.cookies.get(CSRF_COOKIE)
    submitted = request.headers.get(CSRF_HEADER)
    if not submitted:
        # Only parse the body when the header is absent; Starlette caches the parsed form so
        # the Form(...) validators downstream still see it.
        try:
            form = await request.form()
        except ValueError:
            form = None
        if form is not None:
            value = form.get(CSRF_FORM_FIELD)
            if isinstance(value, str):
                submitted = value
    if not csrf_ok(cookie_token, submitted):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize both databases, then run the MCP HTTP transport for the app's lifetime."""
    database.init_db()
    database.create_all()
    async with mcp_http_endpoint.lifespan():
        yield


# CSRF is enforced on all state-changing routes (double-submit cookie + Origin check).
app = FastAPI(
    title="Shopify GEO Agent",
    version="0.2.0",
    dependencies=[Depends(csrf_guard)],
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.middleware("http")
async def csrf_cookie_middleware(request: Request, call_next):
    """Provide a CSRF token (cookie + request.state) for every response."""
    token = request.cookies.get(CSRF_COOKIE)
    if not token:
        token = new_csrf_token()
        request.state.new_csrf = token
    request.state.csrf = token
    response = await call_next(request)
    if getattr(request.state, "new_csrf", None):
        response.set_cookie(
            CSRF_COOKIE, token, samesite="lax", httponly=False, max_age=60 * 60 * 24 * 7
        )
    return response


def ctx(request: Request, **extra):
    cfg = load_config()
    catalog = get_catalog()
    base = {
        "request": request,
        "cfg": cfg,
        "client_live": catalog.mode == "live",
        "store_domain": cfg["shop_domain"],
        "csrf": getattr(request.state, "csrf", ""),
    }
    base.update(extra)
    return base


def _render(request: Request, name: str, extra: dict):
    return templates.TemplateResponse(request, name, extra)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    products = get_catalog().list_products()
    scored = [score_product(p) for p in products]
    avg = round(sum(s["score"] for s in scored) / len(scored)) if scored else 0
    grades = {"A": 0, "B": 0, "C": 0, "D": 0}
    for s in scored:
        grades[s["grade"]] += 1
    pending = database.query("SELECT COUNT(*) c FROM fixes WHERE status='pending'")[0]["c"]
    issues: dict[str, int] = {}
    for s in scored:
        for i in s["issues"]:
            issues[i["text"]] = issues.get(i["text"], 0) + 1
    top_issues = sorted(issues.items(), key=lambda kv: -kv[1])[:8]
    vis = visibility_summary(products)
    return _render(
        request,
        "dashboard.html",
        ctx(
            request,
            scored=scored,
            avg=avg,
            grades=grades,
            pending=pending,
            top_issues=top_issues,
            vis=vis,
            product_count=len(products),
        ),
    )


@app.post("/sync")
async def sync_catalog():
    """Pull the (live or mock) catalog into the local mirror."""
    await get_catalog().sync_async()
    return RedirectResponse("/", status_code=303)


@app.get("/products", response_class=HTMLResponse)
def products(request: Request):
    products = get_catalog().list_products()
    scored = sorted([score_product(p) for p in products], key=lambda s: s["score"])
    return _render(request, "products.html", ctx(request, scored=scored))


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(request: Request, product_id: str):
    p = get_catalog().get_product(product_id)
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    scoring = score_product(p)
    ready = product_readiness(p)
    review_floor = load_scoring_config()["thresholds"]["reviews"]["full_count"]
    pending = database.query(
        "SELECT * FROM fixes WHERE product_id=? AND status='pending'", (product_id,)
    )
    return _render(
        request,
        "product_detail.html",
        ctx(request, p=p, s=scoring, ready=ready, review_floor=review_floor, weights=WEIGHTS, pending=pending, fix_titles=FIX_TITLES),
    )


@app.get("/products/{product_id}/readiness.json")
def product_readiness_json(product_id: str):
    """Catalog readiness as JSON; JSON-LD is fetched only in live mode."""
    p = get_catalog().get_product(product_id)
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    catalog = get_catalog()
    live = catalog.mode == "live"
    return JSONResponse(
        product_readiness(p, check_jsonld=live, store_domain=load_config()["shop_domain"])
    )


@app.get("/accessibility", response_class=HTMLResponse)
def accessibility_page(request: Request):
    history = database.query("SELECT * FROM accessibility_checks ORDER BY created_at DESC LIMIT 10")
    return _render(request, "accessibility.html", ctx(request, history=history))


@app.post("/accessibility/run")
async def accessibility_run(
    request: Request, store_url: str = Form(...), product_url: str = Form("")
):
    # SSRF guard: this endpoint fetches arbitrary URLs server-side. Scheme-less
    # form input ("your-store.com") is normalized to https before validation.
    normalized_store = normalize_public_http_url(store_url)
    normalized_product = normalize_public_http_url(product_url) if product_url else ""
    if not normalized_store:
        return JSONResponse({"error": "Refusing to fetch a non-public URL"}, status_code=400)
    if product_url and not normalized_product:
        return JSONResponse({"error": "Refusing to fetch a non-public URL"}, status_code=400)
    result = run_full_check(normalized_store, normalized_product or None)
    execute(
        "INSERT INTO accessibility_checks (url, created_at, result) VALUES (?,?,?)",
        (store_url, datetime.now(UTC).isoformat(), json.dumps(result)),
    )
    return RedirectResponse("/accessibility", status_code=303)


@app.get("/accessibility/result/{check_id}", response_class=JSONResponse)
def accessibility_result(request: Request, check_id: int):
    row = database.query("SELECT result FROM accessibility_checks WHERE id=?", (check_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Check not found")
    try:
        return JSONResponse(json.loads(row[0]["result"]))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Corrupt check result") from exc


@app.get("/visibility", response_class=HTMLResponse)
def visibility_page(request: Request):
    products = get_catalog().list_products()
    return _render(request, "visibility.html", ctx(request, **visibility_summary(products)))


@app.post("/visibility/paste")
async def visibility_paste(
    request: Request,
    prompt: str = Form(...),
    engine: str = Form(...),
    response_text: str = Form(...),
):
    save_run(prompt, engine, "paste", response_text, get_catalog().list_products())
    return RedirectResponse("/visibility", status_code=303)


@app.post("/visibility/api-run")
async def visibility_api_run(request: Request, prompt: str = Form(...)):
    cfg = load_config()
    if not cfg.get("openai_api_key"):
        return JSONResponse(
            {"error": "No OpenAI API key configured. Use paste mode or add the key in Settings."},
            status_code=400,
        )
    run = run_prompt_via_openai(prompt, cfg["openai_api_key"], get_catalog().list_products())
    if run.get("error"):
        return JSONResponse(run, status_code=400)
    return RedirectResponse("/visibility", status_code=303)


# --- Phase 3 TRACK: prompt sets + provider runner ------------------------------


@app.get("/prompts", response_class=HTMLResponse)
def prompts_page(request: Request):
    cfg = load_config()
    return _render(
        request,
        "prompts.html",
        ctx(
            request,
            prompts=tracking.list_prompts(),
            available=sorted(tracking.available_engines(cfg)),
            result=None,
        ),
    )


@app.post("/prompts/add")
async def prompts_add(request: Request, text: str = Form(...)):
    tracking.add_prompt(text)
    return RedirectResponse("/prompts", status_code=303)


@app.post("/prompts/{prompt_id}/toggle")
async def prompts_toggle(request: Request, prompt_id: int, target: str = Form(...)):
    tracking.set_prompt_active(prompt_id, target == "1")
    return RedirectResponse("/prompts", status_code=303)


@app.post("/prompts/{prompt_id}/delete")
async def prompts_delete(request: Request, prompt_id: int):
    tracking.delete_prompt(prompt_id)
    return RedirectResponse("/prompts", status_code=303)


@app.post("/prompts/generate")
async def prompts_generate(request: Request, limit: int = Form(10)):
    tracking.generate_prompts_from_catalog(get_catalog().list_products(), limit=min(max(limit, 1), 25))
    return RedirectResponse("/prompts", status_code=303)


@app.post("/track/run")
async def track_run(request: Request, engines: list[str] = Form([]), n: int = Form(5)):
    """Fire the tracked shopping prompts against the available providers."""
    cfg = load_config()
    result = await tracking.run_tracking(
        get_catalog().list_products(), cfg,
        engine_names=engines or None,
        n=min(max(n, 1), 5),
    )
    return _render(
        request,
        "prompts.html",
        ctx(
            request,
            prompts=tracking.list_prompts(),
            available=sorted(tracking.available_engines(cfg)),
            result=result,
        ),
    )


@app.get("/prompts/{prompt_id}", response_class=HTMLResponse)
def prompt_detail(request: Request, prompt_id: int):
    prow = tracking.get_prompt(prompt_id)
    if not prow:
        raise HTTPException(status_code=404, detail="Prompt not found")
    runs = database.query(
        "SELECT * FROM visibility_runs WHERE prompt_id=? ORDER BY created_at DESC LIMIT 50", (prompt_id,)
    )
    return _render(request, "prompt_detail.html", ctx(request, prompt=prow, runs=runs))


@app.get("/fixes", response_class=HTMLResponse)
def fixes_page(request: Request):
    pending_rows = database.query(
        "SELECT * FROM fixes WHERE status='pending' "
        "ORDER BY COALESCE(impact, 0) DESC, created_at DESC, id DESC"
    )
    pending = []
    for row in pending_rows:
        fix = dict(row)
        if fix.get("judge"):
            try:
                fix["judge_obj"] = json.loads(fix["judge"])
            except (TypeError, ValueError):
                fix["judge_obj"] = {"verdict": "unknown", "reason": str(fix["judge"])}
        pending.append(fix)
    applied = [
        dict(row) for row in database.query(
            "SELECT * FROM fixes WHERE status='applied' ORDER BY applied_at DESC LIMIT 20"
        )
    ]
    catalog = get_catalog()
    by_product: dict[str, str] = {}
    for f in pending + applied:
        product_id = f.get("product_id") or ""
        if product_id in by_product:
            continue
        p = catalog.get_product(product_id)
        by_product[product_id] = (p or {}).get("title") or product_id
    return _render(
        request,
        "fixes.html",
        ctx(
            request,
            pending=pending,
            applied=applied,
            fix_titles=FIX_TITLES,
            applicable_kinds=APPLICABLE_KINDS,
            by_product=by_product,
        ),
    )


@app.post("/fixes/generate")
def fixes_generate():
    generate_suggestions(get_catalog().list_products())
    return RedirectResponse("/fixes", status_code=303)


@app.post("/fixes/{fix_id}/approve")
def fix_approve(fix_id: int):
    # Sync handler: FastAPI runs it in the threadpool (no running event loop),
    # which lets CatalogService bridge to its async GraphQL client when live.
    result = apply_fix(get_catalog(), fix_id)
    return JSONResponse(result)


@app.post("/fixes/{fix_id}/discard")
def fix_discard(fix_id: int):
    database.execute(
        "UPDATE fixes SET status='discarded' WHERE id=? AND status='pending'", (fix_id,)
    )
    return RedirectResponse("/fixes", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    client = ShopifyClient()  # kept for the connection probe only
    return _render(request, "settings.html", ctx(request, connection=client.check_connection()))


@app.post("/settings")
async def settings_save(
    request: Request,
    shop_domain: str = Form(""),
    mode: str = Form("mock"),
    admin_api_token: str = Form(""),
    openai_api_key: str = Form(""),
    mcp_token: str = Form(""),
    brand: str = Form(""),
    site_url: str = Form(""),
    suggest_llm_refine: str = Form(""),
    erase_secrets: str = Form(""),
):
    cfg = load_config()
    # Blank field = keep the stored secret (never blank it by accident); the
    # "erase saved keys" checkbox is the explicit way to remove one.
    keep = erase_secrets != "on"
    if not admin_api_token and cfg.get("admin_api_token") and keep:
        admin_api_token = cfg["admin_api_token"]
    if not openai_api_key and cfg.get("openai_api_key") and keep:
        openai_api_key = cfg["openai_api_key"]
    if not mcp_token and cfg.get("mcp_token") and keep:
        mcp_token = cfg["mcp_token"]
    if erase_secrets == "on":
        admin_api_token = ""
        openai_api_key = ""
        mcp_token = ""
    updates = {
        "shop_domain": shop_domain,
        "mode": mode,
        "admin_api_token": admin_api_token,
        "openai_api_key": openai_api_key,
        "mcp_token": mcp_token,
        "brand": brand,
        "site_url": site_url,
        "suggest_llm_refine": suggest_llm_refine == "on",
    }
    save_config(updates)
    return RedirectResponse("/settings", status_code=303)


# Mounted last: Mount("/") matches every remaining path, and the MCP app owns /mcp.
# The guard inside http_endpoint allows loopback clients and requires a bearer token remotely.
app.mount("/", mcp_http_endpoint)
