"""Mention-detection regression + app-wide smoke tests."""

from app.mock_data import MOCK_CATALOG
from app.visibility import extract_mentions

# Pages that render POST forms must ship the CSRF hidden field.
FORM_PAGES = ("/accessibility", "/visibility", "/fixes", "/settings")


def test_generic_category_word_does_not_flag_products():
    m = extract_mentions(
        "Best duffel bags: our pick is the Osprey Transporter 40 from Amazon.",
        MOCK_CATALOG,
    )
    ids = [h["product_id"] for h in m["products"]]
    assert "333" not in ids, "generic 'duffel' must not summon the Weekender Duffel"


def test_full_product_name_matches():
    m = extract_mentions("The Weekender Duffel is our budget pick.", MOCK_CATALOG)
    ids = [h["product_id"] for h in m["products"]]
    assert "333" in ids


def test_longest_token_matches_multiword_title():
    m = extract_mentions("The Aurora Pack wins for carry-on.", MOCK_CATALOG)
    assert "111" in [h["product_id"] for h in m["products"]]


def test_backpack_generic_does_not_flag_aurora():
    m = extract_mentions("Great backpack options overall: check REI.", MOCK_CATALOG)
    assert "111" not in [h["product_id"] for h in m["products"]]


def test_drift_single_word_product():
    m = extract_mentions("Drift is 87 reviews and thin.", MOCK_CATALOG)
    assert "222" in [h["product_id"] for h in m["products"]]


def test_brand_and_merchants_extracted():
    m = extract_mentions(
        "Nimbus Outfitters bags ship from Walmart and Amazon.", MOCK_CATALOG
    )
    assert "Nimbus Outfitters" in m["brand"]
    assert m["merchants"]


def test_pages_render(client):
    for path in ("/", "/products", "/products/111", "/accessibility", "/visibility", "/fixes", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, path
    for path in FORM_PAGES:
        r = client.get(path)
        assert 'name="csrf_token"' in r.text, f"{path} must include the CSRF hidden field"
