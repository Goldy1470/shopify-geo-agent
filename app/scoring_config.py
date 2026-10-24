"""Loading and overriding of the GEO scoring configuration (app/scoring.yaml).

Design rules:
- YAML on disk is the source of truth; embedded defaults are the fallback so a
  broken file can never take the app down (severity: silent degrade, logged).
- Configuration is process-cached; tests can inject a dict via set_config().
- meta.resolved_from records which file (or fallback) actually served, and
  meta.source/last_reviewed carry heuristic provenance.
"""

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("scoring.config")

DEFAULT_PATH = Path(__file__).with_name("scoring.yaml")

EMBEDDED_DEFAULTS: dict[str, Any] = {
    "meta": {
        "name": "geo-scoring",
        "version": 2,
        "source": "embedded defaults (scoring.yaml missing or unreadable)",
        "notes": "",
    },
    "factors": {
        "title": {"weight": 20},
        "description": {"weight": 25},
        "faq": {"weight": 15},
        "media": {"weight": 15},
        "attributes": {"weight": 15},
        "reviews": {"weight": 10},
    },
    "grades": {"A": 80, "B": 60, "C": 40},
    "thresholds": {
        "title": {
            "min_length": 20,
            "max_length": 70,
            "hard_max": 90,
            "stuffing_segments": 5,
            "stuffed_floor_score": 4,
            "short_penalty": 9,
            "long_penalty": 4,
            "no_type_penalty": 4,
            "type_not_in_title_penalty": 2,
            "no_use_case_penalty": 5,
            "all_caps_penalty": 4,
        },
        "description": {
            "thin_words": 60,
            "thin_penalty": 12,
            "short_words": 100,
            "short_penalty": 4,
            "topic_penalty_each": 3,
            "topic_penalty_cap": 10,
            "no_list_penalty": 2,
            "topic_words": {
                "material": ["material", "fabric", "nylon", "polyester", "leather", "laminate", "canvas"],
                "dimensions": ["dimension", "size", "liter", "litre", "oz", "ounce", "capacity", "wide", "tall", "kg", "lb"],
                "compatibility": ["compat", "fits", "carry-on", "airline", "laptop", "device"],
                "intended_use": ["designed for", "ideal for", "perfect for", "made for", "built for", "use", "commuting", "travel"],
                "limitations": ["not for", "not designed", "do not", "avoid", "limitation", "warning"],
            },
        },
        "faq": {"full_count": 3, "partial_score": 9},
        "media": {
            "full_images": 5,
            "few_images_penalty": 2,
            "single_image_penalty": 7,
            "no_images_penalty": 15,
            "missing_alt_penalty": 3,
            "thin_alt_chars": 25,
            "thin_alt_penalty": 2,
            "no_video_penalty": 3,
        },
        "attributes": {
            "keys": ["material", "dimensions", "compatibility", "intended_use", "limitations"],
            "flat_missing_count": 4,
            "flat_missing_penalty": 2,
        },
        "reviews": {
            "full_count": 787,
            "full_score": 10,
            "strong_count": 352,
            "strong_score": 7,
            "growing_count": 50,
            "growing_score": 4,
            "first_review_score": 2,
        },
    },
    "use_case_words": [
        "travel", "commut", "hik", "beach", "gym", "weekend", "rain", "hiking",
        "camping", "work", "school", "carrying", "everyday", "daily",
    ],
    "attribute_keys": ["material", "dimensions", "compatibility", "intended_use", "limitations"],
}

_cache: dict[str, Any] | None = None


def _merge(base: dict, overrides: dict) -> dict:
    """Deep-merge YAML data over defaults (dicts merge; lists replace)."""
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("Falling back to embedded scoring defaults (%s: %s)", path, exc)
        return None


def load_scoring_config(force: bool = False) -> dict:
    """Cached merged config; records the resolved source under meta.resolved_from."""
    global _cache
    if _cache is not None and not force:
        return _cache
    path = os.environ.get("GEO_SCORING_CONFIG", "") or str(DEFAULT_PATH)
    data = _load_file(path)
    if data is None:
        merged = copy.deepcopy(EMBEDDED_DEFAULTS)
        resolved = "embedded-fallback"
    else:
        merged = _merge(copy.deepcopy(EMBEDDED_DEFAULTS), data)
        resolved = path
    merged.setdefault("meta", {})
    merged["meta"]["resolved_from"] = resolved
    _cache = merged
    return merged


def set_config(config: dict) -> None:
    """Test hook: install an exact config dict (deep-copied)."""
    global _cache
    _cache = copy.deepcopy(config)


def reset_config() -> None:
    """Clear the cache (next load re-reads the file)."""
    global _cache
    _cache = None


def config_provenance() -> dict:
    cfg = load_scoring_config()
    return {
        "source": cfg.get("meta", {}).get("source", ""),
        "last_reviewed": cfg.get("meta", {}).get("last_reviewed", ""),
        "resolved_from": cfg.get("meta", {}).get("resolved_from", ""),
    }
