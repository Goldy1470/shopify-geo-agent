"""Catalog readiness: mechanical data-quality checks behind the GEO scores.

Covers what the scored factors deliberately don't: taxonomy assignment,
GTIN/barcode presence, SEO metafields, variant completeness, price and
availability sanity, and Product JSON-LD on the live PDP.

Check results are structured like scoring issues:
  {"check", "status" (ready|warning|missing|unchecked), "severity",
   "fix_type", "detail", "evidence"}

Rules of the house:
- Nothing is invented. Unfetchable/unverifiable data -> "unchecked", never a
  guess; autofix suggestions stay suggestions (Phase 4 decides).
- The store's own domain is the only fetch target, via the same URL
  normalization the SSRF guard uses.
"""

import json
import re

import httpx

from .core.security import normalize_public_http_url
from .scoring_config import load_scoring_config


def _check(name: str, status: str, severity: str, fix_type: str, detail: str, evidence: dict | None = None) -> dict:
    return {
        "check": name,
        "status": status,  # ready | warning | missing | unchecked
        "severity": severity,
        "fix_type": fix_type,
        "detail": detail,
        "evidence": evidence or {},
    }


# JSON-LD fetch must fail fast; readiness is best-effort, not load-bearing.
JSONLD_TIMEOUT = 10.0


def check_product_jsonld(store_domain: str, handle: str) -> dict:
    """Fetch the PDP and validate the Product JSON-LD block.

    Returns {"status", "valid", "detail", "issues": [str], "evidence": {...}}.
    """
    domain = normalize_public_http_url(store_domain)
    if not domain or not handle:
        return {"status": "unchecked", "valid": False, "detail": "Live store URL not available.",
                "issues": ["no public store domain configured"], "evidence": {}}
    url = f"{domain}/products/{handle}"
    try:
        resp = httpx.get(url, timeout=JSONLD_TIMEOUT, follow_redirects=True,
                         headers={"User-Agent": "geo-agent-audit/1.0"})
    except httpx.HTTPError as exc:
        return {"status": "unchecked", "valid": False, "detail": f"Couldn't fetch {url} ({exc.__class__.__name__}).",
                "issues": ["fetch_failed"], "evidence": {"url": url}}
    if resp.status_code != 200:
        return {"status": "unchecked", "valid": False, "detail": f"PDP returned HTTP {resp.status_code}.",
                "issues": [f"http_{resp.status_code}"], "evidence": {"url": url}}

    blocks: list[dict] = []
    for m in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', resp.text, re.S | re.I):
        try:
            data = json.loads(m)
        except ValueError:
            continue
        if isinstance(data, list):
            blocks.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            blocks.append(data)

    product_blocks = [
        b for b in blocks
        if "Product" in ({b.get("@type")} if isinstance(b.get("@type"), str) else set(b.get("@type") or []))
    ]
    if not product_blocks:
        return {"status": "missing", "valid": False,
                "detail": "No Product JSON-LD found on the page. Agents read structured data directly.",
                "issues": ["no_product_jsonld"], "evidence": {"url": url, "ld_blocks": len(blocks)}}

    p = product_blocks[0]
    problems: list[str] = []
    offers = p.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not offers:
        problems.append("no_offers")
    else:
        if not (offers.get("price") or (offers.get("priceSpecification") or {}).get("price")):
            problems.append("no_price")
        if not offers.get("priceCurrency"):
            problems.append("no_currency")
        if not offers.get("availability"):
            problems.append("no_availability")
    if not p.get("name"):
        problems.append("no_name")
    if not (p.get("brand") or p.get("seller")):
        problems.append("no_brand")
    if not p.get("image"):
        problems.append("no_image")
    gtin = p.get("gtin") or p.get("gtin13") or p.get("gtin12")
    if not gtin:
        problems.append("no_gtin")

    valid = not problems
    return {
        "status": "ready" if valid else "warning",
        "valid": valid,
        "detail": "Product JSON-LD validates." if valid else "Product JSON-LD found but incomplete: " + ", ".join(problems),
        "issues": problems,
        "evidence": {"url": url, "gtin_present": bool(gtin)},
    }


