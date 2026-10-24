"""Catalog readiness checks (taxonomy, GTIN, SEO, variants, price, alt text, JSON-LD)."""

import json
from unittest.mock import patch

from app.readiness import check_product_jsonld, product_readiness


def base_product(**overrides) -> dict:
    p = {
        "id": "1",
        "title": "Kestrel Shell",
        "handle": "kestrel-shell",
        "category": "Jackets & Vests",
        "seo_title": "Kestrel Shell jacket",
        "seo_description": "Waterproof alpine shell, 3-layer.",
        "price_min": 289.0,
        "price_max": 319.0,
        "images": [{"src": "https://x/1.jpg", "alt": "Kestrel shell front view", "is_video": False}],
        "variants": [
            {"gid": "gid://shopify/ProductVariant/1", "sku": "K1", "barcode": "0123456789", "price": 289.0,
             "available": True},
            {"gid": "gid://shopify/ProductVariant/2", "sku": "K2", "barcode": "0123456790", "price": 299.0,
             "available": True},
        ],
    }
    p.update(overrides)
    return p


def by_check(result: dict, name: str) -> dict:
    return next(c for c in result["checks"] if c["check"] == name)


def test_healthy_product_is_mostly_ready():
    r = product_readiness(base_product())
    for name in ("taxonomy", "gtin", "seo", "variants", "price", "alt_text"):
        assert by_check(r, name)["status"] == "ready", name
    assert r["counts"]["ready"] >= 5


def test_missing_taxonomy_is_a_fixable_check():
    r = product_readiness(base_product(category=""))
    c = by_check(r, "taxonomy")
    assert c["status"] == "missing"
    assert c["fix_type"] == "taxonomy_fill" and c["severity"] == "medium"


def test_gtin_coverage():
    p = base_product(variants=[{"gid": "v1", "sku": "a", "barcode": "", "price": 10.0, "available": True}])
    c = by_check(product_readiness(p), "gtin")
    assert c["status"] == "warning" and c["fix_type"] == "gtin_fill"
    assert c["evidence"] == {"missing": 1, "variants": 1}


def test_price_sanity():
    r = product_readiness(base_product(price_min=400.0, price_max=300.0))
    c = by_check(r, "price")
    assert c["status"] == "warning" and c["severity"] == "high" and c["fix_type"] == "price_fix"

    r2 = product_readiness(base_product(price_min=None, price_max=None))
    assert by_check(r2, "price")["status"] == "missing"


def test_all_variants_unavailable_flags_high():
    p = base_product(variants=[{"gid": "v1", "sku": "a", "barcode": "x", "price": 10.0, "available": False}])
    c = by_check(product_readiness(p), "availability")
    assert c["status"] == "warning" and c["severity"] == "high"


def test_alt_text_coverage():
    r = product_readiness(base_product(images=[{"src": "a.jpg", "alt": "", "is_video": False}]))
    c = by_check(r, "alt_text")
    assert c["status"] == "missing" and c["severity"] == "high" and c["fix_type"] == "alt_text"


def test_jsonld_skipped_without_live_fetch():
    r = product_readiness(base_product())
    c = by_check(r, "jsonld")
    assert c["status"] == "unchecked" and c["fix_type"] == "jsonld_snippet"


def test_jsonld_fetch_failure_stays_unchecked():
    import httpx as httpx_mod

    with patch("app.readiness.httpx.get", side_effect=httpx_mod.ConnectError("boom")):
        ld = check_product_jsonld("kestrel.example.com", "kestrel-shell")
    assert ld["status"] == "unchecked" and ld["issues"] == ["fetch_failed"]


def test_jsonld_http_404_is_unchecked():
    class R:
        status_code = 404
        text = ""

    with patch("app.readiness.httpx.get", return_value=R()):
        ld = check_product_jsonld("kestrel.example.com", "ghost")
    assert ld["status"] == "unchecked" and ld["issues"] == ["http_404"]


GOOD_LD = json.dumps(
    {
        "@type": "Product",
        "name": "Kestrel Shell",
        "brand": {"@type": "Brand", "name": "Kestrel"},
        "image": "https://x/1.jpg",
        "gtin13": "0123456789",
        "offers": {"@type": "Offer", "price": "289.00", "priceCurrency": "USD",
                   "availability": "http://schema.org/InStock"},
    }
)


def _fake_pdp(html: str):
    class R:
        status_code = 200
        text = html

    return R


def test_jsonld_validates_complete_block():
    html = f'<html><script type="application/ld+json">{GOOD_LD}</script></html>'
    with patch("app.readiness.httpx.get", return_value=_fake_pdp(html)):
        ld = check_product_jsonld("kestrel.example.com", "kestrel-shell")
    assert ld["status"] == "ready" and ld["valid"] is True and ld["issues"] == []


def test_jsonld_reports_incomplete_fields():
    thin = json.dumps({"@type": "Product", "name": "Kestrel Shell", "offers": []})
    html = f'<html><script type="application/ld+json">{thin}</script></html>'
    with patch("app.readiness.httpx.get", return_value=_fake_pdp(html)):
        ld = check_product_jsonld("kestrel.example.com", "kestrel-shell")
    assert ld["status"] == "warning" and ld["valid"] is False
    assert {"no_offers", "no_brand", "no_image", "no_gtin"} <= set(ld["issues"])
    # with an offers block present but stale, field-level gaps are reported
    stale = json.dumps({"@type": "Product", "name": "Kestrel Shell",
                        "offers": [{"@type": "Offer", "priceCurrency": "USD"}]})
    html2 = f'<html><script type="application/ld+json">{stale}</script></html>'
    with patch("app.readiness.httpx.get", return_value=_fake_pdp(html2)):
        ld2 = check_product_jsonld("kestrel.example.com", "kestrel-shell")
    assert {"no_price", "no_availability"} <= set(ld2["issues"])


def test_jsonld_missing_block_is_a_high_readiness_check():
    with patch("app.readiness.httpx.get", return_value=_fake_pdp("<html></html>")):
        r = product_readiness(base_product(), check_jsonld=True, store_domain="kestrel.example.com")
    c = by_check(r, "jsonld")
    assert c["status"] == "missing" and c["severity"] == "high" and c["fix_type"] == "jsonld_snippet"
