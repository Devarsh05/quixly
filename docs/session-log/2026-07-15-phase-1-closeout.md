# 2026-07-15 — Phase 1: Connect (closeout)

**Branch:** `phase-1-connect`
**Goal:** Close out PRD §15 Phase 1 — first real install + catalog ingest against a live dev
store, verify the Connect gates, and confirm the app/uninstalled lifecycle.

---

## First real install + ingest

The embedded app was installed on **`quixly-ljymkoyb.myshopify.com`** and OAuth → ingest ran
end-to-end for the first time (the open item from the 2026-07-12 log — `shopify app dev` needing
an interactive Partner login — is now resolved; the app runs under `--use-localhost`).

Registering the app as a web process required an `app/shopify.web.toml` (previously absent, so the
CLI classified the app as extension-only and never provisioned an app URL, so OAuth never fired).
It pins the React Router process to a fixed `port = 3000` so the agent's `APP_SHELL_URL` stays
stable across `app dev` runs, with `dev = npm exec react-router dev` (not `npm run dev`, which
would recurse into `shopify app dev`).

## Connect gates — PASS

Verified directly against `public.products` after a clean re-ingest:

| Gate | Expected | Observed |
|---|---|---|
| Products | 20 | 20 |
| Active / Draft | 18 / 2 | 18 / 2 |
| Variants (nested in `variants_json`) | 25 | 25 |
| `visibility_state` vocabulary | lowercase | `active` / `draft` (lowercase) |
| Reinstall-idempotency | no dupes on re-ingest | upsert on `uq_products_shop_shopify_id` holds |

**GTIN (first-barcoded-variant rule):** 14 variants carry a barcode across the catalog.
`products.gtin` is populated by `extract_gtin`, which returns the **first variant in
`variants_json` order that has a barcode** (not strictly the primary/default variant). That
yields **11** product-level GTINs — i.e. 11 products have at least one barcoded variant — and the
remaining **9** products are legitimately barcode-free. Every barcode is preserved in
`variants_json` regardless; the product-level column is a convenience, not the source of truth.
(See backlog: the "first barcoded" selection is not guaranteed to be the primary variant.)

## Bug fixed this phase

Catalog ingest wrote Shopify's GraphQL `ProductStatus` enum verbatim (`ACTIVE`/`DRAFT`/`ARCHIVED`),
so `visibility_state` never matched the lowercase canonical the rest of the system (and the webhook
write path) uses. Diagnosed via the DB, not the code: the row grain was already correct (one row
per product, 37 distinct IDs at the time, no per-variant expansion) — only the status casing was
wrong. Fixed with an explicit `_STATUS` map in `agent/app/jobs/ingest_catalog.py` that raises on an
unrecognized value rather than leaking or nulling it, plus tests asserting lowercase storage for
ACTIVE/DRAFT/ARCHIVED. Re-ingest overwrote the existing rows in place (no row deletion needed).

## app/uninstalled lifecycle

Reviewed the forwarded-webhook path end to end. The app shell
(`webhooks.app.uninstalled.tsx`) authenticates under the serialized lock, forwards the topic to
the agent **before** deleting the Prisma session, then deletes it. The agent
(`webhooks.py::_handle_uninstalled`) sets `shops.status = 'uninstalled'` (lowercase, matching the
`shop_status` enum) and invalidates the cached admin token via `TokenProvider.invalidate`.

**The branch was already present and correct — no code change was needed.** There is no scheduled
scan work in Phase 1 (the Arq worker registers only `ingest_catalog`; no `cron_jobs`), so there is
nothing to cancel for an uninstalled shop yet. The reason `SELECT status FROM shops` still returned
`active` after test uninstalls is simply that **Shopify cannot deliver webhooks to a `--use-localhost`
app** — the handler is never reached, not that it is wrong.

The code path is now covered by unit tests (`agent/tests/test_webhooks.py`): forwarded
`app/uninstalled` flips the shop to `uninstalled`, requires the internal key, and is idempotent on
redelivery.

## Deferred to a tunnel session (untestable under `--use-localhost`)

Shopify does not deliver live webhooks to a localhost app, so two things cannot be exercised until
the app runs behind a public tunnel:

- **Webhook HMAC verification** (`authenticate.webhook` in the app shell) — never invoked because no
  live webhook arrives.
- **End-to-end `app/uninstalled` delivery** — the full chain (Shopify → app shell HMAC → forward →
  agent → `status = uninstalled`). The agent-side handler itself is unit-tested; only live delivery
  is deferred.

## Verification

- app: `npm test`, `npm run lint`, `npm run build` — all pass.
- agent: `pytest`, `ruff check .` — all pass (adds `test_webhooks.py`; ingest status-normalization
  tests from the earlier fix included).
