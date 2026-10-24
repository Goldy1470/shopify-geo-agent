"""Rules-based GEO scoring for Shopify products (v2, config-driven).

Factors and weights follow the ranking signals described for AI shopping
engines and personal agents: descriptive titles with use cases, PDPs that
spell out intended use / compatibility / dimensions / materials / limitations,
FAQs, media richness, review volume, and attribute completeness.

All weights and thresholds live in app/scoring.yaml (checked into the repo so
tuning is reviewable); embedded defaults in scoring_config.py are the
fallback. Issue dicts carry factor / severity / evidence / fix_type so the
suggestion engine and UI can act on them directly.
"""

import re

from .scoring_config import load_scoring_config

_CFG = load_scoring_config()

WEIGHTS: dict[str, int] = {k: int(v.get("weight", 0)) for k, v in _CFG["factors"].items()}
USE_CASE_WORDS: list[str] = list(_CFG["use_case_words"])
ATTRIBUTE_KEYS: list[str] = list(_CFG["attribute_keys"])
GRADES: dict[str, int] = {k: int(v) for k, v in _CFG["grades"].items()}


def _issue(factor: str, text: str, severity: str, fix_type: str, evidence: dict | None = None) -> dict:
    return {
        "factor": factor,
        "text": text,
        "severity": severity,
        "fix_type": fix_type,
        "evidence": evidence or {},
    }


def _th(factor: str) -> dict:
    return load_scoring_config()["thresholds"][factor]


def _text(p: dict) -> str:
    body = re.sub(r"<[^>]+>", " ", p.get("body_html") or "")
    return " ".join((body + " " + (p.get("faq_text") or "")).split())


def _clean_title(t: str | None) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def score_title(p: dict) -> tuple[int, list[dict]]:
    th = _th("title")
    issues: list[dict] = []
    t = _clean_title(p.get("title"))
    s = WEIGHTS["title"]
    tl = len(t)

    # Penalty: keyword-stuffed titles (5+ " - " or "|" segments read as spam).
    if len(re.findall(r"\s[-|]\s", t)) >= th["stuffing_segments"] or tl > th["hard_max"]:
        return th["stuffed_floor_score"], [
            _issue(
                "title",
                "Title looks keyword-stuffed (Spam Pattern Notation). AI engines may distrust or truncate it.",
                "high",
                "title_rewrite",
                {"chars": tl, "segments": len(re.findall(r"\s[-|]\s", t))},
            )
        ]

    lower = t.lower()
    has_use_case = any(w in lower for w in USE_CASE_WORDS)
    body = _text(p).lower()
    body_use = any(w in body for w in USE_CASE_WORDS)

    if tl < th["min_length"]:
        s -= th["short_penalty"]
        issues.append(
            _issue(
                "title",
                f"Title is too short ({tl} chars). Aim for 20-70 characters incl. product type.",
                "high",
                "title_extend",
                {"chars": tl},
            )
        )
    elif tl > th["max_length"]:
        s -= th["long_penalty"]
        issues.append(
            _issue(
                "title",
                f"Title is long ({tl} chars); prefer <= 70 so engines don't truncate.",
                "low",
                "title_extend",
                {"chars": tl},
            )
        )
    if not p.get("product_type"):
        s -= th["no_type_penalty"]
        issues.append(
            _issue(
                "title",
                "No product_type set. The category word anchors what prompts an agent considers relevant.",
                "medium",
                "metafield_fill",
                {},
            )
        )
    elif p["product_type"].lower() not in lower:
        s -= th["type_not_in_title_penalty"]
        issues.append(
            _issue(
                "title",
                "Title doesn't include the product type/category word.",
                "medium",
                "title_extend",
                {"product_type": p.get("product_type")},
            )
        )
    if not has_use_case and not body_use:
        s -= th["no_use_case_penalty"]
        issues.append(
            _issue(
                "title",
                "No use case mentioned anywhere (title/description). Personal agents match product rules to shopper profiles.",
                "medium",
                "title_extend",
                {},
            )
        )
    if t.isupper() or (t and t.upper() == t):
        s -= th["all_caps_penalty"]
        issues.append(_issue("title", "Title in ALL CAPS.", "medium", "title_rewrite", {}))
    return max(0, min(s, WEIGHTS["title"])), issues


