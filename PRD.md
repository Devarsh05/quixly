# Quixly — Product Requirements Document

**Version:** 0.1 (build kickoff)
**Owner:** Devarsh
**Status:** Draft → building MVP
**One-liner:** An autonomous agent that gets Shopify merchants' products recommended and bought by AI shopping assistants (ChatGPT, Google AI Mode, Perplexity, Copilot, Gemini) — and proves the revenue lift.

---

## 1. Problem

AI answer/shopping engines are becoming a primary product-discovery channel. In March 2026 Shopify auto-activated "Agentic Storefronts" for 2M+ US stores, syndicating catalogs into ChatGPT and other engines; AI-attributed orders on Shopify grew ~11–15x year-over-year.

But most merchants get little or no AI traffic, because AI agents rank on **structured, machine-readable product data**, not page design. Typical catalogs have thin/"marketing fluff" descriptions, missing GTINs, inconsistent variants, and no product schema — so engines can't parse or confidently recommend them. Merchants are also **blind**: they don't know whether ChatGPT recommends them or a competitor, or why.

Existing tooling is mostly **passive monitoring** (Shopify's native dashboard, GA4 setups, horizontal GEO trackers). Almost nothing **diagnoses the gap and fixes it automatically across engines, then verifies the uplift.**

## 2. Solution

A Shopify app whose multi-agent system runs a continuous loop:

1. **Interrogate** the engines with real buyer-intent queries and measure *share-of-model* (how often the store is recommended vs. named competitors, and why).
2. **Simulate** an AI shopping agent against the store's catalog to find where products are unparseable or unselectable.
3. **Diagnose** the specific data gaps behind competitor wins.
4. **Fix** them — grounded rewrites to spec-dense descriptions, JSON-LD product schema, filled metafields/GTINs — pushed to Shopify behind a preview + approval gate.
5. **Verify** by re-running the panel and reporting the recommendation-rate delta and AI-referral traffic.

## 3. Goals & non-goals

**Goals (first 3 months)**
- Ship a Shopify App Store listing with a self-serve free audit → paid tiers.
- Deliver a demonstrable before/after: measurable share-of-model uplift on a fixed query panel within ~2–4 weeks of a merchant enabling fixes.
- Get to first 10 paying merchants in one starting vertical.

**Non-goals (for now)**
- Full CRO / paid-ads / checkout optimization (that's Ryze's lane — stay focused on AI-shopping *visibility*).
- Non-Shopify platforms (WooCommerce/BigCommerce/Amazon) — architecture stays portable, but not in MVP.
- In-chat checkout mechanics — Shopify owns the rails; we optimize discovery/recommendation.
- Fabricating any product attribute, review, or spec. Ever.

## 4. Target user & positioning

**Primary user:** owner/marketer of a DTC Shopify store, 10–2,000 SKUs, in a spec-legible category (start with ONE: e.g., coffee, outdoor gear, or supplements — categories where "200 GSM / 15,000 mAh / 100% GOTS cotton" clearly beats fluff).

**Jobs to be done:** "Am I being recommended by AI shoppers? If not, why, and fix it — and show me it worked."

**Differentiation**
- vs. **Profound / horizontal GEO** ($58.5M-funded, enterprise/marketer, mostly monitoring): we're Shopify-vertical, action-oriented, self-serve, and priced for SMBs.
- vs. **Ryze / broad autonomous CRO:** we're the dedicated "get recommended by AI shoppers" tool, not a whole-funnel optimizer.
- vs. **Shopify's native Agentic Commerce dashboard:** Shopify shows a score for its own syndication; we diagnose + fix + verify **across all engines**, including the off-store surfaces engines actually cite (Perplexity leans on Reddit; ChatGPT on Wikipedia/G2) that Shopify Catalog can't fix for you.

## 5. Key user flows

1. **Install & free audit (hook):** merchant installs → OAuth → catalog ingested → agent runs a starter query panel → report: "AI Visibility Score, competitors beating you, and your invisible products + why." No payment required for the audit.
2. **Fix & publish:** merchant reviews prioritized fixes → sees before/after diff per product → approves → agent publishes to Shopify.
3. **Verify & report:** weekly re-scan → uplift chart (share-of-model delta) + AI-referral traffic → email/Slack digest.
4. **Ongoing:** scheduled scans surface new gaps as catalog/engines change; agent proposes fixes continuously.

## 6. Agent design (multi-agent, LangGraph)

Nodes:
- **Interrogator** — generates/loads the buyer-intent query panel per category ("best X for Y under $Z").
- **EngineRunner** — fans out each query across engines; returns raw answers + citations.
- **Extractor** — parses which brands/products are recommended, in what order, and which sources were cited.
- **ShareOfModelAggregator** — computes recommendation rate for the store vs. named competitors, per engine, per period.
- **ShoppingAgentSimulator** — runs a browser agent (Browserbase) against the store the way an AI shopper would; records parse/selection failures.
- **Diagnostician** — maps competitor wins + simulator failures to specific data gaps (missing GTIN, thin copy, no schema, bad variants).
- **Optimizer/Writer** — produces **grounded** fixes: spec-dense description rewrites, JSON-LD (Product/Offer/AggregateRating/FAQ), metafields, normalized variants. Only from verified source data.
- **ApprovalGate** — human review; nothing publishes without it (hard rule).
- **Publisher** — writes approved changes via Shopify Admin API; re-reads the published page as an agent to confirm it parses.
- **Verifier** — re-runs the panel, computes uplift delta, updates precedent memory.

**Memory**
- *Org memory:* catalog snapshot, named competitors, category baselines, brand voice/style constraints.
- *Precedent memory:* which fix types moved share-of-model → prioritize future fixes.
- *Case memory:* per-product evidence, gaps, fix history.

**Grounding & verification (the core discipline)**
- Never assert an attribute not present in verified source data (merchant fields, spec sheets, GTIN lookup).
- Every fix carries a before/after diff and a citation to its source.
- Publish only after approval; roll out on low-risk products first; verify the published page re-parses correctly.

## 7. Architecture & tech stack

**Two services, one repo (monorepo):**

- **App shell — `app/` (TypeScript, Shopify React Router app template):** OAuth, session storage, billing, webhooks, App Bridge + Polaris embedded UI. Use the official template because it removes the most error-prone Shopify plumbing. This is the thin Shopify-facing layer. (Shopify's official template migrated from Remix to React Router 7; `@shopify/shopify-app-react-router`.)
- **Agent service — `agent/` (Python, FastAPI + LangGraph):** the brain — engine querying, simulation, diagnosis, optimization, verification, workers. This is where the IP lives and where your strengths are.

**Shared infra**
- **Postgres** (primary store; pgvector for semantic recall of flows/precedents).
- **Redis** (job queue + locks + short-lived run state).
- **Worker** (Arq or Celery) for async scans and scheduled weekly runs.
- **Browserbase** for browser-agent simulation / ground-truth engine checks.
- **Engine access:** Perplexity Sonar API (returns citations), OpenAI API, Gemini API (Google Search grounding) for scalable panels; Browserbase runs for periodic ground-truth calibration against the real ChatGPT/Google shopping surfaces.
- **Deploy:** Railway (both services + Postgres + Redis); Vercel optional for a marketing site.

**Interfaces**
- App shell ↔ agent service over an internal authenticated API (shared secret,
  `INTERNAL_API_KEY`). **Bidirectional:** the app shell pushes events to the agent
  (shop connected, webhooks), and the agent calls *back* to the app shell to obtain
  short-lived Shopify admin tokens — the app shell is the single token-refresh authority.
- Agent service exposes an **MCP server** (`scan_visibility`, `audit_product`, `propose_fix`, `publish_fix`, `verify_uplift`) so the loop is MCP-native and drivable by other agents.

## 8. Data model (core tables)

- `shops(id, shop_domain, plan, status, created_at)`
  *Revised in Phase 1: the original `access_token_ref` column is **removed**. Shopify offline
  tokens now expire (~60 min), and minting a new one invalidates the previous token's refresh
  token — so there can be exactly one refresh authority, and that is the app shell. The agent
  stores no Shopify credential; it fetches short-lived tokens from the app shell on demand via
  `TokenProvider`. `status`: `active|uninstalled|reauth_required`.*
- `products(id, shop_id, shopify_product_id, title, body, variants_json, gtin, metafields_json, visibility_state, updated_at)`
  *Note: GTIN/barcode is a **variant** field in Shopify. `products.gtin` is a convenience column
  populated from the primary variant; every variant's barcode lives in `variants_json`.*
- `ingest_runs(id, shop_id, status, products_seen, products_written, cursor, error, started_at, completed_at)`
  *Added in Phase 1. Kept separate from `agent_runs`, which is shaped for LangGraph node
  execution. `cursor` makes a failed catalog ingest resumable rather than restarting from zero.*
- `competitors(id, shop_id, name, domain)`
- `query_panels(id, shop_id, category, queries_json, created_at)`
- `engine_runs(id, panel_id, engine, query, response_raw, cited_brands_json, cited_sources_json, our_mentions_json, ts)`
- `share_of_model(id, shop_id, engine, period, our_rate, competitor_rates_json)`
- `audits(id, product_id, gaps_json, severity, created_at)`
- `fixes(id, product_id, type, before_json, after_json, status, diff, created_at)`  // status: proposed|approved|published|verified|rejected
- `verifications(id, fix_id, pre_rate, post_rate, delta, ts)`
- `agent_runs(id, shop_id, node_logs_json, tokens, model, started_at, completed_at)`
- `billing(id, shop_id, plan, status, current_period_end)`

## 9. API sketch (agent service, FastAPI)

- `POST /shops/connect` — register shop + enqueue catalog ingest (202 + `run_id`)
  *Revised in Phase 1: was `POST /shops/{id}/connect`, which cannot work — at connect time no
  internal shop id exists yet. Keyed on `shop_domain`, idempotent, and it carries no access
  token. Companion: `GET /shops/{id}/ingest/{run_id}` for progress.*
- `POST /shops/{id}/panels` — generate/define query panel for categories
- `POST /shops/{id}/scan` — run panel → share-of-model + audits (async job → returns run_id)
- `GET  /shops/{id}/report` — visibility score, competitor gaps, invisible products
- `POST /products/{id}/audit`
- `POST /products/{id}/fixes/generate`
- `POST /fixes/{id}/approve` — publish to Shopify Admin API
- `POST /fixes/{id}/verify` — re-scan, compute delta
- `POST /webhooks/shopify` — products/update, app/uninstalled (forwarded from app shell)
- MCP tools mirror the above.

## 10. MVP vs. advanced

**MVP (ship this)**
- One vertical. OAuth + catalog ingest. Query panel across 3 engines (Perplexity + OpenAI + Gemini). Share-of-model report + competitor gaps + invisible-product list (the free audit). One-click grounded fixes (description rewrite + JSON-LD + metafields/GTIN) with preview/approve → publish. Re-scan to show uplift. Basic billing.

**Advanced (after traction)**
- Browserbase shopping-agent simulation as default ground truth. Off-store citation surface (Reddit/G2/Wikipedia entity signals) recommendations. Weekly auto-scans + digests. Auto-publish (trusted mode). Multi-vertical. AI-referral revenue attribution. WooCommerce/BigCommerce expansion. MCP server for external agents.

## 11. Success metrics

- **North star:** share-of-model uplift per merchant (recommendation rate on a fixed panel, before vs. after).
- Activation: % installs that complete the free audit.
- Conversion: free audit → paid.
- Retention: monthly active merchants; churn.
- Leading value proof: median days-to-first-uplift after enabling fixes.
- Revenue: MRR, ARPU.

## 12. Monetization

Shopify App Store subscription. Free audit as the hook.
- **Starter ~$29/mo:** 1 vertical, N products, 3 engines, manual approve.
- **Growth ~$99/mo:** more products, weekly auto-scans, all engines, off-store recommendations.
- **Pro ~$199+/mo:** high product counts, auto-publish, priority scans, done-with-you onboarding.

## 13. Risks & de-risking

- **Platform dependency (highest business risk):** Shopify/OpenAI/Google own the rails and change them fast (OpenAI removed in-chat Instant Checkout in March 2026; Shopify ships 150+ updates a season and has a native dashboard it could extend). *De-risk:* be the cross-engine, off-store, **action** layer the native tools aren't; keep architecture platform-agnostic; build merchant workflow trust.
- **Hardest technical risk — safe write-back:** publishing incorrect or hallucinated product data into a live store instantly damages the merchant and causes churn. *De-risk:* strict grounding (never invent attributes; enrich only from verified data), diff + approval gate, staged rollout on low-risk products, and a post-publish re-parse verification. This is also the flagship demonstration of your agent-verification skill.
- **Attribution is messy:** "we caused this revenue" is hard when AI referral tracking is immature. *De-risk:* sell the clean leading metric (share-of-model before/after on a fixed panel); report AI-referral traffic via a UTM/GA4 setup you provide; treat precise revenue attribution as a later feature.
- **Engine API ≠ real surface:** API answers may differ from what users see in ChatGPT/Google shopping. *De-risk:* APIs for scalable panels + periodic Browserbase ground-truth calibration; disclose methodology.

## 14. Open questions

- Which starting vertical maximizes spec-legibility + reachable merchants?
- Which engines to prioritize for the MVP panel (weight by real shopping share)?
- Auto-publish threshold: what confidence/precedent unlocks trusted mode?

## 15. Build phases (expand each into tasks via plan mode)

- **Phase 0 — Scaffold:** monorepo, CLAUDE.md, Shopify React Router app (official template), FastAPI agent service, Postgres/Redis, docker-compose, Railway deploy, dev store.
- **Phase 1 — Connect:** Shopify OAuth, embedded app loads in dev store, catalog ingestion → `products`.
- **Phase 2 — Audit (first demoable value):** query panel + EngineRunner (Perplexity/OpenAI/Gemini) + Extractor + ShareOfModel + read-only report UI. *This is the free-audit hook.*
- **Phase 3 — Fix:** product audit + grounded Optimizer (description + JSON-LD + metafields/GTIN) + preview/approve UI + Publisher (Admin API).
- **Phase 4 — Verify:** Verifier loop + uplift chart + scheduled weekly scans + Browserbase simulation.
- **Phase 5 — Ship:** Shopify Billing API tiers + onboarding + App Store submission + MCP server.
