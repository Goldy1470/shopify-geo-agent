"""Regression tests for the KNOWN BUGS in fixer.py (fixed in Phase 0)."""

import json

from app.fixer import (
    draft_description_fix,
    draft_faq_fix,
    draft_title_fix,
    validate_no_placeholders,
)


def base_product(**over):
    p = {
        "id": 111,
        "title": "Aurora Pack",
        "handle": "aurora-pack",
        "product_type": "Travel Backpack",
        "vendor": "Nimbus Outfitters",
        "body_html": "<p>Waterproof roll-top pack for trips.</p>",
        "faq": [],
        "metafields": {
            "material": "600D nylon",
            "dimensions": "28 x 18 x 12 in",
            "compatibility": "Fits 16 inch laptop",
            "intended_use": "Multi day trips, commuting",
            "limitations": "Not for climbing",
        },
    }
    p.update(over)
    return p


def test_title_keeps_product_name():
    fix = draft_title_fix(base_product())
    assert fix is not None
    assert fix["before"] == "Aurora Pack"
    assert fix["after"].startswith("Aurora Pack"), "product name must survive"
    assert "Travel Backpack" in fix["after"]
    low = fix["after"].lower()
    assert any(w in low for w in ("commuting", "rain", "trip"))


def test_title_never_injects_placeholders():
    fix = draft_title_fix(base_product(metafields={"intended_use": "TODO decide"}))
    if fix:
        assert not validate_no_placeholders(fix["after"])


def test_stuffed_title_left_alone_for_phase4():
    stuffed = base_product(title="Backpack - for - travel - best - top - cheap - sale")
    assert draft_title_fix(stuffed) is None


def test_faq_compatibility_template_has_no_placeholder():
    fix = draft_faq_fix(base_product())
    assert fix is not None
    items = json.loads(fix["after"])
    compat = next(i for i in items if "compat" in i["q"].lower() or "fit" in i["q"].lower())
    assert "..." not in compat["q"] + compat["a"]
    assert "16 inch" in compat["a"]  # grounded in product data


def test_faq_skips_missing_fields():
    fix = draft_faq_fix(base_product(metafields={"material": "canvas"}))
    items = json.loads(fix["after"])
    assert {i["q"] for i in items} <= {"What is it made of?", "What are the dimensions?"}


def test_placeholder_validator_blocks_known_bugs():
    assert validate_no_placeholders("Is it compatible with ...?")
    assert validate_no_placeholders("")
    assert validate_no_placeholders("Uses {value}")
    assert validate_no_placeholders("TODO fix me")
    assert validate_no_placeholders("Clean description text.") == []


def test_description_block_is_escaped_and_idempotent():
    p = base_product(metafields={"material": "<script>alert('xss')</script>"})
    fix = draft_description_fix(p)
    assert fix is not None
    after = fix["after"]
    assert "<script>" not in after
    assert "&lt;script&gt;" in after
    assert after.count("geo-agent:specs start") == 1

    second = draft_description_fix({**p, "body_html": after})
    assert second is None, "second draft on the same data must be a no-op"


def test_description_block_replaces_stale_block():
    p = base_product(metafields={"material": "600D nylon"})
    first = draft_description_fix(p)
    stale_body = first["after"].replace("600D nylon", "OLD stale specs")
    refreshed = draft_description_fix({**p, "body_html": stale_body})
    assert refreshed is not None
    after = refreshed["after"]
    assert after.count("geo-agent:specs start") == 1, "must replace, not stack"
    assert "600D nylon" in after
    assert "OLD stale specs" not in after


def test_description_block_gives_missing_data_task_not_fabrication():
    p = base_product(metafields={})
    assert draft_description_fix(p) is None


def test_title_trims_the_suffix_never_the_name():
    long_name = "Aurora Weatherproof Rolltop Travel Companion Pack"
    fix = draft_title_fix(base_product(title=long_name))
    assert fix is not None
    assert fix["after"].startswith(long_name)
    assert len(fix["after"]) <= 78


def test_title_refuses_noop_placeholder_and_name_mutation():
    # Product type + use case are already in the title: nothing safe to add.
    assert draft_title_fix(base_product(title="Aurora Travel Backpack commuting")) is None
    # A placeholder in the product type must never ship.
    assert draft_title_fix(base_product(product_type="TODO")) is None
    # A title containing " - " would make the name check ambiguous: refuse.
    assert draft_title_fix(base_product(title="Aurora - Pack")) is None


def test_faq_skips_when_three_exist_and_blocks_placeholder_values():
    existing = [{"q": "q1", "a": "a1"}, {"q": "q2", "a": "a2"}, {"q": "q3", "a": "a3"}]
    assert draft_faq_fix(base_product(faq=existing)) is None
    assert draft_faq_fix(base_product(metafields={"material": "..."})) is None


def test_description_refuses_placeholder_metafields():
    assert draft_description_fix(base_product(metafields={"material": "TODO"})) is None


def test_legacy_generate_fixes_is_idempotent():
    from app.fixer import generate_fixes

    p = base_product()
    assert generate_fixes([p]) >= 1
    assert generate_fixes([p]) == 0
