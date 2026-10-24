import json
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as database
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
from .fixer import FIX_TITLES, apply_fix, generate_fixes
from .scoring import score_product
from .shopify_client import ShopifyClient
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


# CSRF is enforced on all state-changing routes (double-submit cookie + Origin check).
app = FastAPI(
    title="Shopify GEO Agent",
    version="0.2.0",
    dependencies=[Depends(csrf_guard)],
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


@app.on_event("startup")
def startup():
    database.init_db()


def ctx(request: Request, **extra):
    cfg = load_config()
    client = ShopifyClient()
    base = {
        "request": request,
        "cfg": cfg,
        "client_live": client.is_live,
        "store_domain": cfg["shop_domain"],
        "csrf": getattr(request.state, "csrf", ""),
    }
    base.update(extra)
    return base


def _render(request: Request, name: str, extra: dict):
    return templates.TemplateResponse(request, name, extra)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    client = ShopifyClient()
    products = client.get_products()
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


@app.get("/products", response_class=HTMLResponse)
def products(request: Request):
    client = ShopifyClient()
    products = client.get_products()
    scored = sorted([score_product(p) for p in products], key=lambda s: s["score"])
    return _render(request, "products.html", ctx(request, scored=scored))


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(request: Request, product_id: str):
    client = ShopifyClient()
    p = client.get_product(product_id)
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    scoring = score_product(p)
    pending = database.query(
        "SELECT * FROM fixes WHERE product_id=? AND status='pending'", (product_id,)
    )
    return _render(
        request,
        "product_detail.html",
        ctx(request, p=p, s=scoring, pending=pending, fix_titles=FIX_TITLES),
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
    client = ShopifyClient()
    products = client.get_products()
    return _render(request, "visibility.html", ctx(request, **visibility_summary(products)))


@app.post("/visibility/paste")
async def visibility_paste(
    request: Request,
    prompt: str = Form(...),
    engine: str = Form(...),
    response_text: str = Form(...),
):
    client = ShopifyClient()
    save_run(prompt, engine, "paste", response_text, client.get_products())
    return RedirectResponse("/visibility", status_code=303)


@app.post("/visibility/api-run")
async def visibility_api_run(request: Request, prompt: str = Form(...)):
    cfg = load_config()
    if not cfg.get("openai_api_key"):
        return JSONResponse(
            {"error": "No OpenAI API key configured. Use paste mode or add the key in Settings."},
            status_code=400,
        )
    client = ShopifyClient()
    run = run_prompt_via_openai(prompt, cfg["openai_api_key"], client.get_products())
    if run.get("error"):
        return JSONResponse(run, status_code=400)
    return RedirectResponse("/visibility", status_code=303)


@app.get("/fixes", response_class=HTMLResponse)
def fixes_page(request: Request):
    pending = database.query("SELECT * FROM fixes WHERE status='pending' ORDER BY created_at DESC")
    applied = database.query(
        "SELECT * FROM fixes WHERE status='applied' ORDER BY applied_at DESC LIMIT 20"
    )
    by_product: dict[str, str] = {}
    client = ShopifyClient()
    for f in pending:
        p = client.get_product(f["product_id"])
        by_product.setdefault(
            f["product_id"], p.get("title", f["product_id"]) if p else f["product_id"]
        )
    return _render(
        request,
        "fixes.html",
        ctx(
            request,
            pending=pending,
            applied=applied,
            fix_titles=FIX_TITLES,
            by_product=by_product,
        ),
    )


@app.post("/fixes/generate")
def fixes_generate():
    client = ShopifyClient()
    generate_fixes(client.get_products())
    return RedirectResponse("/fixes", status_code=303)


@app.post("/fixes/{fix_id}/approve")
def fix_approve(fix_id: int):
    client = ShopifyClient()
    result = apply_fix(client, fix_id)
    return JSONResponse(result)


@app.post("/fixes/{fix_id}/discard")
def fix_discard(fix_id: int):
    database.execute("UPDATE fixes SET status='discarded' WHERE id=?", (fix_id,))
    return RedirectResponse("/fixes", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    client = ShopifyClient()
    return _render(request, "settings.html", ctx(request, connection=client.check_connection()))


@app.post("/settings")
async def settings_save(
    request: Request,
    shop_domain: str = Form(""),
    mode: str = Form("mock"),
    admin_api_token: str = Form(""),
    openai_api_key: str = Form(""),
    brand: str = Form(""),
    site_url: str = Form(""),
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
    if erase_secrets == "on":
        admin_api_token = ""
        openai_api_key = ""
    updates = {
        "shop_domain": shop_domain,
        "mode": mode,
        "admin_api_token": admin_api_token,
        "openai_api_key": openai_api_key,
        "brand": brand,
        "site_url": site_url,
    }
    save_config(updates)
    return RedirectResponse("/settings", status_code=303)
