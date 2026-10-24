"""Scoring configuration: YAML loading, fallbacks, configurability, issue shape."""

import pytest

from app.scoring import ATTRIBUTE_KEYS, GRADES, USE_CASE_WORDS, WEIGHTS, score_product, score_reviews
from app.scoring_config import config_provenance, load_scoring_config, reset_config, set_config


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    reset_config()


def test_shipped_yaml_is_valid_and_provenant():
    cfg = load_scoring_config(force=True)
    assert cfg["meta"]["source"], "heuristic provenance must be recorded"
    assert cfg["meta"]["resolved_from"].endswith("scoring.yaml"), "shipped file must win over fallback"
    assert cfg["thresholds"]["reviews"]["full_count"] == 787
    config_provenance()  # smoke: provenance helpers use the same cache


def test_weights_and_lists_come_from_config():
    assert WEIGHTS == {"title": 20, "description": 25, "faq": 15, "media": 15, "attributes": 15, "reviews": 10}
    assert ATTRIBUTE_KEYS == ["material", "dimensions", "compatibility", "intended_use", "limitations"]
    assert "commut" in USE_CASE_WORDS
    assert GRADES == {"A": 80, "B": 60, "C": 40}


def test_review_threshold_is_configurable():
    # default: 787 is the top band
    assert score_reviews({"reviews": {"count": 800}})[0] == 10
    assert score_reviews({"reviews": {"count": 780}})[0] < 10
    # lower the floor -> 780 now lands in the full band
    overriding = load_scoring_config()
    overriding["thresholds"]["reviews"]["full_count"] = 780
    set_config(overriding)
    val, issues = score_reviews({"reviews": {"count": 780}})
    assert val == 10 and issues == []


def test_grade_bands_are_configurable():
    overriding = load_scoring_config()
    overriding["grades"] = {"A": 60, "B": 40, "C": 20}
    set_config(overriding)
    assert score_product(by_aurora())["grade"] == "B"  # 54 clears B under looser bands


def test_revision_severity_and_fix_types_carry():
    s = score_product(by_aurora())
    assert s["issues"], "fix expected"
    for i in s["issues"]:
        assert {"factor", "text", "severity", "fix_type", "evidence"} <= set(i.keys())
        assert i["severity"] in ("high", "medium", "low")
        assert i["fix_type"]


def by_aurora():
    from app.mock_data import MOCK_CATALOG

    return next(p for p in MOCK_CATALOG if p["id"] == 111)


def test_missing_yaml_file_falls_back_to_embedded_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("GEO_SCORING_CONFIG", str(tmp_path / "nope.yaml"))
    cfg = load_scoring_config(force=True)
    assert cfg["meta"]["resolved_from"] == "embedded-fallback"
    assert cfg["thresholds"]["reviews"]["full_count"] == 787


def test_broken_yaml_falls_back(tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed", encoding="utf-8")
    monkeypatch.setenv("GEO_SCORING_CONFIG", str(bad))
    cfg = load_scoring_config(force=True)
    assert cfg["meta"]["resolved_from"] == "embedded-fallback"