def product_readiness(p: dict, *, check_jsonld: bool = False, store_domain: str = "") -> dict:
    """Run every readiness check for one canonical product dict."""
    checks: list[dict] = []

    # 1. Taxonomy / category assignment
    category = (p.get("category") or "").strip()
    if category:
        checks.append(_check("taxonomy", "ready", "low", "taxonomy_fill",
                             f"Category assigned: {category}.", {"category": category}))
    else:
        checks.append(_check("taxonomy", "missing", "medium", "taxonomy_fill",
                             "No taxonomy category assigned. Agents filter and compare by canonical categories.",
                             {"category": ""}))

    # 2. GTIN / barcode coverage
    variants = p.get("variants") or []
    if variants:
        missing = [v["gid"] for v in variants if not (v.get("barcode") or "").strip()]
        if missing:
            checks.append(_check("gtin", "warning", "medium", "gtin_fill",
                                 f"{len(missing)}/{len(variants)} variants missing a GTIN/barcode "
                                 "(buyable listings in agent surfaces need one).",
                                 {"missing": len(missing), "variants": len(variants)}))
        else:
            checks.append(_check("gtin", "ready", "low", "gtin_fill",
                                 "All variants carry a barcode/GTIN.", {"variants": len(variants)}))
    else:
        checks.append(_check("gtin", "missing", "medium", "gtin_fill",
                             "No variants on this product; nothing to validate barcodes against.", {"variants": 0}))

    # 3. SEO metafields
    seo_title, seo_desc = (p.get("seo_title") or "").strip(), (p.get("seo_description") or "").strip()
    if not seo_title or not seo_desc:
        missing_parts = [label for label, val in (("SEO title", seo_title), ("SEO description", seo_desc)) if not val]
        checks.append(_check("seo", "missing", "medium", "seo_fill",
                             f"Missing SEO metafield{'s' if len(missing_parts) > 1 else ''}: " + ', '.join(missing_parts) +
                             ". Agent crawlers and Google Shopping both read these.",
                             {"seo_title": bool(seo_title), "seo_description": bool(seo_desc)}))
    else:
        checks.append(_check("seo", "ready", "low", "seo_fill", "SEO title and description present.",
                             {"seo_title": len(seo_title), "seo_description": len(seo_desc)}))

    # 4. Variant completeness (every variant has sku + price)
    if variants:
        incomplete = [
            v["gid"] if not (v.get("sku") or "").strip() else None
            for v in variants
        ]
        no_sku = [x for x in incomplete if x]
        bad_price = [v["gid"] for v in variants if not v.get("price") or float(v.get("price") or 0) <= 0]
        if no_sku or bad_price:
            parts = []
            if no_sku:
                parts.append(f"{len(no_sku)}/{len(variants)} missing SKU")
            if bad_price:
                parts.append(f"{len(bad_price)}/{len(variants)} missing valid price")
            checks.append(_check("variants", "warning", "medium", "variant_fill",
                                 "Variant data incomplete: " + "; ".join(parts) + ".",
                                 {"no_sku": len(no_sku), "bad_price": len(bad_price), "variants": len(variants)}))
        else:
            checks.append(_check("variants", "ready", "low", "variant_fill",
                                 f"All {len(variants)} variants carry SKU and price.", {"variants": len(variants)}))
    else:
        checks.append(_check("variants", "missing", "low", "variant_fill",
                             "No variants exist (single implicit offer).", {"variants": 0}))

    # 5. Price / availability sanity
    pmin, pmax = p.get("price_min"), p.get("price_max")
    if pmin is None or pmax is None:
        checks.append(_check("price", "missing", "medium", "price_fix",
                            "No price range on record (usually a variant sync gap).", {"price_min": pmin, "price_max": pmax}))
    elif pmin <= 0 or pmax <= 0 or pmin > pmax:
        checks.append(_check("price", "warning", "high", "price_fix",
                             f"Price range looks wrong: min={pmin}, max={pmax}.",
                             {"price_min": pmin, "price_max": pmax}))
    else:
        checks.append(_check("price", "ready", "low", "price_fix",
                             f"Price range {pmin}-{pmax} looks sane.", {"price_min": pmin, "price_max": pmax}))
    statuses = [v.get("available") for v in variants if v.get("available") is not None]
    if statuses:
        if not any(statuses):
            checks.append(_check("availability", "warning", "high", "price_fix",
                                 "Every variant reads as unavailable to buyers.", {"available_axes": len(statuses)}))
        else:
            checks.append(_check("availability", "ready", "low", "price_fix",
                                 f"{sum(1 for s in statuses if s)}/{len(statuses)} variants available.", {}))

    # 6. Image alt-text coverage
    images = p.get("images") or []
    plain = [i for i in images if not i.get("is_video")]
    if plain:
        missing_alt = sum(1 for i in plain if not (i.get("alt") or "").strip())
        if missing_alt == len(plain):
            checks.append(_check("alt_text", "missing", "high", "alt_text",
                                 f"All {len(plain)} images lack alt text.", {"images": len(plain), "missing": missing_alt}))
        elif missing_alt:
            checks.append(_check("alt_text", "warning", "medium", "alt_text",
                                 f"{missing_alt}/{len(plain)} images missing alt text.", {"images": len(plain), "missing": missing_alt}))
        else:
            checks.append(_check("alt_text", "ready", "low", "alt_text",
                                 f"All {len(plain)} images have alt text.", {"images": len(plain)}))
    else:
        checks.append(_check("alt_text", "missing", "medium", "media_add", "No images on record.", {"images": 0}))

    # 7. PDP JSON-LD (fetch only when explicitly asked + live store)
    if check_jsonld and store_domain:
        ld = check_product_jsonld(store_domain, p.get("handle") or "")
        checks.append(_check(
            "jsonld",
            ld["status"],
            "low" if ld["status"] == "ready" else "high" if ld["status"] == "missing" else "medium",
            "jsonld_snippet",
            ld["detail"],
            ld["evidence"],
        ))
    else:
        checks.append(_check("jsonld", "unchecked", "low", "jsonld_snippet",
                             "Enable a live fit check to fetch and validate the PDP's Product JSON-LD.", {}))

    counts = {"ready": 0, "warning": 0, "missing": 0, "unchecked": 0}
    for c in checks:
        counts[c["status"]] += 1
    return {
        "product_id": str(p["id"]),
        "checks": checks,
        "counts": counts,
        "heuristic_provenance": load_scoring_config().get("meta", {}).get("source", ""),
    }
