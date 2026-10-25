"""Phase 4 SUGGEST engine: validators, impact scoring, queue, LLM judge."""

import json

import pytest

from app import fixer
from app.db import query
from app.suggest import (
    ALT_MAX,
    SEO_DESC_MAX,
    alt_text_problems,
    caps_problems,
    catalog_price_ceiling,
    generate_suggestions,
    html_safe_problems,
    impact_score,
    jsonld_snippet,
    llm_refine,
    name_preserved_problems,
    suggest_for_product,
    validate_suggestion,
)


def product(**over):
    p = {
        "id": "111",
        "title": "Aurora Pack",
        "handle": "aurora-pack",
        "product_type": "Backpack",
        "vendor": "Nimbus Outfitters",
        "status": "active",
        "tags": ["travel", "waterproof"],
        "body_html": (
            "<p>The Aurora Pack was made for long trips in bad weather. It has a 32 L main "
            "compartment, a top grab handle, and a roll-top closure that keeps rain out.</p>"
            "<ul><li>Capacity: 32 liters</li></ul>"
        ),
        "seo_title": "",
        "seo_description": "",
        "category": "",
        "price_min": None,
        "price_max": None,
        "images": [
            {"src": "https://cdn.example/a.jpg", "alt": "Aurora Pack front view", "position": 1, "is_video": False},
            {"src": "https://cdn.example/b.jpg", "alt": "", "position": 2, "is_video": False},
        ],
        "variants": [
            {
                "gid": "gid://shopify/ProductVariant/1",
                "sku": "AUR-32",
                "barcode": "",
                "price": 184.0,
                "available": True,
            }
        ],
        "metafields": {
            "material": "500D TPU-coated nylon with PU rain cover",
            "dimensions": "28 x 18 x 12 in, 32 L capacity",
            "compatibility": "Fits 15-17 inch laptops",
            "intended_use": "Multi-day travel, commuting in rain",
            "limitations": "Not for climbing or over 40 lb loads",
        },
        "faq": [],
        "reviews": {"count": 812, "avg": 4.6, "source": "yotpo"},
    }
    p.update(over)
    return p


# --- validators --------------------------------------------------------------


def test_name_and_caps_validators():
    assert name_preserved_problems("Aurora Pack", "Aurora Pack - Backpack") == []
    assert name_preserved_problems("Aurora Pack", "Travel Backpack")
    assert caps_problems("AURORA PACK")
    assert caps_problems("Best - travel - backpack - top - cheap - sale")
    assert caps_problems("Aurora Pack - Backpack") == []


def test_html_safe_validator_blocks_executable_markup():
    assert html_safe_problems('<div class="geo-specs"><ul><li><strong>Material:</strong> nylon</li></ul></div>') == []
    assert html_safe_problems("<script>alert(1)</script>")
    assert html_safe_problems('<div onclick="steal()">x</div>')
    assert html_safe_problems('<a href="javascript:alert(1)">x</a>')
    assert html_safe_problems("<table><tr><td>x</td></tr></table>")


def test_alt_text_must_reuse_title_and_position():
    assert alt_text_problems("Aurora Pack — product image 2 of 2", product()) == []
    assert alt_text_problems("A backpack on a table", product())
    assert alt_text_problems("Aurora Pack — leather texture", product())
    assert alt_text_problems("Aurora Pack — product image 2 of 2, premium leather", product())


def test_validate_faq_blocks_invalid_json_and_invented_claims():
    p = product()
    bad_json = {"kind": "faq_generate", "after": "not json", "field": "faq", "auto": 1}
    assert validate_suggestion(bad_json, p)

    invented = {
        "kind": "faq_generate",
        "after": json.dumps([{"q": "What is it made of?", "a": "It is made of leather."}]),
        "field": "faq",
        "auto": 1,
    }
    assert validate_suggestion(invented, p)

    grounded = fixer.draft_faq_fix(p)
    assert grounded is not None
    assert validate_suggestion({**grounded, "auto": 1}, p) == []


def test_validate_description_block_traces_every_value():
    p = product()
    draft = fixer.draft_description_fix(p)
    assert draft is not None
    assert validate_suggestion({**draft, "auto": 1}, p) == []

    tampered = dict(draft, after=draft["after"].replace("500D TPU-coated nylon", "premium leather"))
    problems = validate_suggestion({**tampered, "auto": 1}, p)
    assert any("untraceable" in problem for problem in problems)


