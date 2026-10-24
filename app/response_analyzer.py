"""Deterministic response analyzer for visibility runs.

Ported from OneGlanse's open-source GEO analysis methodology
(their analysisPrompt.ts -> rules computable without an LLM):

- Response quality gate (refusal / echo-only / substantive)
- Mention counting rules (echoes and URL-only mentions don't count)
- Absolute rank by reading order, not local list rank
- Sentiment decision tree with anti-inflation caps
- Visibility across 5 dimensions: coverage, placement, structure, frequency, framing
- Recommendation ladder: discouraged < mentioned_only < conditional < strong_alternative < top_pick
- GEO overall = 25% visibility + 25% rank + 25% sentiment + 25% recommendation (with caps)
- Competitor co-mentions, source/domain extraction, risk flags
"""

import re
from urllib.parse import urlparse

PROVIDER_OWNED_DOMAINS = [
    "chatgpt.com", "openai.com", "perplexity.ai", "gemini.google.com",
    "google.com", "claude.ai", "anthropic.com",
]

NEGATIVE_STRONG = [
    "avoid", "worst", "not recommended", "don't buy", "do not buy",
    "steer clear", "stay away", "skip", "beware", "scam",
]
DRAWBACK = [
    "however", "but ", "downside", "drawback", "limitation", "pricey",
    "expensive", "heavy", "flimsy", "leaks", "uncomfortable", "concern", "issue",
]
SUPERLATIVE = [
    "best", "excellent", "outstanding", "standout", "top pick",
    "top recommendation", "must-have", "must have", "perfect", "brilliant",
    "unbeatable", "#1", "winner", "clear leader",
]
POSITIVE = [
    "good", "solid", "popular", "reliable", "great", "well-made", "well made",
    "worth", "comfortable", "durable", "budget-friendly", "recommended",
    "choice", "pick", "quality",
]
CONDITIONAL = [
    "if you need", "if you want", "good for", "best for", "depending on",
    "small budgets", "for those", "only if", "budget option", "ideally",
]
CONTRASTIVE = ["unlike", "better than", "compared to", "compared with", "instead of"]

QUESTION_ONLY = re.compile(r"^\s*[-*\d.]*(?:\*\*)?[^.!?]*\?\s*$")

STOP_CAPS = {
    "the", "and", "for", "with", "that", "this", "have", "you", "best", "top",
    "also", "its", "our", "new", "all", "not", "but", "from", "pick", "products",
    "product", "shopping", "shop", "price", "prices", "budget", "under", "more",
    "get", "why", "what", "where", "when", "who", "how", "pro", "plus", "review",
    "reviews", "reddit", "amazon", "walmart", "target", "bestbuy", "klarna",
    "instagram", "shopify", "chatgpt", "openai", "google", "gemini", "claude",
    "perplexity", "copilot", "here", "there", "these", "those", "your", "their",
    "overall", "verbs", "excellent", "decent", "great", "solid", "reliable",
}

RETAILER_RE = re.compile(
    r"\b(Shopify|Amazon|Walmart|Target|Best Buy|Wayfair|Etsy|Klarna|Instacart|Shein|Temu)\b",
    re.I,
)


def _substantive_text(response_text: str) -> str:
    """Drop echoed questions / question-only lines so they are never scored as mentions."""
    keep = []
    for line in response_text.splitlines():
        if line.strip().endswith("?") and QUESTION_ONLY.match(line):
            continue
        keep.append(line)
    return "\n".join(keep)


def _no_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1", text)
    text = re.sub(r"[#*_`>]+", " ", text)
    return text


def _strip_urls(text: str) -> str:
    return re.sub(r"https?://\S+", " ", text)


def _sentences(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\s*[-*•]\s|^\s*\d+[.)]", line):
            out.append(line)  # list items stay intact so ordinals carry rank
            continue
        out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip())
    return out


def _product_tokens(product: dict) -> list[str]:
    title = (product.get("title") or "").strip()
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'&-]{2,}", title.lower())
    return [tok for tok in tokens if len(tok) >= 3 and tok not in STOP_CAPS]


