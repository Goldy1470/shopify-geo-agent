"""SUGGEST engine (Phase 4).

Composes every actionable suggestion per product from the Phase 2-3 data:
scoring issues (structured fix_types), readiness checks, the fixer's grounded
drafts, and manual / missing-data tasks. Fix kinds stay compatible with the
apply plumbing (fixer's title_rewrite / faq_generate / description_append)
and extend it with new kinds (alt_text, seo_fill_title, seo_fill_description,
metafield_fill, taxonomy_fill, gtin_fill, variant_fill, price_fix, media_add,
review_nudge, jsonld_snippet).

Hard rules (validators, not convention):
- a suggested title never removes or replaces the product's own name;
- generated text only reuses data present on the product - a fact with no
  source in the catalog becomes a missing-data task instead;
- no placeholders, no ALL-CAPS or 5+-segment "stuffing", field limits hold;
- HTML additions are tag-whitelisted (ul/li/h3/strong/em/div only);
- no duplicate pending suggestion for the same product + kind + field.

Engine modes:
- rules/templates (default, deterministic),
- optional LLM-judged refinement through the same provider interface
  (cfg "suggest_llm_refine" + OPENAI key): the model may re-draft a top-impact
  proposal, but the revision must pass the SAME validators or it is ignored.

Impact score = visibility gap x revenue weight x fix confidence:
- visibility gap: (factor weight - factor score) / weight, or the readiness
  severity proxy for kinds without a scored factor;
- revenue weight: product price vs. the catalog's priciest product
  (missing price = neutral 0.6, never invented);
- fix confidence: 1.0 grounded drafts, 0.9 seo fill, 0.6 alt text,
  0.5 JSON-LD snippet, 0.3 manual/missing-data tasks.
"""

import asyncio
import concurrent.futures
import html
import json
import os
import re
from datetime import UTC, datetime

import httpx

from . import fixer
from .config import load_config
from .db import execute, query
from .providers.analysis import AnalysisClient, AnalysisFailed  # same provider interface
from .readiness import product_readiness
from .scoring import WEIGHTS, score_product

# --- validators ----------------------------------------------------------------

TITLE_MAX = 78            # fixer draft budget; scoring prefers <= 70
SEO_TITLE_MAX = 70        # Shopify SEO title budget
ALT_MAX = 125             # screen-reader budget
SEO_DESC_MAX = 320        # Shopify SEO description budget
ALLOWED_TAGS = {"ul", "li", "h3", "strong", "em", "div"}
TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)\b")
DANGEROUS_TAG_RE = re.compile(r"<\s*(script|iframe|img|link|style|object)\b", re.I)
EVENT_HANDLER_RE = re.compile(r"\son[a-z]+\s*=", re.I)
STUFF_RE = re.compile(r"\s[-|]\s")
LI_FACT_RE = re.compile(r"<li><strong>(.*?)</strong>\s*(.*?)</li>", re.S)
TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.,'’/+&-][a-z0-9]+)*", re.I)
GEO_BLOCK_RE = re.compile(
    re.escape(fixer.GEO_BLOCK_START) + r"(.*?)" + re.escape(fixer.GEO_BLOCK_END), re.S
)

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "can", "could",
    "do", "does", "for", "from", "had", "has", "have", "if", "in", "into", "is",
    "it", "its", "may", "might", "must", "not", "of", "on", "or", "our", "so",
    "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "to", "was", "we", "were", "what", "when", "where", "which", "who",
    "why", "will", "with", "you", "your",
})
# Words the deterministic FAQ templates add around the grounded fact. They are
# allowed even when the exact word is not in the product data; everything else
# in an FAQ answer must trace back to the catalog.
TEMPLATE_WORDS = frozenset({
    "answer", "compatible", "designed", "dimensions", "image", "made", "material",
    "precautions", "product", "question",
})


def name_preserved_problems(before: str, after: str) -> list[str]:
    b = (before or "").strip()
    a = (after or "").strip()
    if not a:
        return ["empty value"]
    if b and b.lower() not in a.lower():
        return ["suggested text drops the product's own name"]
    return []


def caps_problems(text: str) -> list[str]:
    problems = []
    if len(STUFF_RE.findall(text or "")) >= 5:
        problems.append("reads as keyword-stuffed (5+ - or | segments)")
    if text and text.strip() and text.strip().upper() == text.strip():
        problems.append("is in ALL CAPS")
    return problems


