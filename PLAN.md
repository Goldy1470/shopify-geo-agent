# PLAN.md — shopify-geo-agent end-to-end upgrade

The loop this app implements:

```
TRACK ────────▶ DIAGNOSE ────────▶ SUGGEST ────────▶ APPLY ────────▶ VERIFY
prompt runs    scoring v2 +        prioritized fix    approval-gated    re-runs + AI-referred
per provider   catalog readiness   queue w/ diffs     writes + rollback revenue timelines
```

## Module layout (target)

```
app/
  api/            webhook+REST endpoints (http boundary layer)
  core/           settings (env+keyring), security (CSRF, SSRF guard, redaction logging)
  shopify/        graphql client, throttling, adapters, catalog sync/mirror, webhooks
  providers/      GeProvider interface: openai / perplexity / gemini / anthropic (+none)
  analysis/       OneGlanse-methodology analyzer (LLM, T=0, Pydantic schema) + regex fallback
  scoring/        scoring v2 (YAML-driven) + catalog-readiness checks + JSON-LD validation
  suggest/        suggestion engine, validators, priority scoring
  apply/          apply pipeline: dry-run, staleness, snapshot, write, read-back, rollback
  roi/            AI-referred sessions/orders, timelines, digests
  db/             SQLAlchemy 2.0 models, session, alembic migrations, repository helpers
  templates/      Jinja pages (HTMX interactivity allowed)
  main.py         FastAPI wiring
tests/            unit + contract + regression + e2e (mock mode, full loop)
docs/             ARCHITECTURE.md, SCORING.md
```

## DB schema (Phase 1+)

**Catalog mirror**: `products` (shopify_gid PK, title, handle, status, vendor, product_type, tags,
description_html, seo_title, seo_description, price_min/max, status_flags, content_hash, synced_at)
· `variants` · `images` (alt, position) · `metafields` (owner_product, ns, key, type, value)
· `reviews` (source_app, count, avg — denormalized per product) · `sync_state` (cursor/hash/bookmark)

**Tracking**: `prompt_sets` · `prompts` (text, set_id, auto_generated, enabled)
· `runs` (prompt, provider, model, status, cost, started/finished) · `answers` (raw text, provider,
model, created_at, tokens/cost) · `citations` (url, domain, title, answer FK)
· `analyses` (answer FK, JSON per OneGlanse BrandAnalysisResult adapted to commerce,
analyzer_engine ["llm"|"regex"], schema_version)

**Suggestions & apply**: `suggestions` (product FK, fix_type, field, before/after JSON, payload_json,
validator_report, impact_score, status[proposed|approved|applied|discarded|stale|failed],
missing_data flag) · `snapshots` (suggestion FK, field, before_value, before_hash, taken_at)
· `applied_log` (suggestion FK, result, readback_json, applied_at) · `audit_log` (actor, action,
target, detail)

**ROI**: `ai_referrals` (source_domain, session_count, order_count, gmv, day) · `verify_re_checks`
(prompt FK related to applied batch)

SQLite default; `DATABASE_URL` switches to Postgres. Alembic migrations from day one.

## Provider rules (hard constraints)

- Collection: official APIs with web search/grounding only (OpenAI Responses `web_search`,
  Perplexity Sonar, Gemini grounding, Anthropic web search), env-configured, skipped if keyless.
  Everything labeled "API-based estimate". Paste mode remains. **No** consumer-site automation.
- Writes: approval-gated → dry-run → staleness hash check → snapshot → GraphQL write
  (productUpdate / metafieldsSet / alt text updates) → read-back verify → audit entry; one-click
  rollback. Idempotent, retry-safe, rate-limit-aware batching.
- Generated text: only tokens traceable to product data; violations blocked by validators and log
  a "missing data" task instead. Titles: only extended/reordered, name never removed.
- Secrets: env vars or OS keyring; never rendered, never logged. Bind 127.0.0.1 by default.

## Phase checklist

### Phase 0 — Foundation (this phase)
- [x] pytest + coverage config; ruff; mypy; pre-commit config; Makefile; Dockerfile; .env.example
- [x] CI (GitHub Actions): lint + type + tests
- [x] Fixtures: synthetic Shopify-shaped JSON (REST-shaped mock now, GraphQL fixtures Phase 1)
- [x] Characterization tests: current scoring on mock catalog (pins behavior)
- [x] NOTICE (OneGlanse credit)
- Bug fixes independent of later phases: CSRF/Origin, SSRF guard on /accessibility/run, secrets
  masking in settings HTML, settings-POST blank-token guard, extract_mentions word-boundary
  matching, title fix preserves product name, placeholder validator, idempotent marker-delimited
  description block (HTML-escaped)
