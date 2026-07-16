# Backlog — known issues & deferred work

Durable ticket notes that outlive a single session. Not task lists (those live in the
PR/issue) and deliberately **not** in `CLAUDE.md`. Each entry says what, why it matters, and
the phase by which it should be revisited.

## Ingest

- **No prune path for deleted products.** Catalog ingest only inserts/updates (upsert on
  `uq_products_shop_shopify_id`); it never deletes. A product removed on the Shopify store
  leaves a stale `products` row behind indefinitely. Decide the reconciliation strategy
  (e.g. mark-and-sweep against the set of IDs seen in a full run, or handle `products/delete`
  webhooks) before catalog freshness matters downstream.
  _Raised: 2026-07-15 (Phase 1 closeout)._

- **`products.gtin` picks the first barcoded variant, not necessarily the primary one.**
  `extract_gtin` (`agent/app/services/catalog.py`) scans `variants_json` in order and returns
  the first variant that carries a barcode. So a product-level GTIN can be sourced from a
  **secondary** variant when the primary/default variant has no barcode — which may not be the
  product's canonical GTIN. (Observed: 14 barcoded variants across the catalog → 11 products
  with at least one, so 11 product-level GTINs; 9 barcode-free.) Decide the intended rule —
  primary/default variant, or first-barcoded as today — before **Phase 3 (Optimizer)**, which
  relies on product-level GTIN for grounding. Every variant's barcode is already preserved in
  `variants_json`, so this is a selection-rule change, not new data.
  _Raised: 2026-07-15 (Phase 1 closeout)._

## Token custody / refresh locking

- **Webhook refresh path is pool-coupled and can deadlock under concurrent same-shop webhook
  refreshes.** `withShopRefreshLock` (`app/app/lib/shop-lock.server.ts`) runs its critical
  section inside a `prisma.$transaction`, which pins one pooled connection and holds the
  `pg_advisory_xact_lock` for the whole transaction. The admin-token path was fixed to run its
  inner session read/write on that same transaction connection, so it borrows no extra
  connection. The **webhook** path (`app/app/lib/webhook-auth.server.ts` →
  `authenticate.webhook()` → the library's `ensureValidOfflineSession`) refreshes through the
  library's own **global-client** session storage, which cannot be pinned to the transaction
  client. So it keeps the exact deadlock the admin-side fix removed: with N concurrent
  same-shop webhook refreshes and a Prisma pool ≤ N, every connection is held by a transaction
  blocked on the advisory lock, and the lock winner cannot get the extra connection its inner
  session I/O needs → deadlock (surfaces as a hang/timeout, not an error). Masked on
  high-core-count hosts where the default pool (`num_cpus*2+1`) exceeds concurrency; reproducible
  when the pool is small (CI, constrained prod). Candidate fixes: give the webhook lock a
  **dedicated pinned connection** for its inner refresh, or use a library hook to inject
  tx-bound session storage into `authenticate.webhook()`. Deliberately left as a separate change
  — the admin-token fix was scoped to `admin-token.server.ts` and must not touch library-owned
  auth. _Raised: 2026-07-15 (admin-token tx-pinning fix)._

## App shell / tooling

- **`app/tsconfig.json` uses the deprecated `compilerOptions.baseUrl`.** Shipped by the Shopify
  React Router template; `baseUrl` is deprecated ahead of TS 7. Not failing typecheck today, but
  will need migrating (drop `baseUrl`; express any path mapping via `paths` alone) before a TS 7
  bump. _Raised: 2026-07-16 (Phase 1 closeout)._

## Webhooks / delivery

- **End-to-end webhook HMAC delivery is unverified.** Shopify does not deliver live webhooks to a
  `--use-localhost` app, so `authenticate.webhook` (HMAC verify) and the full `app/uninstalled`
  chain (Shopify → app shell HMAC → forward → agent → `status = uninstalled`) have never run against
  a live delivery. The agent-side handler is unit-tested; only live delivery is deferred. Exercise
  behind a public tunnel. _Raised: 2026-07-16 (Phase 1 closeout)._

## Compliance

- **Mandatory compliance webhooks are still commented out.** `customers/data_request`,
  `customers/redact`, and `shop/redact` are not yet subscribed in `app/shopify.app.toml`. Required
  for App Store submission (Phase 5); implement and verify before submitting.
  _Raised: 2026-07-16 (Phase 1 closeout, carried from the 2026-07-12 connect log)._