def score_description(p: dict) -> tuple[int, list[dict]]:
    th = _th("description")
    issues: list[dict] = []
    text = _text(p)
    lowered = text.lower()
    s = WEIGHTS["description"]
    wc = len(text.split())

    if wc < th["thin_words"]:
        s -= th["thin_penalty"]
        issues.append(
            _issue(
                "description",
                f"Description is thin ({wc} words). Agents need prose + specs to reason with.",
                "high",
                "description_section",
                {"word_count": wc},
            )
        )
    elif wc < th["short_words"]:
        s -= th["short_penalty"]

    signal_topics = th["topic_words"]
    missing = []
    for topic, words in signal_topics.items():
        if not any(w in lowered for w in words):
            missing.append(topic)
    if missing:
        s -= min(th["topic_penalty_cap"], th["topic_penalty_each"] * len(missing))
        issues.append(
            _issue(
                "description",
                "PDP doesn't clearly cover: " + ", ".join(missing)
                + ". Top PDPs state intended uses, compatibility, dimensions, materials, limitations explicitly.",
                "medium",
                "description_section",
                {"missing_topics": missing, "word_count": wc},
            )
        )

    # Checklists / specs help agents parse the page.
    if "<ul" not in (p.get("body_html") or "") and "-" not in text:
        s -= th["no_list_penalty"]
        issues.append(
            _issue(
                "description",
                "Add a specs checklist (ul) - agents parse bullet specs more reliably than paragraphs.",
                "low",
                "description_section",
                {},
            )
        )
    return max(0, s), issues


def score_faq(p: dict) -> tuple[int, list[dict]]:
    th = _th("faq")
    faqs = p.get("faq") or []
    if len(faqs) >= th["full_count"]:
        return WEIGHTS["faq"], []
    if len(faqs) >= 1:
        return th["partial_score"], [
            _issue(
                "faq",
                f"Only {len(faqs)} FAQ found. First-position PDPs have 3+ FAQs covering use cases, sizing, care.",
                "medium",
                "faq",
                {"faq_count": len(faqs)},
            )
        ]
    return 0, [
        _issue(
            "faq",
            "No FAQ / Q&A content on the page. Ranked-first offers include FAQs, Q&As, images, and video consistently.",
            "high",
            "faq",
            {"faq_count": 0},
        )
    ]


def score_media(p: dict) -> tuple[int, list[dict]]:
    th = _th("media")
    issues: list[dict] = []
    images = [i for i in (p.get("images") or []) if not (i.get("src") or "").endswith(".mp4") and not i.get("is_video")]
    videos = [i for i in (p.get("images") or []) if (i.get("src") or "").endswith(".mp4") or i.get("is_video")]
    missing_alt = [i for i in images if not (i.get("alt") or "").strip()]
    described_alt = [i for i in images if (i.get("alt") or "").strip() and len(i.get("alt") or "") > th["thin_alt_chars"]]

    s = WEIGHTS["media"]
    if len(images) >= th["full_images"]:
        s -= 0
    elif len(images) >= 3:
        s -= th["few_images_penalty"]
        issues.append(
            _issue(
                "media",
                f"Only {len(images)} images. Winning cards average 5+; add detail shots, on-model, use case.",
                "low",
                "media_add",
                {"images": len(images)},
            )
        )
    elif images:
        s -= th["single_image_penalty"]
        issues.append(
            _issue("media", f"Only {len(images)} image. Add detail / in-use imagery.", "medium", "media_add", {"images": len(images)})
        )
    else:
        s -= th["no_images_penalty"]
        issues.append(_issue("media", "No images at all.", "high", "media_add", {"images": 0}))
    if missing_alt and images:
        s -= th["missing_alt_penalty"]
        issues.append(
            _issue(
                "media",
                f"{len(missing_alt)} of {len(images)} images have no alt text. Alt text is how agents describe your product images.",
                "medium",
                "alt_text",
                {"missing_alt": len(missing_alt), "images": len(images)},
            )
        )
    if not described_alt and images:
        s -= th["thin_alt_penalty"]
        issues.append(
            _issue("media", "Alt texts are thin. Describe what the image shows + the product's use.", "low", "alt_text", {})
        )
    if not videos:
        s -= th["no_video_penalty"]
        issues.append(
            _issue("media", "No video embedded. Video is consistently present on top-ranked PDPs.", "low", "media_add", {})
        )
    return max(0, s), issues