def placeholder_problems(text: str) -> list[str]:
    return fixer.validate_no_placeholders(text)


def html_safe_problems(text: str) -> list[str]:
    """Raw additions must be tag-whitelisted; dangerous or unknown tags refused."""
    problems = []
    if DANGEROUS_TAG_RE.search(text or ""):
        problems.append("contains a forbidden tag (script/iframe/img/link/style)")
    if EVENT_HANDLER_RE.search(text or "") or re.search(r"javascript\s*:", text or "", re.I):
        problems.append("contains executable markup (event handler or javascript: URL)")
    for tag in TAG_RE.findall(text or ""):
        if tag.lower() not in ALLOWED_TAGS:
            problems.append(f"unapproved tag <{tag}> in HTML addition")
    return problems


def alt_text_problems(after: str, product: dict) -> list[str]:
    """Alt text may only reuse the product title + image position/count.

    It must contain the product's own title (so screen readers hear the real name)
    and follow the deterministic "<title> - product image N of M" shape. No new
    materials, colors, claims or invented descriptions.
    """
    problems: list[str] = []
    title = (product.get("title") or "").strip()
    if not title:
        return ["product has no title to ground alt text"]
    if title.lower() not in (after or "").lower():
        problems.append("alt text does not include the product title")
    if not re.search(r"product image\s+\d+\s+of\s+\d+", after or "", re.I):
        problems.append("alt text is not the grounded '<title> - product image N of M' pattern")
    problems += claim_problems(after, _corpus(product))
    return problems


def _corpus(product: dict) -> str:
    mf = product.get("metafields") or {}
    mf_text = " ".join(str(v) for v in mf.values() if v)
    variants_text = " ".join(
        f"{v.get('sku') or ''} {v.get('title') or ''} {v.get('barcode') or ''}"
        for v in (product.get("variants") or [])
    )
    faq_text = " ".join(f"{f.get('q') or ''} {f.get('a') or ''}" for f in (product.get("faq") or []))
    images_text = " ".join((i.get("alt") or "") for i in (product.get("images") or []))
    body = product.get("body_html") or ""
    plain_body = " ".join(re.sub(r"<[^>]+>", " ", body).split())
    return " ".join(
        (
            product.get("title") or "",
            product.get("product_type") or "",
            product.get("vendor") or "",
            product.get("category") or "",
            " ".join(product.get("tags") or []),
            body,
            html.unescape(body),
            plain_body,
            mf_text,
            variants_text,
            faq_text,
            images_text,
        )
    )


def trace_problems(values: list[str], corpus: str) -> list[str]:
    """Every published fact chunk must exist verbatim (case-insensitive) in the product data."""
    low_corpus = corpus.lower()
    problems = []
    for v in values:
        if v.strip() and v.strip().lower() not in low_corpus:
            problems.append(f"untraceable claim: {v[:60]!r}")
    return problems


def _tokens(text: str):
    for raw in TOKEN_RE.findall((text or "").lower()):
        token = raw.strip(".,'’/+&-")
        if len(token) < 2 or token in STOPWORDS or token in TEMPLATE_WORDS:
            continue
        yield token


def claim_problems(text: str, corpus: str) -> list[str]:
    """Token-level tracing for template sentences (FAQ answers).

    Template glue words are ignored; every remaining content token must occur
    in the product data. This catches "made of leather" when the catalog only
    says nylon while still allowing "It is made of 600D nylon."
    """
    low = (corpus or "").lower()
    missing = sorted({token for token in _tokens(text) if token not in low})
    if missing:
        return [f"untraceable claim token(s): {', '.join(missing[:6])}"]
    return []


