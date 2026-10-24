"""Contract tests: normalization of recorded GraphQL product nodes (2026-10 shape)."""

import json
from pathlib import Path

from app.scoring import score_product
from app.shopify.normalize import (
    faq_from_metafields,
    node_to_canonical,
    reviews_from_metafields,
    strip_internal,
)

FIXTURES = Path(__file__).parent / "fixtures" / "graphql"


def kestrel_canonical() -> dict:
    node = json.loads((FIXTURES / "kestrel_product_node.json").read_text())["product"]
    return strip_internal(node_to_canonical(node))


def test_metafield_adapter_flattens_custom_namespace_to_bare_keys():
    c = kestrel_canonical()
    assert c["metafields"]["material"] == "3-layer waterproof laminate, fully taped seams"
    assert c["metafields"]["intended_use"].startswith("Alpine climbing")
    # namespaced review metafields keep their prefix
    assert c["metafields"]["yotpo.reviews_count"] == "914"


def test_review_adapters_from_metafields():
    assert reviews_from_metafields({"yotpo.reviews_count": "914", "yotpo.average_rating": "4.7"}) == {
        "count": 914,
        "avg": 4.7,
        "source": "yotpo",
    }
    # judge.me style 0-50 weighted average gets scaled
    assert reviews_from_metafields({"judge.me.rating": "46"})["avg"] == 4.6
    # dict-shaped okendo metafield
    assert reviews_from_metafields({"okendo.summary": '{"reviewCount": 120, "averageRating": 4.2}'}) == {
        "count": 120,
        "avg": 4.2,
        "source": "okendo",
    }
    # no review data -> explicit zeros, never fabricated
    assert reviews_from_metafields({}) == {"count": None, "avg": None, "source": "none"}


def test_faq_from_geo_agent_metafield():
    metafields = {
        "geo_agent.faq_json": json.dumps([{"q": "Is it waterproof?", "a": "Yes - see the laminate spec."}])
    }
    assert faq_from_metafields(metafields) == [{"q": "Is it waterproof?", "a": "Yes - see the laminate spec."}]
    assert faq_from_metafields({}) == []
    assert faq_from_metafields({"geo_agent.faq_json": "not json"}) == []


def test_images_and_prices_normalized():
    c = kestrel_canonical()
    assert len(c["images"]) == 7  # 6 media images + 1 video node
    assert c["images"][0]["alt"].startswith("Timberline Kestrel")
    videos = [i for i in c["images"] if i["is_video"]]
    assert len(videos) == 1
    assert c["price_min"] == 289.0 and c["price_max"] == 319.0
    assert c["category"] == "Jackets & Vests"
    assert c["seo_title"].startswith("Kestrel")


def test_variants_normalized():
    c = kestrel_canonical()
    assert [v["sku"] for v in c["variants"]] == ["KES-AS-BLK-S", "KES-AS-BLK-M", "KES-AS-BLK-L"]
    assert all(v["barcode"] for v in c["variants"]), "GTIN/barcode must survive into the mirror"
    assert c["gid"] == "gid://shopify/Product/8012345678901"
    assert c["id"] == "8012345678901"


def test_realistic_product_scores_a():
    """ACCEPTANCE: live-shaped good product scores in the A/B range, not 57."""
    score = score_product(kestrel_canonical())
    assert score["score"] >= 80, f"expected A/B, got {score}"
    assert score["grade"] in ("A", "B")
