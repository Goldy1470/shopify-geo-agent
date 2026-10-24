"""Rules-based GEO scoring for Shopify products.

Factors and weights follow the ranking signals described for AI shopping
engines and personal agents: descriptive titles with use cases, PDPs that
spell out intended use / compatibility / dimensions / materials / limitations,
FAQs, media richness, review volume, and attribute completeness.
"""

import re

USE_CASE_WORDS = [
    "travel", "commut", "hik", "beach", "gym", "weekend", "rain", "hiking",
    "camping", "work", "school", "carrying", "everyday", "daily",
]

ATTRIBUTE_KEYS = ["material", "dimensions", "compatibility", "intended_use", "limitations"]

WEIGHTS = {"title": 20, "description": 25, "faq": 15, "media": 15, "attributes": 15, "reviews": 10}


def _text(p: dict) -> str:
    body = re.sub(r"<[^>]+>", " ", p.get("body_html") or "")
    return " ".join((body + " " + (p.get("faq_text") or "")).split())


def _clean_title(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def score_title(p: dict) -> tuple[int, list[str]]:
    issues = []
    t = _clean_title(p.get("title"))
    s = WEIGHTS["title"]
    tl = len(t)

    # Penalty: keyword-stuffed titles (5+ " - " or "|" segments read as spam).
    if len(re.findall(r"\s[-|]\s", t)) >= 5 or tl > 90:
        return 4, ["Title looks keyword-stuffed (Spam Pattern Notation). AI engines may distrust or truncate it."]

    lower = t.lower()
    has_use_case = any(w in lower for w in USE_CASE_WORDS)
    body = _text(p).lower()
    body_use = any(w in body for w in USE_CASE_WORDS)

    if tl < 20:
        s -= 9
        issues.append(f"Title is too short ({tl} chars). Aim for 20-70 characters incl. product type.")
    elif tl > 70:
        s -= 4
        issues.append(f"Title is long ({tl} chars); prefer <= 70 so engines don't truncate.")
    if not p.get("product_type"):
        s -= 4
        issues.append("No product_type set. The category word anchors what prompts an agent considers relevant.")
    elif p["product_type"].lower() not in lower:
        s -= 2
        issues.append("Title doesn't include the product type/category word.")
    if not has_use_case and not body_use:
        s -= 5
        issues.append("No use case mentioned anywhere (title/description). Personal agents match product rules to shopper profiles.")
    if t.isupper() or (t and t.upper() == t):
        s -= 4
        issues.append("Title in ALL CAPS.")
    return max(0, min(s, WEIGHTS["title"])), issues


def score_description(p: dict) -> tuple[int, list[str]]:
    issues = []
    text = _text(p)
    lowered = text.lower()
    s = WEIGHTS["description"]
    wc = len(text.split())

    if wc < 60:
        s -= 12
        issues.append(f"Description is thin ({wc} words). Agents need prose + specs to reason with.")
    elif wc < 100:
        s -= 4

    signal_topics = {
        "material": ["material", "fabric", "nylon", "polyester", "leather", "laminate", "canvas"],
        "dimensions": ["dimension", "size", "liter", "litre", "oz", "ounce", "capacity", "wide", "tall", "kg", "lb"],
        "compatibility": ["compat", "fits", "carry-on", "airline", "laptop", "device"],
        "intended_use": ["designed for", "ideal for", "perfect for", "made for", "built for", "use", "commuting", "travel"],
        "limitations": ["not for", "not designed", "do not", "avoid", "limitation", "warning"],
    }
    missing = []
    for topic, words in signal_topics.items():
        if not any(w in lowered for w in words):
            missing.append(topic)
    if missing:
        s -= min(10, 3 * len(missing))
        issues.append(
            "PDP doesn't clearly cover: " + ", ".join(missing)
            + ". Top PDPs state intended uses, compatibility, dimensions, materials, limitations explicitly."
        )

    # Checklists / specs help agents parse the page.
    if "<ul" not in (p.get("body_html") or "") and "-" not in text:
        s -= 2
        issues.append("Add a specs checklist (ul) - agents parse bullet specs more reliably than paragraphs.")
    return max(0, s), issues


def score_faq(p: dict) -> tuple[int, list[str]]:
    issues = []
    faqs = p.get("faq") or []
    if len(faqs) >= 3:
        return WEIGHTS["faq"], []
    if len(faqs) >= 1:
        return 9, ["Only " + str(len(faqs)) + " FAQ found. First-position PDPs have 3+ FAQs covering use cases, sizing, care."]
    return 0, ["No FAQ / Q&A content on the page. Ranked-first offers include FAQs, Q&As, images, and video consistently."]


def score_media(p: dict) -> tuple[int, list[str]]:
    issues = []
    images = [i for i in (p.get("images") or []) if not (i.get("src") or "").endswith(".mp4")]
    videos = [i for i in (p.get("images") or []) if (i.get("src") or "").endswith(".mp4")]
    missing_alt = [i for i in images if not (i.get("alt") or "").strip()]
    described_alt = [i for i in images if (i.get("alt") or "").strip() and len((i.get("alt") or "")) > 25]

    s = WEIGHTS["media"]
    if len(images) >= 5:
        s -= 0
    elif len(images) >= 3:
        s -= 2
        issues.append(f"Only {len(images)} images. Winning cards average 5+; add detail shots, on-model, use case.")
    elif images:
        s -= 7
        issues.append(f"Only {len(images)} image. Add detail / in-use imagery.")
    else:
        s -= 15
        issues.append("No images at all.")
    if missing_alt and images:
        s -= 3
        issues.append(f"{len(missing_alt)} of {len(images)} images have no alt text. Alt text is how agents describe your product images.")
    if not described_alt and images:
        s -= 2
        issues.append("Alt texts are thin. Describe what the image shows + the product's use.")
    if not videos:
        s -= 3
        issues.append("No video embedded. Video is consistently present on top-ranked PDPs.")
    return max(0, s), issues


def score_attributes(p: dict) -> tuple[int, list[str]]:
    issues = []
    mf = p.get("metafields") or {}
    missing = [k for k in ATTRIBUTE_KEYS if not (mf.get(k) or "").strip()]
    coverage = len(ATTRIBUTE_KEYS) - len(missing)
    s = WEIGHTS["attributes"]
    if missing:
        s -= int(WEIGHTS["attributes"] * (len(missing) / len(ATTRIBUTE_KEYS))) + (2 if len(missing) >= 4 else 0)
        issues.append("Missing attributes metafields: " + ", ".join(missing) + ". These feed the comparison agents run against a shopper's needs.")
    return max(0, s), issues


def score_reviews(p: dict) -> tuple[int, list[str]]:
    issues = []
    r = p.get("reviews") or {}
    count = int(r.get("count") or 0)
    if count >= 787:
        return WEIGHTS["reviews"], []
    if count >= 352:
        s = 7
        issues.append(f"{count} reviews. Bottom-ranked median is 352, top-ranked median is 787. Keep collecting.")
    elif count >= 50:
        s = 4
        issues.append(f"Only {count} reviews. Top-ranked cards have 787+ (median). Push review requests.")
    elif count >= 1:
        s = 2
        issues.append(f"Only {count} review. Reviews are a decisive proof signal.")
    else:
        s = 0
        issues.append("No reviews yet. ChatGPT Shopping weights review volume heavily.")
    return s, issues


def score_product(p: dict) -> dict:
    parts = {}
    all_issues = []
    for name, fn in [
        ("title", score_title), ("description", score_description), ("faq", score_faq),
        ("media", score_media), ("attributes", score_attributes), ("reviews", score_reviews),
    ]:
        val, issues = fn(p)
        # normalize to max weight (functions already cap)
        parts[name] = min(val, WEIGHTS[name])
        for i in issues:
            all_issues.append({"factor": name, "text": i})

    total = sum(min(v, WEIGHTS[k]) for k, v in parts.items())
    grade = "A" if total >= 80 else "B" if total >= 60 else "C" if total >= 40 else "D"
    quick_wins = [i for i in all_issues if i["factor"] in ("faq", "title")] if total < 60 else all_issues[:3]
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
        "att_missing": [k for k in ATTRIBUTE_KEYS if not ((p.get("metafields") or {}).get(k) or "").strip()],
    }