def test_validate_title_seo_and_limits():
    p = product()
    dropped = {"kind": "title_rewrite", "before": "Aurora Pack", "after": "Travel Backpack", "auto": 1}
    assert validate_suggestion(dropped, p)

    kept = {"kind": "title_rewrite", "before": "Aurora Pack", "after": "Aurora Pack - Backpack", "auto": 1}
    assert validate_suggestion(kept, p) == []

    seo = {"kind": "seo_fill_title", "before": "", "after": "Aurora Pack", "auto": 1}
    assert validate_suggestion(seo, p) == []
    seo_dropped = {"kind": "seo_fill_title", "before": "", "after": "Travel Backpack", "auto": 1}
    assert validate_suggestion(seo_dropped, p)

    long_alt = {"kind": "alt_text", "after": "Aurora Pack — product image 1 of 2 " + "x" * ALT_MAX, "auto": 1}
    assert validate_suggestion(long_alt, p)


# --- impact ------------------------------------------------------------------


def test_impact_is_monotonic_in_gap_price_and_readiness():
    good = {"title": 18, "description": 25, "faq": 15, "media": 15, "attributes": 15, "reviews": 10}
    worse = {**good, "title": 5}

    cheap = impact_score("title_rewrite", worse, None, 50, 1000)
    expensive = impact_score("title_rewrite", worse, None, 1000, 1000)
    assert expensive > cheap
    assert impact_score("title_rewrite", good, None, 1000, 1000) < expensive

    missing = impact_score("jsonld_snippet", good, "missing", 500, 1000)
    warning = impact_score("jsonld_snippet", good, "warning", 500, 1000)
    assert missing > warning


def test_catalog_price_ceiling_uses_variant_fallback():
    assert catalog_price_ceiling([product(price_max=None, variants=[{"price": 75}])]) == 75
    assert catalog_price_ceiling([product(price_max=None, variants=[])]) is None


# --- builders ----------------------------------------------------------------


def test_suggest_for_product_covers_the_fix_types():
    p = product()
    suggestions = suggest_for_product(p)
    keys = {(s["kind"], s["field"]) for s in suggestions}
    assert ("title_rewrite", "title") in keys
    assert ("faq_generate", "faq") in keys
    assert ("description_append", "body_html") in keys
    assert ("alt_text", "image:2") in keys
    assert ("seo_fill_title", "seo_title") in keys
    assert ("seo_fill_description", "seo_description") in keys
    assert ("taxonomy_fill", "category") in keys
    assert ("gtin_fill", "barcode") in keys
    assert ("price_fix", "price") in keys
    assert ("jsonld_snippet", "theme_snippet") in keys
    assert ("media_add", "video") in keys

    auto = [s for s in suggestions if s["auto"]]
    assert auto
    assert all(s["after"].strip() for s in auto)
    assert all(s["impact"] >= 0 for s in suggestions)
    assert all(validate_suggestion(s, p) == [] for s in auto)


def test_manual_tasks_have_no_draft_and_are_not_applicable():
    p = product(images=[], reviews={"count": 0}, metafields={})
    suggestions = suggest_for_product(p)
    manual = [s for s in suggestions if not s["auto"]]
    assert manual
    assert all(s["after"] == "" for s in manual if s["kind"] != "jsonld_snippet")
    assert any(s["kind"] == "jsonld_snippet" and s["after"] for s in manual)
    assert any(s["kind"] == "media_add" and s["field"] == "images" for s in manual)
    assert any(s["kind"] == "review_nudge" for s in manual)
    assert not any(s["kind"] == "alt_text" for s in suggestions)


def test_seo_description_truncates_on_a_word_boundary():
    body = "<p>" + ("alpha " * 80) + "omega.</p>"
    suggestions = suggest_for_product(product(body_html=body))
    seo = next(s for s in suggestions if s["kind"] == "seo_fill_description" and s["auto"])
    assert len(seo["after"]) <= SEO_DESC_MAX
    assert seo["after"].split()[-1] == "alpha"
    assert "omega" not in seo["after"]