def _own_tokens(products: list[dict]) -> set[str]:
    tokens = set()
    for p in products:
        tokens.update(_product_tokens(p))
        vendor = (p.get("vendor") or "").strip()
        if vendor:
            tokens.update(w.lower() for w in re.findall(r"[A-Za-z0-9&-]{2,}", vendor))
    return tokens


def _token_patterns(tokens: list[str]) -> list[re.Pattern]:
    return [re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE) for t in tokens]


def _count_mentions(clean: str, tokens: list[str]) -> int:
    """Same-sentence repeats collapse to one; URL-only occurrences don't count."""
    prose = _strip_urls(clean)
    patterns = _token_patterns(tokens)
    count = 0
    for sent in _sentences(prose):
        if any(rx.search(sent) for rx in patterns):
            count += 1
    return count


def _first_index(clean: str, tokens: list[str]) -> int:
    idxs = []
    for rx in _token_patterns(tokens):
        m = rx.search(clean)
        if m:
            idxs.append(m.start())
    return min(idxs) if idxs else -1


def _sources(response_text: str) -> list[dict]:
    """Extract cited links: [title](url) first, then bare URLs."""
    raw = []
    for title, url in re.findall(r"\[([^\]]*)\]\((https?://[^)]+)\)", response_text):
        raw.append((title, url))
    for url in re.findall(r"\bhttps?://[^\s)>\]\"]+", response_text):
        raw.append(("", url))
    seen = set()
    sources = []
    for title, url in raw:
        url = url.rstrip(".,;)\"'")
        domain = urlparse(url).netloc.lower().replace("www.", "")
        if not domain:
            continue
        if any(domain == d or domain.endswith("." + d) for d in PROVIDER_OWNED_DOMAINS):
            continue  # provider-owned citation, ignore (OneGlanse sourceUtils.ts rule)
        key = url  # dedupe by URL only so the bare-URL pass can't double-count
        if key in seen:
            continue
        seen.add(key)
        sources.append({
            "title": title.strip() or domain,
            "url": url,
            "domain": domain,
        })
    return sources


def _merchants(response_text: str) -> list[str]:
    found = RETAILER_RE.findall(response_text)
    out = []
    for m in found:
        if m.lower() not in [o.lower() for o in out]:
            out.append(m.capitalize() if len(m) > 4 else m)
    return out


def _competitor_candidates(sub: str, products: list[dict], brand_present: bool) -> list[dict]:
    """Brand names co-mentioned with our products (or anywhere, if we're absent)."""
    own = _own_tokens(products)
    own_patterns = _token_patterns(sorted(own))
    # Full names so "Aurora Pack" (multiword) is recognized as ours, not a competitor.
    own_texts = " || ".join(
        ((p.get("title") or "") + " " + (p.get("vendor") or "")).lower() for p in products
    )
    retailers = _merchants(sub)
    retailer_lows = {r.lower() for r in retailers}
    scope = _sentences(_no_markdown(_strip_urls(sub)))
    if brand_present:
        scoped = [sent for sent in scope if any(rx.search(sent) for rx in own_patterns)]
        scope = scoped
    candidates: dict[str, int] = {}
    for sent in scope:
        is_listy = bool(re.match(r"^\s*[-*•]\s|^\s*\d+[.)]", sent))
        sent_low = sent.lower()
        for match in re.findall(r"\b[A-Z][A-Za-z&'-]{2,}(?:\s+[A-Z][A-Za-z&'-]{2,}){0,2}", sent):
            key = match.strip()
            low = key.lower()
            if "'" in low:  # "I'd" style contractions aren't brands
                continue
            if low in STOP_CAPS or low in own or low in own_texts or low in retailer_lows:
                continue
            if len(key.split()) > 3:
                continue
            first_word = key.split()[0].lower()
            if first_word in STOP_CAPS:
                continue
            # Capitalized topic words that open a prose sentence aren't competitors.
            if not is_listy and len(key.split()) == 1 and sent_low.startswith(low):
                continue
            candidates[key] = candidates.get(key, 0) + 1
    ranked = sorted(candidates.items(), key=lambda kv: -kv[1])[:6]
    out = [{"name": name, "kind": "retailer" if name.lower() in retailer_lows else "brand",
            "co_mentions": count} for name, count in ranked]
    for r in retailers:
        if not any(o["name"].lower() == r.lower() for o in out):
            out.append({"name": r, "kind": "retailer", "co_mentions": 0})
    return out[:8]


