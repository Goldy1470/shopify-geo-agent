"""GraphQL product node -> canonical product dict.

Includes the adapters that make scoring work on real-shaped stores:
- metafields flattened to {"ns.key": value}
- review-app metafields (Yotpo / Judge.me / Okendo / Shopify Product Reviews
  where they still write metafields) -> {"count","avg","source"} (best effort,
  never scraped)
- FAQ pair list from the geo_agent.faq_json json metafield
- image alt text + video flags
Nothing is invented: missing facts stay missing (the suggestion engine emits
"missing data" tasks instead of fabricating copy).
"""

import json
from typing import Any

from app.db.models import split_metafield_key


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


REVIEW_NAMESPACES = ("yotpo", "judge.me", "judgeme", "okendo", "reviews", "product-reviews", "spr")
REVIEW_COUNT_KEYS = ("rating_count", "reviews_count", "review_count", "count", "total_reviews")
REVIEW_AVG_KEYS = ("average_rating", "avg_rating", "rating_average", "rating", "average", "avg")


def _norm(s: str) -> str:
    return s.lower().replace("_", "").replace("-", "").replace(" ", "")


_COUNT_TAGS = tuple(_norm(k) for k in REVIEW_COUNT_KEYS)
_AVG_TAGS = tuple(_norm(k) for k in REVIEW_AVG_KEYS)


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.replace(".", "", 1).isdigit():
        return float(value)
    return None


def reviews_from_metafields(metafields: dict[str, str]) -> dict:
    """Scan review-app metafields for count/avg. Best-effort per ASSUMPTIONS #4.

    Matching is underscore-insensitive so camelCase app payloads
    (okendo "reviewCount") and snake_case keys both land. Rating averages are
    only kept when they look like a 0-5 scale or a Judge.me-style 0-50
    weighted average (scaled by /10); anything else is left "missing" rather
    than guessed.
    """
    found_count: float | None = None
    found_avg: float | None = None
    source = ""
    for key, value in metafields.items():
        ns, short = split_metafield_key(key)
        if ns.lower() not in REVIEW_NAMESPACES:
            continue
        short_low = _norm(short)
        try:
            decoded = json.loads(value) if value and value[:1] in "{[" else value
        except ValueError:
            decoded = value
        if isinstance(decoded, dict):
            for k, v in decoded.items():
                k_norm = _norm(str(k))
                num = _num(v)
                if num is None:
                    continue
                if any(tag in k_norm for tag in _COUNT_TAGS):
                    found_count = num if found_count is None else max(found_count, num)
                    source = source or ns
                elif any(tag in k_norm for tag in _AVG_TAGS):
                    scaled = num if 0 <= num <= 5 else (round(num / 10.0, 2) if 5 < num <= 50 else None)
                    if scaled is not None:
                        found_avg = scaled if found_avg is None else max(found_avg, scaled)
                        source = source or ns
            continue
        num = _num(decoded)
        if num is None:
            continue
        if any(tag in short_low for tag in _COUNT_TAGS):
            found_count = num if found_count is None else max(found_count, num)
            source = source or ns
        elif any(tag in short_low for tag in _AVG_TAGS):
            scaled = num if 0 <= num <= 5 else (round(num / 10.0, 2) if 5 < num <= 50 else None)
            if scaled is not None:
                found_avg = scaled if found_avg is None else max(found_avg, scaled)
            source = source or ns
    return {
        "count": int(found_count) if found_count is not None else None,
        "avg": found_avg,
        "source": source or "none",
    }


def faq_from_metafields(metafields: dict[str, str]) -> list[dict]:
    raw = metafields.get("geo_agent.faq_json") or "[]"
    try:
        items = json.loads(raw)
        return [{"q": str(i.get("q", "")), "a": str(i.get("a", ""))} for i in items if isinstance(i, dict)]
    except ValueError:
        return []


def node_to_canonical(node: dict, *, raw: bool = False) -> dict:
    """Turn a GraphQL Product node into the canonical dict."""
    price = node.get("priceRangeV2") or {}
    min_p = _float((price.get("minVariantPrice") or {}).get("amount"))
    max_p = _float((price.get("maxVariantPrice") or {}).get("amount"))
    seo = node.get("seo") or {}
    category = node.get("category") or {}
    images = []
    for img in ((node.get("images") or {}).get("nodes") or []):
        images.append(
            {
                "src": img.get("url") or "",
                "alt": img.get("altText") or "",
                "position": int(img.get("position") or 0),
                "is_video": False,
            }
        )
    for vid in ((node.get("video") or {}).get("nodes") or []):
        images.append({"src": "", "alt": vid.get("altText") or "", "position": 100, "is_video": True})
    variants = []
    for v in ((node.get("variants") or {}).get("nodes") or []):
        variants.append(
            {
                "gid": v.get("id") or "",
                "title": v.get("title") or "",
                "sku": v.get("sku") or "",
                "barcode": v.get("barcode") or "",
                "price": _float((v.get("price") or {}).get("amount")) or 0.0,
                "available": None if v.get("currentlyNotAvailable") is None else (not v["currentlyNotAvailable"]),
                "position": int(v.get("position") or 0),
            }
        )
    metaflat: dict[str, str] = {}
    mm_nodes = (node.get("metafields") or {}).get("nodes") or []
    for m in mm_nodes:
        ns, key = str(m.get("namespace") or ""), str(m.get("key") or "")
        flat_key = key if ns == "custom" else f"{ns}.{key}"  # scoring consumes bare custom keys
        metaflat[flat_key] = m.get("value") or ""
    meta_types = {f"{m.get('namespace')}.{m.get('key')}": (m.get("type") or "") for m in mm_nodes}
    canonical = {
        "id": node["id"].rsplit("/", 1)[-1],
        "gid": node["id"],
        "title": node.get("title") or "",
        "handle": node.get("handle") or "",
        "product_type": node.get("productType") or "",
        "vendor": node.get("vendor") or "",
        "status": node.get("status") or "",
        "tags": list(node.get("tags") or []),
        "body_html": node.get("descriptionHtml") or "",
        "seo_title": seo.get("title") or "",
        "seo_description": seo.get("description") or "",
        "category": category.get("name") or "",
        "price_min": min_p,
        "price_max": max_p,
        "images": images,
        "variants": variants,
        "metafields": metaflat,
        "faq": faq_from_metafields(metaflat),
        "reviews": reviews_from_metafields(metaflat),
        "updated_at": node.get("updatedAt") or "",
    }
    if raw:
        canonical["raw_json"] = json.dumps(node, default=str)
    canonical["_meta_types"] = meta_types
    return canonical


def strip_internal(canonical: dict) -> dict:
    return {k: v for k, v in canonical.items() if not k.startswith("_")}
