"""Checks whether AI agents can reach and use your storefront.

What we check (mirrors how personal agents browse):
- robots.txt rules for known AI agents (OpenAI, Perplexity, Anthropic, Meta,
  Amazon, Apple, Google AI data-collection)
- llms.txt presence
- server-rendered product title in raw HTML (JS-only pages are invisible to
  crawlers and expensive for agents)
- JSON-LD Product schema (name, price, image, rating)
- semantic HTML: <h1>, labeled form controls (size/variant selectors),
  button-like add-to-cart, aria-labels
"""

import json
import re
from typing import Any

import httpx

KNOWN_AGENTS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User",
    "PerplexityBot", "ClaudeBot", "anthropic-ai",
    "meta-webindexer", "Meta-ExternalAgent", "Amazonbot", "Applebot-Extended",
    "Google-Extended", "Bytespider",
]


def check_robots_txt(url: str) -> tuple[list[str], list[str], str]:
    domain = re.sub(r"^https?://", "", url.strip("/"))
    robots_url = f"https://{domain}/robots.txt"
    try:
        resp = httpx.get(robots_url, timeout=15, follow_redirects=True,
                         headers={"User-Agent": "geo-agent-audit/1.0"})
    except httpx.HTTPError:
        return [], [], robots_url
    if resp.status_code != 200:
        return [], [], robots_url
    text = resp.text
    groups = []
    current_ua = None
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("user-agent"):
            current_ua = line.split(":", 1)[1].strip()
        elif line.lower().startswith("disallow") and current_ua is not None:
            path = line.split(":", 1)[1].strip()
            groups.append((current_ua, path))
    allowed = []
    blocked = []
    for agent in KNOWN_AGENTS:
        rules = [p for ua, p in groups if ua == agent or ua == "*"]
        blocks = any(p == "/" for ua, p in groups if ua == agent)
        if agent == "GPTBot":
            blocks = blocks or any(ua == "GPTBot" and p != "" and p.split("/")[:2] == ["/", "collections"] for ua, p in groups)
        if blocks:
            blocked.append(agent)
        elif rules:
            allowed.append(agent)
        else:
            allowed.append(agent)
    return allowed, blocked, robots_url


def check_llms_txt(url: str) -> bool:
    domain = re.sub(r"^https?://", "", url.strip("/"))
    try:
        resp = httpx.get(f"https://{domain}/llms.txt", timeout=10,
                         headers={"User-Agent": "geo-agent-audit/1.0"})
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def check_page(url: str) -> dict:
    """Fetch raw HTML and evaluate agent-friendliness signals."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; geo-agent-audit/1.0)"}
    checks: dict[str, Any] = {
        "url": url,
        "reachable": False,
        "h1": None,
        "ssr_product_title": None,
        "jsonld_product": False,
        "jsonld_price": False,
        "jsonld_rating": False,
        "button_semantics": False,
        "labeled_inputs": True,
        "aria_label_count": 0,
        "unlabeled_selects": 0,
        "notes": [],
    }
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True, headers=headers)
        checks["reachable"] = resp.status_code == 200
        html = resp.text
    except httpx.HTTPError as exc:
        checks["notes"].append(f"Fetch failed: {exc}")
        return checks

    if not checks["reachable"]:
        checks["notes"].append(f"HTTP {resp.status_code} - agents cannot read the page.")
        return checks

    h1s = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    checks["h1"] = re.sub(r"<[^>]+>", "", h1s[0]).strip() if h1s else None

    for script in re.findall(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html, re.S | re.I):
        try:
            data = json.loads(script)
        except ValueError:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if "@graph" in entry:
                entries.extend(entry["@graph"])
                continue
            types = entry.get("@type")
            types = types if isinstance(types, list) else [types]
            if any("Product" in (t or "") for t in types):
                checks["jsonld_product"] = True
                if "offers" in entry:
                    offers = entry["offers"]
                    offers = offers if isinstance(offers, list) else [offers]
                    checks["jsonld_price"] = any("price" in o for o in offers if isinstance(o, dict))
                agg = entry.get("aggregateRating")
                checks["jsonld_rating"] = bool(agg and agg.get("ratingValue"))

    title_tag = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    checks["ssr_product_title"] = bool(title_tag and title_tag.group(1).strip())

    buttons = re.findall(r"<button[^>]*>", html, re.I)
    checks["button_semantics"] = bool(buttons)
    if not buttons:
        checks["notes"].append("No native <button> elements found. If add-to-cart is a <div>, agents using the accessibility tree can't activate it.")

    unlabeled = 0
    for sel in re.findall(r"<select[^>]*>", html, re.I):
        if "aria-label" not in sel and "title=" not in sel:
            unlabeled += 1
    checks["unlabeled_selects"] = unlabeled
    if unlabeled:
        checks["notes"].append(f"{unlabeled} <select> (variant/size picker) lacks an aria-label or title attribute.")

    checks["aria_label_count"] = len(re.findall(r"aria-label=", html, re.I))
    if checks["aria_label_count"] < 3:
        checks["notes"].append("Few/no aria-labels. The accessibility tree is what agents read; add labels to nav, buttons, and fields.")
    return checks


def run_full_check(store_url: str, product_url: str | None = None) -> dict:
    allowed, blocked, robots_url = check_robots_txt(store_url)
    return {
        "assets": {"robots_url": robots_url},
        "allowed_agents": allowed,
        "blocked_agents": blocked,
        "llms_txt": check_llms_txt(store_url),
        "page": check_page(product_url) if product_url else None,
    }