def truncate_words(text: str, limit: int) -> str:
    """Trim to ``limit`` on a sentence/word boundary (never mid-word if possible)."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if ". " in cut:
        sentence = cut.rsplit(". ", 1)[0].strip()
        if len(sentence) >= 80:
            return sentence
    word = cut.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return word or cut


def _description_block_problems(after: str, product: dict) -> list[str]:
    match = GEO_BLOCK_RE.search(after or "")
    if not match:
        return ["description draft is missing the geo-agent specs block markers"]
    block = match.group(1)
    problems = html_safe_problems(block)
    problems += placeholder_problems(html.unescape(block))
    values = [html.unescape(value).strip() for _key, value in LI_FACT_RE.findall(block)]
    problems += trace_problems(values, _corpus(product))
    return problems


def _faq_problems(after: str, product: dict) -> list[str]:
    try:
        items = json.loads(after or "")
    except ValueError:
        return ["FAQ draft is not valid JSON"]
    if not isinstance(items, list) or not items:
        return ["FAQ draft must be a non-empty JSON list of {q, a} objects"]
    problems: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            problems.append(f"FAQ item {index} is not an object")
            continue
        question = str(item.get("q") or "").strip()
        answer = str(item.get("a") or "").strip()
        if not question or not answer:
            problems.append(f"FAQ item {index} needs non-empty q and a")
            continue
        problems += placeholder_problems(question)
        problems += placeholder_problems(answer)
        problems += caps_problems(question)
        problems += caps_problems(answer)
        if re.search(r"<[a-zA-Z/]", answer):
            problems.append(f"FAQ item {index} answer must be plain text (no HTML tags)")
        problems += claim_problems(answer, _corpus(product))
    return problems


def validate_suggestion(s: dict, product: dict) -> list[str]:
    """Returns validator problems; empty list = the draft may enter the queue."""
    kind, after = s.get("kind", ""), (s.get("after") or "")
    before = s.get("before") or ""

    if kind == "jsonld_snippet":
        # Liquid braces and the JSON-LD <script> wrapper are intentional here.
        low = after.lower()
        problems = []
        if not after.strip():
            return ["empty value"]
        if "<script" not in low or "application/ld+json" not in low:
            problems.append("JSON-LD snippet must be a script application/ld+json block")
        for token in ("product.title", "product.available", "geo_variant.price", "geo_currency"):
            if token not in after:
                problems.append(f"JSON-LD snippet must read {token} at render time")
        if re.search(r'"priceCurrency"\s*:\s*"', after):
            problems.append("JSON-LD snippet must not hardcode a currency")
        return problems

    if kind == "description_append":
        return _description_block_problems(after, product)

    if kind == "faq_generate":
        return _faq_problems(after, product)

    problems = placeholder_problems(after)
    if kind in ("title_rewrite", "seo_fill_title"):
        if kind == "seo_fill_title":
            title = (product.get("title") or "").strip()
            if title and title.lower() not in after.lower():
                problems.append("SEO title drops the product name")
        problems += name_preserved_problems(before, after)
        problems += caps_problems(after)
        limit = SEO_TITLE_MAX if kind == "seo_fill_title" else TITLE_MAX
        if len(after) > limit:
            problems.append(f"title draft exceeds {limit} chars")
    if kind == "seo_fill_description":
        if len(after) > SEO_DESC_MAX:
            problems.append(f"seo description draft exceeds {SEO_DESC_MAX} chars")
        if re.search(r"<[a-zA-Z/]", after):
            problems.append("SEO description must be plain text (no HTML tags)")
        problems += trace_problems([after], _corpus(product))
    if kind == "alt_text":
        problems += alt_text_problems(after, product)
        if len(after) > ALT_MAX:
            problems.append(f"alt draft exceeds {ALT_MAX} chars")
        problems += caps_problems(after)
    return problems


# --- impact ---------------------------------------------------------------

FACTOR_BY_KIND = {
    "title_rewrite": "title",
    "faq_generate": "faq",
    "faq": "faq",
    "description_append": "description",
    "description_section": "description",
    "alt_text": "media",
    "media_add": "media",
    "metafield_fill": "attributes",
    "review_nudge": "reviews",
}
KIND_CONFIDENCE = {
    "title_rewrite": 1.0,
    "faq_generate": 1.0,
    "description_append": 1.0,
    "seo_fill_title": 0.9,
    "seo_fill_description": 0.9,
    "alt_text": 0.6,
    "jsonld_snippet": 0.5,
    "metafield_fill": 0.3,
    "taxonomy_fill": 0.3,
    "gtin_fill": 0.3,
    "variant_fill": 0.3,
    "price_fix": 0.3,
    "media_add": 0.3,
    "review_nudge": 0.3,
    "faq": 0.3,
    "description_section": 0.3,
}
READINESS_GAP = {"missing": 0.6, "warning": 0.3, "ready": 0.0, "unchecked": 0.4}


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def product_price(product: dict) -> float | None:
    """Highest known price (product range first, variant prices as fallback)."""
    price = _to_float(product.get("price_max"))
    if price and price > 0:
        return price
    prices = [_to_float(v.get("price")) for v in (product.get("variants") or [])]
    valid = [p for p in prices if p and p > 0]
    return max(valid) if valid else None


def catalog_price_ceiling(products: list[dict]) -> float | None:
    prices = [product_price(p) for p in products]
    return max((p for p in prices if p), default=None)


def _gap_norm_for_scored_kind(kind: str, breakdown: dict) -> float | None:
    factor = FACTOR_BY_KIND.get(kind)
    if factor is None:
        return None
    weight = WEIGHTS.get(factor, 0)
    deficit = max(0, weight - breakdown.get(factor, 0))
    return deficit / weight if weight else 0.0


def impact_score(kind: str, breakdown: dict, readiness_status: str | None, price: float | None,
                 price_ceiling: float | None) -> int:
    """impact = visibility gap x revenue weight x fix confidence (0..100)."""
    gap = _gap_norm_for_scored_kind(kind, breakdown)
    if gap is None:
        gap = READINESS_GAP.get(readiness_status or "", 0.4)
    confidence = KIND_CONFIDENCE.get(kind, 0.3)
    weight = (
        min(1.0, max(0.15, price / price_ceiling)) if price and price_ceiling else 0.6
    )
    return max(0, min(100, round(100 * gap * weight * confidence)))


# --- suggestion builders ----------------------------------------------------


def _task(product_id: str, kind: str, field: str, note: str, severity: str, product: dict,
          breakdown: dict, readiness_status: str | None, ceiling: float | None) -> dict:
    return {
        "product_id": product_id,
        "kind": kind,
        "field": field,
        "before": "",
        "after": "",
        "severity": severity,
        "auto": 0,
        "notes": note,
        "impact": impact_score(
            kind, breakdown, readiness_status, product_price(product), ceiling
        ),
    }


def _severity_for(scored: dict, fix_type: str, default: str = "medium") -> str:
    rank = {"low": 0, "medium": 1, "high": 2}
    severities = [i["severity"] for i in scored["issues"] if i["fix_type"] == fix_type]
    return max(severities, key=lambda s: rank.get(s, 1)) if severities else default


def suggest_for_product(p: dict, readiness: dict | None = None,
                        price_ceiling: float | None = None) -> list[dict]:
    """Every grounded suggestion for one product; validated + impact-scored."""
    scored = score_product(p)
    readiness = readiness or product_readiness(p)
    check_by_name = {c["check"]: c for c in readiness["checks"]}
    checks = {name: c["status"] for name, c in check_by_name.items()}
    pid = str(p["id"])
    breakdown = scored["breakdown"]
    price = product_price(p)
    out: list[dict] = []

    def rows_for(kind: str, draft: dict | None, severity: str, note: str = "") -> None:
        if draft is None:
            return
        s = {
            "product_id": pid,
            "kind": kind,
            "field": draft.get("field"),
            "before": draft.get("before", ""),
            "after": draft.get("after", ""),
            "severity": severity,
            "auto": 1,
            "notes": note,
            "impact": impact_score(kind, breakdown, None, price, price_ceiling),
        }
        if not validate_suggestion(s, p):
            out.append(s)

    # 1. title (extend / reorder only - the name always survives)
    title_issue = next((i for i in scored["issues"] if i["factor"] == "title"), None)
    if title_issue:
        draft = fixer.draft_title_fix(p)
        if draft:
            rows_for("title_rewrite", draft, title_issue["severity"],
                     "Name preserved; only the type/use-case context is appended.")
        else:
            note = (
                "Title needs a rewrite (" + title_issue["text"] + ") - reorder or re-extend by "
                "hand: the product name must stay intact. Nothing was auto-drafted."
            )
            out.append(_task(
                pid, "title_rewrite", "title", note, title_issue["severity"], p, breakdown,
                None, price_ceiling,
            ))
    if not (p.get("product_type") or "").strip():
        out.append(_task(
            pid, "metafield_fill", "product_type",
            "Set the Shopify product type/category word. It anchors which prompts an agent "
            "considers relevant and is required before a title extension can be drafted.",
            "medium", p, breakdown, "missing", price_ceiling,
        ))

    # 2. FAQ (grounded in metafields only)
    faq_issue = next((i for i in scored["issues"] if i["factor"] == "faq"), None)
    if faq_issue:
        draft = fixer.draft_faq_fix(p)
        if draft:
            rows_for("faq_generate", draft, faq_issue["severity"],
                     "Answers reuse the product's own metafield values; no new claims.")
        else:
            out.append(_task(
                pid, "faq", "faq",
                "FAQ gap. Collect 3 customer questions (sizing, care, compatibility) and answer "
                "them on the page from verified product data.",
                faq_issue["severity"], p, breakdown, None, price_ceiling,
            ))

    # 3. description specs block
    desc_issue = next((i for i in scored["issues"] if i["factor"] == "description"), None)
    if desc_issue:
        draft = fixer.draft_description_fix(p)
        if draft:
            rows_for("description_append", draft, desc_issue["severity"],
                     "Marker-delimited specs block; re-drafting replaces it instead of duplicating.")
        else:
            out.append(_task(
                pid, "description_section", "body_html",
                "Description needs work (" + desc_issue["text"] + "). Draft the missing sections "
                "from supplier sheets; nothing was invented.",
                desc_issue["severity"], p, breakdown, None, price_ceiling,
            ))

    # 4. alt text (image position + count only - nothing invented)
    images = p.get("images") or []
    plain = [(index, img) for index, img in enumerate(images) if not img.get("is_video")]
    missing_alt = [(index, img) for index, img in plain if not (img.get("alt") or "").strip()]
    if missing_alt:
        title = (p.get("title") or "").strip()
        total = len(plain)
        index, img = missing_alt[0]
        position = int(img.get("position") or index + 1)
        order = next(pos for pos, (_i, candidate) in enumerate(plain, start=1) if candidate is img)
        after = f"{title} — product image {order} of {total}"
        s = {
            "product_id": pid, "kind": "alt_text",
            "field": f"image:{position}",
            "before": "", "after": after,
            "severity": _severity_for(scored, "alt_text"),
            "auto": 1,
            "notes": f"Applies to {len(missing_alt)} image(s); further alts follow the same pattern.",
            "impact": impact_score("alt_text", breakdown, None, price, price_ceiling),
        }
        if not validate_suggestion(s, p):
            out.append(s)

    # 5. SEO metafields (title = the product's own name; description = traced prose)
    title = (p.get("title") or "").strip()
    if not (p.get("seo_title") or "").strip() and title:
        if len(title) <= SEO_TITLE_MAX:
            s = {
                "product_id": pid, "kind": "seo_fill_title", "field": "seo_title",
                "before": "", "after": title,
                "severity": "medium", "auto": 1,
                "notes": "SEO title = the product's own name (name never removed).",
                "impact": impact_score(
                    "seo_fill_title", breakdown, checks.get("seo", "missing"), price, price_ceiling
                ),
            }
            if not validate_suggestion(s, p):
                out.append(s)
        else:
            out.append(_task(
                pid, "seo_fill_title", "seo_title",
                f"SEO title is missing and the product name is {len(title)} chars. Write a "
                f"<={SEO_TITLE_MAX}-char SEO title without dropping the name.",
                "medium", p, breakdown, checks.get("seo", "missing"), price_ceiling,
            ))
    if not (p.get("seo_description") or "").strip():
        plain_body = " ".join(re.sub(r"<[^>]+>", " ", p.get("body_html") or "").split())
        if plain_body:
            after = truncate_words(plain_body, SEO_DESC_MAX)
            s = {
                "product_id": pid, "kind": "seo_fill_description", "field": "seo_description",
                "before": "", "after": after,
                "severity": "medium", "auto": 1,
                "notes": "First prose of the description, trimmed on a word boundary.",
                "impact": impact_score(
                    "seo_fill_description", breakdown, checks.get("seo", "missing"),
                    price, price_ceiling,
                ),
            }
            if not validate_suggestion(s, p):
                out.append(s)
            else:
                out.append(_task(
                    pid, "seo_fill_description", "seo_description",
                    "Write a 1-2 sentence SEO description from verified product data; the "
                    "existing prose could not be traced verbatim.",
                    "medium", p, breakdown, checks.get("seo", "missing"), price_ceiling,
                ))
        else:
            out.append(_task(
                pid, "seo_fill_description", "seo_description",
                "Description is empty. Write a 1-2 sentence SEO description from verified specs.",
                "medium", p, breakdown, checks.get("seo", "missing"), price_ceiling,
            ))

    # 6-13. manual / missing-data tasks (grounded gaps, nothing invented)
    missing_attrs = scored.get("att_missing") or []
    if missing_attrs:
        out.append(_task(
            pid, "metafield_fill", "metafields",
            "Collect attribute metafields (from supplier/manufacturer data): "
            + ", ".join(missing_attrs)
            + ". These feed the comparison step agents run against a shopper's needs.",
            _severity_for(scored, "metafield_fill"), p, breakdown, "missing", price_ceiling,
        ))
    taxonomy = check_by_name.get("taxonomy")
    if taxonomy and taxonomy["status"] == "missing":
        out.append(_task(
            pid, "taxonomy_fill", "category",
            "Assign the canonical Shopify taxonomy category (Products > category). Agents "
            "filter and compare by it.",
            taxonomy["severity"], p, breakdown, "missing", price_ceiling,
        ))
    gtin = check_by_name.get("gtin")
    if gtin and gtin["status"] == "warning":
        out.append(_task(
            pid, "gtin_fill", "barcode",
            "Add GTIN/_barcode per variant (from the supplier; sweep-scan the pack). Buyable "
            "listings on agent surfaces need it.",
            gtin["severity"], p, breakdown, "warning", price_ceiling,
        ))
    variants_check = check_by_name.get("variants")
    if variants_check and variants_check["status"] == "warning":
        out.append(_task(
            pid, "variant_fill", "variants",
            "Complete variant data (SKU + price per variant) so agent carts and checkout "
            "resolve correctly.",
            variants_check["severity"], p, breakdown, "warning", price_ceiling,
        ))
    price_check = check_by_name.get("price")
    if price_check and price_check["status"] in ("warning", "missing"):
        out.append(_task(
            pid, "price_fix", "price",
            "Fix the price in Shopify admin (missing or inverted min/max blocks agent buy "
            "surfaces).",
            price_check["severity"], p, breakdown, price_check["status"], price_ceiling,
        ))
    availability = check_by_name.get("availability")
    if availability and availability["status"] == "warning":
        out.append(_task(
            pid, "price_fix", "availability",
            "Every variant reads unavailable - restock or flip availability; agents skip "
            "sold-out listings.",
            availability["severity"], p, breakdown, "warning", price_ceiling,
        ))
    if not images:
        out.append(_task(
            pid, "media_add", "images",
            "No images on record. Upload 5+ (front, side, detail, in use, scale) before "
            "anything else.",
            "high", p, breakdown, "missing", price_ceiling,
        ))
    elif not any(img.get("is_video") for img in images):
        out.append(_task(
            pid, "media_add", "video",
            "No video embedded. Add a short feature/use-case clip; video is consistently "
            "present on top-ranked PDPs.",
            _severity_for(scored, "media_add", "low"), p, breakdown, None, price_ceiling,
        ))
    review_count = int((p.get("reviews") or {}).get("count") or 0)
    if review_count == 0:
        out.append(_task(
            pid, "review_nudge", "reviews",
            "Enable review requests (post-purchase email/SMS) and hold a launch giveaway to "
            "seed reviews - review volume is a decisive ranking proof signal.",
            "high", p, breakdown, "missing", price_ceiling,
        ))
    elif review_count < 352:
        out.append(_task(
            pid, "review_nudge", "reviews",
            "Keep collecting reviews: bottom-ranked median is 352, top-ranked is 787.",
            "low", p, breakdown, "warning", price_ceiling,
        ))

    # 14. JSON-LD snippet (suggested Liquid, never auto-installed)
    jsonld = check_by_name.get("jsonld")
    if jsonld and jsonld["status"] in ("missing", "warning", "unchecked"):
        snippet = jsonld_snippet(p)
        s = {
            "product_id": pid, "kind": "jsonld_snippet", "field": "theme_snippet",
            "before": "", "after": snippet, "severity": jsonld["severity"], "auto": 0,
            "notes": "Suggested theme snippet (snippets/geo-json-ld.liquid). Never "
                     "auto-installed: review, add, and render it in theme.liquid <head> yourself.",
            "impact": impact_score(
                "jsonld_snippet", breakdown, jsonld["status"], price, price_ceiling
            ),
        }
        if not validate_suggestion(s, p):
            out.append(s)

    # One suggestion per product + kind + field; insertion dedupes across runs.
    unique: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for suggestion in out:
        key = (suggestion["kind"], suggestion.get("field") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(suggestion)
    return unique


def jsonld_snippet(_product: dict) -> str:
    """Product JSON-LD as a Liquid snippet, sourced from store data only.

    Every value is read at render time (title, images, SKU/GTIN, price,
    currency, availability), so the snippet cannot invent a fact and never
    hardcodes USD/InStock. It is suggested, never auto-installed.
    """
    return """{%- assign geo_variant = product.selected_or_first_available_variant -%}
{%- assign geo_currency = cart.currency.iso_code | default: shop.currency -%}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": {{ product.title | json }},
  "url": {{ shop.url | append: product.url | json }},
  "description": {{ product.description | strip_html | truncate: 300 | json }},
  "brand": {"@type": "Brand", "name": {{ product.vendor | json }}},
  {%- if product.featured_image %}
  "image": [{% for image in product.images %}{{ image | image_url: width: 1200 | prepend: "https:" | json }}{% unless forloop.last %},{% endunless %}{% endfor %}],
  {%- endif %}
  {%- if geo_variant.sku != blank %}
  "sku": {{ geo_variant.sku | json }},
  {%- endif %}
  {%- if geo_variant.barcode != blank %}
  "gtin": {{ geo_variant.barcode | json }},
  {%- endif %}
  "offers": {
    "@type": "Offer",
    "price": {{ geo_variant.price | divided_by: 100.0 | json }},
    "priceCurrency": {{ geo_currency | json }},
    "availability": {% if product.available %}"https://schema.org/InStock"{% else %}"https://schema.org/OutOfStock"{% endif %},
    "url": {{ shop.url | append: product.url | json }}
  }
}
</script>"""


# --- queue write ------------------------------------------------------------


def _pending_exists(product_id: str, kind: str, field: str | None) -> bool:
    return bool(query(
        "SELECT id FROM fixes WHERE product_id=? AND kind=? AND COALESCE(field,'')=? AND status='pending'",
        (product_id, kind, field or ""),
    ))


def _insert_suggestion(s: dict) -> int | None:
    if _pending_exists(s["product_id"], s["kind"], s.get("field")):
        return None
    execute(
        "INSERT INTO fixes (product_id, kind, field, before, after, status, created_at, impact, "
        "severity, auto, notes) VALUES (?,?,?,?,?, 'pending', ?,?,?,?,?)",
        (
            s["product_id"], s["kind"], s.get("field"), s.get("before") or "", s.get("after") or "",
            datetime.now(UTC).isoformat(), s.get("impact"), s.get("severity"), s.get("auto"),
            s.get("notes") or "",
        ),
    )
    return int(query("SELECT last_insert_rowid() AS id")[0]["id"])


def _llm_refine_enabled(cfg: dict) -> bool:
    env = os.environ.get("SUGGEST_LLM_REFINE", "").strip().lower()
    return bool(cfg.get("suggest_llm_refine")) or env in {"1", "true", "yes", "on"}


def generate_suggestions(products: list[dict], cfg: dict | None = None) -> list[dict]:
    """Full SUGGEST pass for the catalog; returns only newly inserted suggestions."""
    cfg = cfg if cfg is not None else load_config()
    ceiling = catalog_price_ceiling(products)
    inserted: list[dict] = []
    for p in products:
        for s in suggest_for_product(p, price_ceiling=ceiling):
            suggestion_id = _insert_suggestion(s)
            if suggestion_id is not None:
                inserted.append({**s, "id": suggestion_id})
    if _llm_refine_enabled(cfg) and _api_key(cfg):
        inserted = llm_refine(inserted, products, cfg)
    return inserted


# --- LLM-judged refinement (same provider interface as Phase 3) --------------

JUDGE_SYSTEM_PROMPT = """You judge a suggested product-copy draft for a Shopify listing. \
Return STRICT JSON only: {"verdict": "approve" | "revise" | "reject", "reason": str, "revised": str|null}.
Rules: the product's own name must survive intact; only facts visible in the provided product \
data may appear (no invented materials, sizes, claims); no ALL CAPS, no keyword stuffing, no \
placeholders; keep revisions tight and factual. Reject when the draft invents facts."""