def test_jsonld_snippet_is_dynamic_and_valid():
    p = product()
    snippet = jsonld_snippet(p)
    assert "application/ld+json" in snippet
    assert "product.title | json" in snippet
    assert "cart.currency.iso_code" in snippet
    assert "geo_variant.price" in snippet
    assert "product.available" in snippet
    assert '"priceCurrency": "USD"' not in snippet
    assert validate_suggestion({"kind": "jsonld_snippet", "after": snippet}, p) == []


# --- queue -------------------------------------------------------------------


def test_generate_suggestions_returns_inserted_ids_and_dedupes():
    p = product()
    first = generate_suggestions([p], cfg={})
    assert first
    assert all("id" in s for s in first)
    pending = query("SELECT id FROM fixes WHERE status='pending'")
    assert len(pending) == len(first)

    second = generate_suggestions([p], cfg={})
    assert second == []
    assert len(query("SELECT id FROM fixes WHERE status='pending'")) == len(first)


def test_generate_suggestions_orders_expensive_products_first():
    cheap = product(id="111", title="Budget Pack", price_min=50, price_max=50)
    expensive = product(id="222", title="Premium Pack", price_min=500, price_max=500)
    generate_suggestions([cheap, expensive], cfg={})
    rows = query(
        "SELECT product_id, impact FROM fixes WHERE status='pending' ORDER BY impact DESC, id DESC"
    )
    assert rows[0]["product_id"] == "222"
    assert rows[0]["impact"] > max(
        row["impact"] for row in rows if row["product_id"] == "111"
    )


# --- LLM judge ---------------------------------------------------------------


def test_llm_refine_persists_a_valid_revision(monkeypatch):
    p = product()
    inserted = generate_suggestions([p], cfg={})
    target = next(s for s in inserted if s["kind"] == "title_rewrite")

    class FakeJudge:
        def __init__(self, key):
            assert key == "test-key"

        async def judge(self, draft, corpus):
            assert "500D" in corpus
            return {
                "verdict": "revise",
                "reason": "tighten",
                "revised": draft["before"] + " - Backpack",
            }

    monkeypatch.setattr("app.suggest.SuggestJudge", FakeJudge)
    refined = llm_refine([target], [p], {"openai_api_key": "test-key"}, limit=1)
    assert refined[0]["after"] == "Aurora Pack - Backpack"

    row = query("SELECT after, judge, auto FROM fixes WHERE id=?", (target["id"],))[0]
    assert row["after"] == "Aurora Pack - Backpack"
    assert json.loads(row["judge"])["verdict"] == "revise"
    assert row["auto"] == 1


def test_llm_refine_rejection_downgrades_to_manual(monkeypatch):
    p = product()
    inserted = generate_suggestions([p], cfg={})
    target = next(s for s in inserted if s["kind"] == "title_rewrite")

    class RejectJudge:
        def __init__(self, key):
            pass

        async def judge(self, draft, corpus):
            return {"verdict": "reject", "reason": "invented material", "revised": None}

    monkeypatch.setattr("app.suggest.SuggestJudge", RejectJudge)
    llm_refine([target], [p], {"openai_api_key": "test-key"}, limit=1)

    row = query("SELECT judge, auto, notes FROM fixes WHERE id=?", (target["id"],))[0]
    assert json.loads(row["judge"])["verdict"] == "reject"
    assert row["auto"] == 0
    assert "LLM judge rejected" in row["notes"]


def test_llm_refine_requires_a_key_and_survives_bad_output(monkeypatch):
    p = product()
    inserted = generate_suggestions([p], cfg={})
    target = next(s for s in inserted if s["kind"] == "title_rewrite")
    assert llm_refine([target], [p], {"openai_api_key": ""}) == [target]

    class BrokenJudge:
        def __init__(self, key):
            pass

        async def judge(self, draft, corpus):
            raise json.JSONDecodeError("bad", "", 0)

    monkeypatch.setattr("app.suggest.SuggestJudge", BrokenJudge)
    llm_refine([target], [p], {"openai_api_key": "test-key"}, limit=1)
    row = query("SELECT judge FROM fixes WHERE id=?", (target["id"],))[0]
    assert json.loads(row["judge"])["verdict"] == "unavailable"


@pytest.mark.parametrize("kind", ["faq", "description_section", "metafield_fill"])
def test_manual_kinds_are_not_applicable(kind):
    from app.fixer import APPLICABLE_KINDS

    assert kind not in APPLICABLE_KINDS