def _domain_counts(sources: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for s in sources:
        counts[s["domain"]] = counts.get(s["domain"], 0) + 1
    return [{"domain": d, "count": n} for d, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def _analyze_product(product: dict, clean: str, total_words: int) -> dict | None:
    tokens = _product_tokens(product)
    if not tokens:
        return None
    idx = _first_index(clean, tokens)
    if idx < 0:
        return None

    mention_count = _count_mentions(clean, tokens)
    patterns = _token_patterns(tokens)
    our_sents = [s for s in _sentences(clean) if any(rx.search(s) for rx in patterns)]
    our_text = " ".join(our_sents).lower()

    # --- Visibility dimension 1: coverage (share of the response talking about this product)
    coverage = min(100.0, 100.0 * sum(len(s.split()) for s in our_sents) / total_words)

    # --- Dimension 2: placement (earlier in the reading order = better)
    placed = idx / max(1, len(clean))
    if placed <= 0.25:
        placement = 90
    elif placed <= 0.5:
        placement = 60
    elif placed <= 0.75:
        placement = 35
    else:
        placement = 12

    # --- Dimension 3: structure (list items / headings / bolded picks)
    structure = 0
    for s in our_sents:
        if re.match(r"^\s*#{1,3}\s", s) or re.search(r"top pick|best overall|#1", s.lower()):
            structure = max(structure, 85)
        elif re.match(r"^\s*[-*•]\s|^\s*\d+[.)]", s):
            structure = max(structure, 65)
        else:
            structure = max(structure, 30)

    # --- Dimension 4: frequency
    freq = 80 if mention_count >= 6 else 68 if mention_count >= 4 else 50 if mention_count >= 2 else 30

    # --- Sentiment decision tree (anti-inflation caps included)
    neg_hits = sum(1 for k in NEGATIVE_STRONG if k in our_text)
    drawbacks = [k for k in DRAWBACK if k in our_text]
    supers = [k for k in SUPERLATIVE if k in our_text]
    positives = [k for k in POSITIVE if k in our_text]
    conditionals = [k for k in CONDITIONAL if k in our_text]
    contrastives = [k for k in CONTRASTIVE if k in our_text]

    if neg_hits:
        sentiment = 15  # explicitly warned against / discouraged
    elif drawbacks and not positives:
        sentiment = 35  # drawbacks only
    elif supers and drawbacks:
        sentiment = 65  # favorable with noted caveats (capped below enthusiastic 81+)
    elif positives and len(drawbacks) >= 2:
        sentiment = 45  # caveat-heavy: mild praise but dominant limitations
    elif supers:
        sentiment = 85  # superlatives, no caveats
    elif positives:
        sentiment = 66  # favorable language, no superlatives
    else:
        sentiment = 50  # factual / neutral list entry

    # --- Dimension 5: framing (is it recommended as an answer, or just contrast material?)
    framing = 55
    if any(k in our_text for k in SUPERLATIVE):
        framing = 90
    elif positives:
        framing = 75
    elif conditionals:
        framing = 55
    elif contrastives:
        framing = 35
    else:
        framing = 40

    visibility = round(coverage * 0.25 + placement * 0.25 + structure * 0.20 + freq * 0.15 + framing * 0.15)

    # Anti-inflation caps on visibility
    if mention_count <= 1:
        visibility = min(visibility, 50)
    if contrastives and not supers:
        visibility = min(visibility, 35)

    # --- Absolute rank: reading-order ordinal of list items that carry this product
    rank = None
    for s in our_sents:
        m = re.match(r"^\s*(\d+)\s*[.)]", s)
        if m:
            rank = int(m.group(1))
            break

    # --- Recommendation ladder (first match wins)
    if neg_hits and contrastives:
        rec = "discouraged"
    elif rank == 1 and supers:
        rec = "top_pick"
    elif rank and rank <= 3 and (positives or supers):
        rec = "strong_alternative"
    elif conditionals:
        rec = "conditional"
    else:
        rec = "mentioned_only"
    # A #1 slot with zero evaluative language is NOT a top pick (OneGlanse strictness rule)
    if rec in ("top_pick", "strong_alternative") and not (positives or supers):
        rec = "mentioned_only"

    rank_score = {1: 100, 2: 80, 3: 65, 4: 50}.get(rank, 40 if rank else 15)
    rec_score = {"top_pick": 100, "strong_alternative": 80, "conditional": 60,
                 "mentioned_only": 30, "discouraged": 10, "not_mentioned": 0}[rec]
    geo = round(0.25 * visibility + 0.25 * rank_score + 0.25 * sentiment + 0.25 * rec_score)

    # GEO credibility caps from the methodology
    if visibility < 20:
        geo = min(geo, 40)
    if sentiment <= 20:
        geo = min(geo, 30)

    return {
        "product_id": str(product["id"]),
        "title": product.get("title"),
        "rank": rank,
        "visibility": visibility,
        "coverage": round(coverage),
        "placement": placement,
        "structure": structure,
        "frequency": freq,
        "framing": framing,
        "sentiment": sentiment,
        "recommendation": rec,
        "geo_score": geo,
        "positive_signals": supers or positives,
        "caveats": len(drawbacks),
        "mention_count": mention_count,
    }


def analyze_response(prompt: str, response_text: str, products: list[dict]) -> dict:
    sub = _substantive_text(response_text)
    clean = _no_markdown(sub)
    source_list = _sources(response_text)
    retailers = _merchants(response_text)

    indices = [i for p in products if (i := _first_index(clean, _product_tokens(p))) >= 0]
    brand_present = bool(indices)
    quality = "substantive" if len(_strip_urls(clean).split()) > 40 else "non_answer"

    if not brand_present:
        return {
            "quality": quality,
            "mentioned": False,
            "visibility": 0,
            "rank": None,
            "sentiment": 50,
            "recommendation": "not_mentioned",
            "geo_score": 0,
            "products": [],
            "competitors": _competitor_candidates(sub, products, brand_present=False),
            "sources": source_list,
            "source_domains": _domain_counts(source_list),
            "retailers": retailers,
            "risks": [{
                "severity": "info",
                "type": "missing_from_response",
                "note": "No catalog products appear anywhere in this answer.",
            }],
        }

    total_words = max(1, len(clean.split()))
    results = []
    for p in products:
        r = _analyze_product(p, clean, total_words)
        if r:
            results.append(r)

    results.sort(key=lambda r: (r["rank"] or 99, -r["geo_score"]))
    best = max(results, key=lambda r: r["geo_score"]) if results else None
    avg_vis = round(sum(r["visibility"] for r in results) / len(results)) if results else 0
    avg_sent = round(sum(r["sentiment"] for r in results) / len(results)) if results else 50

    risks = []
    if best:
        if best["caveats"] >= 2:
            risks.append({"severity": "warning", "type": "negative_association",
                          "note": f"{best['title']} carries {best['caveats']} drawback cues near its mention."})
    outdated = [k for k in ("discontinued", "no longer", "used to", "has been replaced") if k in clean.lower()]
    if outdated:
        risks.append({"severity": "warning", "type": "outdated_info",
                      "note": "Response contains possibly outdated claims: " + ", ".join(outdated[:3])})

    return {
        "quality": quality,
        "mentioned": True,
        "visibility": avg_vis,
        "rank": results[0]["rank"] if results else None,
        "sentiment": avg_sent,
        "recommendation": best["recommendation"] if best else "mentioned_only",
        "geo_score": best["geo_score"] if best else 0,
        "products": results,
        "competitors": _competitor_candidates(sub, products, brand_present=True),
        "sources": source_list,
        "source_domains": _domain_counts(source_list),
        "retailers": retailers,
        "risks": risks,
    }