def _parse_judge_json(raw: str) -> dict:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise AnalysisFailed("judge output not JSON")
    try:
        out = json.loads(cleaned[start : end + 1])
    except ValueError:
        raise AnalysisFailed("judge output not JSON") from None
    if not isinstance(out, dict):
        raise AnalysisFailed("judge output is not a JSON object")
    verdict = str(out.get("verdict", "")).lower()
    if verdict not in ("approve", "revise", "reject"):
        raise AnalysisFailed("judge verdict unknown")
    return {
        "verdict": verdict,
        "reason": str(out.get("reason", ""))[:200],
        "revised": out.get("revised") if isinstance(out.get("revised"), str) else None,
    }


class SuggestJudge:
    """LLM judge over drafts (AnalysisClient pattern: T=0, JSON, one retry)."""

    def __init__(self, api_key: str) -> None:
        self._client = AnalysisClient(api_key)
        self._client.model = "gpt-5-mini"

    async def judge(self, draft: dict, corpus: str) -> dict:
        payload = {
            "model": self._client.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"Product data (the only allowed facts):\n{corpus[:4000]}\n\n"
                    f"Draft ({draft['kind']}):\n"
                    f"{json.dumps({k: draft.get(k) for k in ('before', 'after', 'field')})[:2000]}"
                )},
            ],
        }
        data = await self._client._call(payload)  # shared provider transport
        raw = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return _parse_judge_json(raw)


