# Shopify GEO Agent

A self-hosted web dashboard that audits, scores, and fixes your Shopify catalog so your products rank in AI shopping results (ChatGPT Shopping, Google AI Mode, Copilot, Perplexity) and personal shopping agents (Meta Muse, Instinct).

Built on the ranking signals documented for these engines: descriptive titles with use cases, complete PDPs (intended use / compatibility / dimensions / materials / limitations), FAQs and Q&A, images + video + alt text, review volume (top-ranked median: 787), attribute completeness, and crawler accessibility.

## Run

```
pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:8000

Runs in **mock mode** (sample catalog) until you connect a real store.

## Connect your store

1. Shopify Admin → Settings → Apps → **Develop apps** → Create app
2. Grant scopes: `read_products`, `write_products`, `read/write product metafields`
3. Install and copy the **access token** (starts with `shpat_`)
4. In the dashboard → **Settings** → set Mode to `live`, add your `*.myshopify.com` domain + token

## Modules

| Page       | What it does |
|------------|--------------|
| Overview   | Store-wide score, worst products, recurring issues |
| Products   | Per-SKU audit: score 0–100 across 6 weighted factors, drill into issues |
| Agent access | robots.txt rules for 12 AI agents (GPTBot, PerplexityBot, ClaudeBot, **meta-webindexer/Meta-ExternalAgent for Muse**, …), llms.txt, JSON-LD Product schema, SSR/semantic HTML (buttons, labeled selects, aria-labels) |
| Visibility | Log real answers from ChatGPT/Muse/Instinct (paste mode) or automate runs with an OpenAI key; tracks mention rate, which products got cited, which merchants got the checkout link |
| Fixes      | Rules-based drafts: title rewrites, FAQ generation from specs, description checklists. Nothing goes live until you approve — approve pushes via Admin API |
| Answer analyzer | Every pasted agent answer is scored deterministically (no LLM, no API key): absolute rank by reading order, sentiment decision tree with anti-inflation caps, 5-dimension visibility, recommendation ladder, per-SKU GEO 0–100, competitor co-mentions, cited domains, risk flags |

## Answer analysis methodology

The visibility module ports the deterministic rules from [OneGlanse](https://github.com/oneglanse/oneglanse)'s open-source GEO analysis methodology (MIT) into pure Python — once ranked and cut into these rules, an LLM isn't required to execute them:

- Echoed questions and URL-only matches never count as mentions; same-sentence repeats collapse to one
- Absolute rank by reading order (list ordinals), not per-section
- Sentiment tree with anti-inflation caps: warning 15 / drawbacks 35 / neutral 50 / favorable-with-caveats 65 / superlatives-only 85+; list membership alone never reads as positive; #1 with neutral language caps at 75
- Visibility = 25% coverage + 25% placement + 20% structure + 15% frequency + 15% framing (single mention caps at 50, contrast-only at 35)
- GEO overall = 25% visibility + 25% rank + 25% sentiment + 25% recommendation tier; credibility caps (vis < 20 → ≤40, warned → ≤30)
- Recommendation ladder: not_mentioned < mentioned_only < conditional < strong_alternative < top_pick (top-3 with zero evaluative language demotes to mentioned_only)

## GEO scoring weights

- Title (20): length, category word, use case, no keyword stuffing / ALL CAPS
- Description (25): covers uses, compat, dimensions, materials, limitations, word count
- FAQ (15): 3+ items = full score
- Media (15): 5+ images, alt text, video
- Attributes (15): 5 metafields agents match against shopper profiles
- Reviews (10): 787+ = full (ChatGPT Shopping top-ranked median)

## Notes

- FAQ fixes write to metafield `geo_agent.faq_json` (render via a small theme snippet); titles/descriptions apply directly to the product.
- Muse/Instinct send no referrals and don't identify themselves — visibility uses what you can check directly (catalog presence, prompt runs) instead of referral data.
- All state in `geo_agent.db` (SQLite); config in `config.json`. Delete both to reset.
- Token stays local in `config.json`; nothing is sent anywhere except Shopify/OpenAI directly.
