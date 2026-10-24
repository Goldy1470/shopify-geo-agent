import json
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as database
from .accessibility import run_full_check
from .config import load_config, save_config
from .db import execute
from .fixer import FIX_TITLES, apply_fix, generate_fixes
from .scoring import score_product
from .shopify_client import ShopifyClient
from .visibility import save_run, run_prompt_via_openai, visibility_summary

app = FastAPI(title="Shopify GEO Agent", version="0.1.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
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


@app.on_event("startup")
def startup():
    database.init_db()


def ctx(request: Request, **extra):
    cfg = load_config()
    client = ShopifyClient()
    base = {"request": request, "cfg": cfg, "client_live": client.is_live, "store_domain": cfg["shop_domain"]}
    base.update(extra)
    return base


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
    issues = {}
    for s in scored:
        for i in s["issues"]:
            issues[i["text"]] = issues.get(i["text"], 0) + 1
    top_issues = sorted(issues.items(), key=lambda kv: -kv[1])[:8]
    vis = visibility_summary(products)
    return templates.TemplateResponse(request, "dashboard.html", ctx(
        request, scored=scored, avg=avg, grades=grades, pending=pending,
        top_issues=top_issues, vis=vis, product_count=len(products),
    ))


@app.get("/products", response_class=HTMLResponse)
def products(request: Request):
    client = ShopifyClient()
    products = client.get_products()
    scored = sorted([score_product(p) for p in products], key=lambda s: s["score"])
    return templates.TemplateResponse(request, "products.html", ctx(request, scored=scored))


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(request: Request, product_id: str):
    client = ShopifyClient()
    p = client.get_product(product_id)
    if not p:
        return HTMLResponse("Product not found", status_code=404)
    scoring = score_product(p)
    pending = database.query("SELECT * FROM fixes WHERE product_id=? AND status='pending'", (product_id,))
    return templates.TemplateResponse(request, "product_detail.html", ctx(
        request, p=p, s=scoring, pending=pending, fix_titles=FIX_TITLES,
    ))


@app.get("/accessibility", response_class=HTMLResponse)
def accessibility_page(request: Request):
    history = database.query("SELECT * FROM accessibility_checks ORDER BY created_at DESC LIMIT 10")
    return templates.TemplateResponse(request, "accessibility.html", ctx(request, history=history))


@app.post("/accessibility/run")
async def accessibility_run(request: Request, store_url: str = Form(...), product_url: str = Form("")):
    result = run_full_check(store_url, product_url or None)
    execute(
        "INSERT INTO accessibility_checks (url, created_at, result) VALUES (?,?,?)",
        (store_url, datetime.now(timezone.utc).isoformat(), json.dumps(result)),
    )
    return RedirectResponse("/accessibility", status_code=303)


@app.get("/accessibility/result/{check_id}", response_class=JSONResponse)
def accessibility_result(request: Request, check_id: int):
    row = database.query("SELECT result FROM accessibility_checks WHERE id=?", (check_id,))
    return JSONResponse(json.loads(row[0]["result"]))


@app.get("/visibility", response_class=HTMLResponse)
def visibility_page(request: Request):
    client = ShopifyClient()
    products = client.get_products()
    return templates.TemplateResponse(request, "visibility.html", ctx(request, **visibility_summary(products)))


@app.post("/visibility/paste")
async def visibility_paste(request: Request, prompt: str = Form(...), engine: str = Form(...), response_text: str = Form(...)):
    client = ShopifyClient()
    run = save_run(prompt, engine, "paste", response_text, client.get_products())
    return RedirectResponse("/visibility", status_code=303)


@app.post("/visibility/api-run")
async def visibility_api_run(request: Request, prompt: str = Form(...)):
    cfg = load_config()
    if not cfg.get("openai_api_key"):
        return JSONResponse({"error": "No OpenAI API key configured. Use paste mode or add the key in Settings."}, status_code=400)
    client = ShopifyClient()
    run = run_prompt_via_openai(prompt, cfg["openai_api_key"], client.get_products())
    if run.get("error"):
        return JSONResponse(run, status_code=400)
    return RedirectResponse("/visibility", status_code=303)


@app.get("/fixes", response_class=HTMLResponse)
def fixes_page(request: Request):
    pending = database.query("SELECT * FROM fixes WHERE status='pending' ORDER BY created_at DESC")
    applied = database.query("SELECT * FROM fixes WHERE status='applied' ORDER BY applied_at DESC LIMIT 20")
    by_product = {}
    client = ShopifyClient()
    for f in pending:
        p = client.get_product(f["product_id"])
        by_product.setdefault(f["product_id"], p.get("title", f["product_id"]) if p else f["product_id"])
    return templates.TemplateResponse(request, "fixes.html", ctx(
        request, pending=pending, applied=applied, fix_titles=FIX_TITLES, by_product=by_product,
   ))


@app.post("/fixes/generate")
def fixes_generate():
    client = ShopifyClient()
    n = generate_fixes(client.get_products())
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
    return templates.TemplateResponse(request, "settings.html", ctx(request, connection=client.check_connection()))


@app.post("/settings")
async def settings_save(request: Request, shop_domain: str = Form(""), mode: str = Form("mock"),
                        admin_api_token: str = Form(""), openai_api_key: str = Form(""),
                        brand: str = Form(""), site_url: str = Form("")):
    updates = {"shop_domain": shop_domain, "mode": mode, "admin_api_token": admin_api_token,
               "openai_api_key": openai_api_key, "brand": brand, "site_url": site_url}
    save_config(updates)
    return RedirectResponse("/settings", status_code=303)