def _persist_suggestion(s: dict) -> None:
    if not s.get("id"):
        return
    execute(
        "UPDATE fixes SET after=?, judge=?, auto=?, notes=? WHERE id=?",
        (
            s.get("after") or "", s.get("judge"), s.get("auto"), s.get("notes") or "",
            s["id"],
        ),
    )


async def _refine_one(judge: SuggestJudge, s: dict, product: dict) -> dict:
    try:
        verdict = await judge.judge(s, _corpus(product))
    except (AnalysisFailed, httpx.HTTPError, ValueError) as exc:
        s["judge"] = json.dumps({"verdict": "unavailable", "reason": str(exc)[:120]})
        _persist_suggestion(s)
        return s
    if verdict["verdict"] == "reject":
        reason = verdict["reason"] or "no reason given"
        s["auto"] = 0
        s["notes"] = (s.get("notes") or "").strip() + f" LLM judge rejected this draft: {reason}"
        s["judge"] = json.dumps({"verdict": "reject", "reason": verdict["reason"]})
        _persist_suggestion(s)
    elif verdict["verdict"] == "revise" and verdict["revised"]:
        original = s["after"]
        revised = dict(s, after=verdict["revised"])
        problems = validate_suggestion(revised, product)
        if problems:
            s["judge"] = json.dumps({
                "verdict": "revision_invalid",
                "reason": "validator refused; rule draft kept",
                "problems": problems[:5],
            })
        else:
            s["after"] = revised["after"]
            s["judge"] = json.dumps({
                "verdict": "revise", "reason": verdict["reason"], "was": original[:80],
            })
        _persist_suggestion(s)
    else:
        s["judge"] = json.dumps({"verdict": "approve", "reason": verdict["reason"]})
        _persist_suggestion(s)
    return s


async def _refine_all(judge: SuggestJudge, targets: list[dict], by_pid: dict[str, dict]) -> None:
    for s in targets:
        product = by_pid.get(s.get("product_id") or "")
        if product is None:
            continue
        await _refine_one(judge, s, product)


def _run_sync(coro):
    """Run a coroutine from sync code, even when a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _api_key(cfg: dict) -> str:
    return (cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()


def llm_refine(suggestions: list[dict], products: list[dict], cfg: dict, limit: int = 5) -> list[dict]:
    """Judge top-impact drafts; revisions must pass the same validators."""
    if not _api_key(cfg) or not suggestions:
        return suggestions
    judge = SuggestJudge(_api_key(cfg))
    by_pid: dict[str, dict] = {str(p["id"]): p for p in products}
    targets = [
        s for s in sorted(suggestions, key=lambda item: -(item.get("impact") or 0))
        if s.get("auto") and (s.get("after") or "").strip()
    ][:limit]
    if targets:
        _run_sync(_refine_all(judge, targets, by_pid))
    return suggestions