- Defer (needs Phase 1 infra): staleness check against persistent remote state, shared client +
  persistent mock state, GraphQL-only metafield type swap (adapter redesign), catalog caching
  (redis-free in-memory LRU + mirror in Phase 1)

### Phase 1 — Data layer
- [ ] SQLAlchemy 2.0 models + Alembic; Postgres-compatible via DATABASE_URL
- [ ] Async Shopify GraphQL client: cost-based throttling, backoff on 429/5xx, cursor pagination,
      Bulk Operations for >~1000 products, webhooks (products/update, products/delete)
- [ ] Adapters: product fields, metafields (`json` type not `json_string`), standard taxonomy,
      review app adapters (Yotpo/Judge.me/Okendo/Shopify Point), images/alt
- [ ] Catalog mirror with content hashes; incremental sync; shared async client
- [ ] Persistent mock store in DB with write→read-back behavior
- [ ] Acceptance: live-shaped product scores A/B — not 57
- Verify from docs before coding: exact current stable API version, productUpdate field limits,
      metafieldSet / metafieldsSet signatures, webhook topics, visibility-for-write-products scope

### Phase 2 — DIAGNOSE
- [x] Weights/thresholds to YAML w/ heuristic labels + source/date
- [x] Review threshold 787 configurable
- [x] Catalog readiness: taxonomy, GTIN/barcode, SEO title+desc, variant completeness,
      price/availability sanity, alt text, Product JSON-LD fetch+validation
- [x] Issues carry factor/severity/evidence/fix_type

### Phase 3 — TRACK
- [ ] Prompt sets (user + auto-generated from catalog), editable UI
- [ ] Provider interface + 4 implementations, env-based, skip-if-no-key, cost caps
- [ ] Runner: N=5 per prompt per provider, randomized order, bounded concurrency, resumable
- [ ] OneGlanse analysisPrompt → Python, Pydantic output, T=0, JSON-validate + retry; deterministic
      checklist guards; commerce adaptations (per-product, price/buy link, competitors)
- [ ] Regex analyzer = offline fallback
- [ ] Aggregation: mention rate + Wilson CIs, share of voice, top domains, trends
- [ ] Pages: Visibility, prompt detail, competitors

### Phase 4 — SUGGEST
- [ ] Engine modes: rules/templates + LLM-judged via same provider interface
- [ ] Fix types: title extension, description sections, FAQ, alt text, metafield/taxonomy fill,
      JSON-LD Liquid snippet (suggested, never auto-installed), review-collection nudge,
      missing-data tasks
- [ ] Validators: name preserved, limits, placeholders, claim-tracing, HTML-safe, no dupes,
      no ALL-CAPS/stuffing
- [ ] Impact score = visibility gap × revenue weight × fix confidence; diffs in UI

### Phase 5 — APPLY
- [ ] Approval queue: single/bulk, edit-before-approve, discard
- [ ] dry-run → staleness → snapshot → GraphQL write → read-back → audit log
- [ ] one-click rollback, rollback after partial-failure, bulk batching (rate-limit aware)

### Phase 6 — VERIFY & ROI
- [ ] Re-run affected prompts after an applied batch
- [ ] AI-referred sessions/orders (order referrer/landing-site; match chatgpt.com, perplexity.ai,
      gemini.google.com, copilot.microsoft.com — from Shopify data, verified in Phase 6 docs)
- [ ] Before/after timelines w/ annotations, CIs, "correlation not causation" labels
- [ ] Weekly digest page (+ optional email/webhook)

### Phase 7 — Hardening
- [ ] Dashboard password (optional), CSRF everywhere + Origin/Host checks (extended from Phase 0)
- [ ] Rate limits, redacted structured logging, DB backup/export, /health, Docker Compose,
      README + docs/ARCHITECTURE.md + docs/SCORING.md verified against current Shopify docs

## Testing
- Unit: every scorer, validator, analyzer guard, adapter
- Contract: recorded GraphQL fixtures; providers mocked (recorded JSON)
- E2E (mock): sync→score→track→suggest→approve→apply→read-back→rollback→state equals original
- Regression per KNOWN BUG: title keeps name; no duplicate blocks; placeholders blocked;
  staleness refusal; secrets never in HTML
- Coverage target: 85%+ on scoring/suggest/apply/analysis packages

## Known risks
- API-based visibility ≠ real UI answers (labeled honestly everywhere)
- Review-app data access is best-effort per app; storePoint adapters documented, no scraping
- Shopify order attribution for AI referrals is heuristic domain matching (labeled as such)