def score_attributes(p: dict) -> tuple[int, list[dict]]:
    th = _th("attributes")
    issues: list[dict] = []
    mf = p.get("metafields") or {}
    missing = [k for k in th["keys"] if not (mf.get(k) or "").strip()]
    s = WEIGHTS["attributes"]
    if missing:
        s -= int(WEIGHTS["attributes"] * (len(missing) / len(th["keys"]))) + (th["flat_missing_penalty"] if len(missing) >= th["flat_missing_count"] else 0)
        issues.append(
            _issue(
                "attributes",
                "Missing attributes metafields: " + ", ".join(missing) + ". These feed the comparison agents run against a shopper's needs.",
                "medium",
                "metafield_fill",
                {"missing": list(missing)},
            )
        )
    return max(0, s), issues


def score_reviews(p: dict) -> tuple[int, list[dict]]:
    th = _th("reviews")
    r = p.get("reviews") or {}
    count = int(r.get("count") or 0)
    if count >= th["full_count"]:
        return th["full_score"], []
    if count >= th["strong_count"]:
        return th["strong_score"], [
            _issue(
                "reviews",
                f"{count} reviews. Bottom-ranked median is {th['strong_count']}, top-ranked median is {th['full_count']}. Keep collecting.",
                "low",
                "review_nudge",
                {"count": count},
            )
        ]
    if count >= th["growing_count"]:
        return th["growing_score"], [
            _issue(
                "reviews",
                f"Only {count} reviews. Top-ranked cards have {th['full_count']}+ (median). Push review requests.",
                "medium",
                "review_nudge",
                {"count": count},
            )
        ]
    if count >= 1:
        return th["first_review_score"], [
            _issue("reviews", f"Only {count} review. Reviews are a decisive proof signal.", "medium", "review_nudge", {"count": count})
        ]
    return 0, [
        _issue(
            "reviews",
            "No reviews yet. ChatGPT Shopping weights review volume heavily.",
            "high",
            "review_nudge",
            {"count": 0},
        )
    ]


def score_product(p: dict) -> dict:
    parts: dict[str, int] = {}
    all_issues: list[dict] = []
    for name, fn in [
        ("title", score_title), ("description", score_description), ("faq", score_faq),
        ("media", score_media), ("attributes", score_attributes), ("reviews", score_reviews),
    ]:
        val, issues = fn(p)
        # normalize to max weight (functions already cap)
        parts[name] = min(val, WEIGHTS[name])
        for i in issues:
            all_issues.append(i)

    total = sum(min(v, WEIGHTS[k]) for k, v in parts.items())
    grades = load_scoring_config()["grades"]
    grade = "A" if total >= grades["A"] else "B" if total >= grades["B"] else "C" if total >= grades["C"] else "D"
    mf = p.get("metafields") or {}
    return {
        "product_id": str(p["id"]),
        "title": p.get("title"),
        "handle": p.get("handle", ""),
        "score": total,
        "grade": grade,
        "breakdown": parts,
        "issues": all_issues,
        "image_count": len(p.get("images") or []),
        "review_count": int((p.get("reviews") or {}).get("count") or 0),
        "faq_count": len(p.get("faq") or []),
        "att_missing": [k for k in ATTRIBUTE_KEYS if not (mf.get(k) or "").strip()],
    }
