# ASSUMPTIONS.md

Assumptions made while building (review welcome, all revisable):

1. **Single merchant, self-hosted** — one store per install; no multi-tenancy in v1. Dashboard
   password protection lands in Phase 7 (optional).
2. **SQLite default, Postgres optional** — `DATABASE_URL` env switches; Alembic migrations path.
3. **English-only text heuristics v1** (title/description/FAQ quality rules are English-tuned).
4. **Review data is best-effort** — read from product metafields that review apps commonly write
   (Yotpo / Judge.me / Okendo / Shopify Product Reviews where still exposed); no scraping of
   third-party sites. If a store has none, reviews score 0 with an explicit "connect a reviews app"
   issue.
5. **API-based visibility is labeled as an estimate** — official search/grounding APIs, never
   consumer-site browser sessions. Paste mode covers real-UI capture.
6. **Cost safety first**: per-run budget caps default small; providers skipped silently when no key.
7. **Dev environment is Windows**; `make dev` also documented with a PowerShell equivalent.
8. **No emails/webhooks sent by default**; digest page first, optional sending after config.
9. Shopify `category_prompts` in config.json are a seed; prompt sets are DB-driven from Phase 3.
10. The mock catalog is intentionally the sample in mock_data.py; Phase 1 keeps product IDs stable
    (111/222/333/444/555) so tests and user expectations stay consistent.
