"""Characterization tests: pin current scoring behavior on the mock catalog.

These freeze today's numbers BEFORE the data layer changes them in Phase 1.
When Phase 1 improves the adapters, values may rise — update intentionally.
"""


from app.mock_data import MOCK_CATALOG
from app.scoring import WEIGHTS, score_faq, score_media, score_product, score_title


def by_pid(pid):
    return next(p for p in MOCK_CATALOG if p["id"] == pid)


def test_weights_are_stable():
    assert WEIGHTS == {"title": 20, "description": 25, "faq": 15, "media": 15, "attributes": 15, "reviews": 10}


def test_literally_complete_product_caps_each_factor():
    """A product with everything present maxes each factor (media video excepted)."""
    p = {
        "id": "x",
        "title": "Aurora Trail Backpack for rainy commuting",
        "product_type": "Backpack",
        "vendor": "Nimbus",
        "body_html": (
            "<p>Designed for rainy commuting and travel with six pockets, a padded top "
            "handle, and a removable hip belt adjustable for different torso lengths. "
            "The shell is a laminated nylon weave with taped seams.</p>"
            "<ul><li>Material: 1000D nylon</li><li>Dimensions: 32 L</li>"
            "<li>Compatibility: fits 17 ounce laptop</li><li>Limitations: not for climbing</li>"
            "<li>Care instructions: machine wash cold on a gentle cycle, skip the dryer, "
            "and hang to dry away from direct heat for best longevity.</li></ul>"
        ),
        "faq": [{"q": "a", "a": "b"}] * 3,
        "images": [
            {"src": f"https://x/{i}.jpg", "alt": f"Rubberized on-trail product view photo {i}, side angle in use"}
            for i in range(5)
        ] + [{"src": "https://x/v.mp4", "alt": "Feature tour"}],
        "metafields": {k: f"v for {k}" for k in ("material", "dimensions", "compatibility", "intended_use", "limitations")},
        "reviews": {"count": 900, "avg": 4.5, "source": "yotpo"},
    }
    s = score_product(p)
    assert s["score"] >= 90
    assert s["grade"] == "A"
    assert s["breakdown"]["attributes"] == WEIGHTS["attributes"]
    assert s["breakdown"]["faq"] == WEIGHTS["faq"]
    assert s["breakdown"]["reviews"] == WEIGHTS["reviews"]


def test_aurora_characterization():
    s = score_product(by_pid(111))
    assert s["score"] == 54
    assert s["grade"] == "C"
    assert s["breakdown"] == {"title": 9, "description": 4, "faq": 9, "media": 7, "attributes": 15, "reviews": 10}


def test_drift_characterization_thin_listing():
    s = score_product(by_pid(222))
    assert s["score"] == 10
    assert s["grade"] == "D"


def test_stuffed_title_penalty_boundary():
    val, issues = score_title(by_pid(333))
    assert val == 20, "4 ' - ' segments sit under the 5-segment spam threshold today"
    fat = {"title": "Backpack - one - two - three - four - five", "product_type": "Backpack", "body_html": "hiking"}
    v2, i2 = score_title(fat)
    assert v2 == 4
    assert any("keyword-stuffed" in t for t in i2)


def test_faq_thresholds():
    assert score_faq({"faq": []}) == (0, ["No FAQ / Q&A content on the page. Ranked-first offers include FAQs, Q&As, images, and video consistently."])
    v, issues = score_faq({"faq": [{"q": "q", "a": "a"}]})
    assert v == 9
    assert score_faq({"faq": [{"q": str(i), "a": "a"} for i in range(3)]})[0] == WEIGHTS["faq"]


def test_media_video_counts():
    p = by_pid(333)
    v, _ = score_media(p)
    assert v == WEIGHTS["media"] - 2  # 4 images (3-4 bucket) but video present: images 4 >= 3 -> -2, alt ok
    no_video = {**p, "images": [i for i in p["images"] if not i["src"].endswith(".mp4")]}
    v2, _ = score_media(no_video)
    assert v2 < v


def test_short_title_deductions():
    v, issues = score_title({"title": "Drift", "product_type": "", "body_html": "jacket"})
    assert v == 2  # 20 -9 short -4 no product_type -5 no use case
    joined = " ".join(i for i in issues)
    assert "too short" in joined and "product_type" in joined
