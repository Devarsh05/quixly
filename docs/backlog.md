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

- **RESOLVED (2026-07-22, Phase 3 step 1): the `products.gtin` first-barcoded rule is intentional
  and needs no change.** `extract_gtin` (`agent/app/services/catalog.py`) returns the first barcoded
  variant in `variants_json` order — verified accurate, kept as-is. The trigger ("decide before
  Phase 3, the Optimizer relies on product-level GTIN for grounding") is void: `products.gtin` is a
  convenience projection, **not the source of truth** (so documented in `catalog.py` and
  `models/product.py`). The audit rubric and the Optimizer both read the **variant barcode**
  (`extract_gtin(variants_json)`) as the single source of truth, and GTIN is gated on product class
  (third-party equipment, not self-roasted coffee). No product-level selection rule needs deciding.
  _Raised: 2026-07-15 (Phase 1 closeout); resolved: 2026-07-22._

## Extractor

- **`verbatim` is DISCARDED, not stored — and returns the whole answer.** Two problems, corrected
  here: (1) the extractor model populates each `ExtractedBrand.verbatim` with the **full answer
  text** rather than the brand's local snippet; and (2) the Extractor does **not persist verbatim at
  all** — `cited_brands_json` stores only `{rank, brand, product}`
  (`agent/app/graph/extractor.py`), and there is **no `engine_runs.verbatim` column**. Harmless to
  grounding today — `_is_grounded` matches on the brand **name**, not `verbatim`. But consuming
  verbatim later (e.g. an answer-snippet evidence join) requires BOTH persisting it AND narrowing it
  to the brand's local sentence/window. _Raised: 2026-07-17; corrected: 2026-07-22._

## Optimizer

- **FALSE "ABSENT" TO-DO — blocker, fix before Step 4.** The Optimizer emits a merchant to-do
  claiming a spec is absent when extraction flakily misses a spec the deterministic audit
  classified `unstructured`. Run 839: `spec:origin` "No origin stated in any source field" on
  product 113, title *"Ethiopia Yirgacheffe 340 g"* — the to-do contradicts both the audit's own
  `unstructured` classification and the literal title. **Root cause:** positive claims (fills) are
  grounded by literal-presence; negative claims (absence to-dos) are not. **Fix:** no "absent" to-do
  may contradict the audit — mirror the positive guard with a negative literal-presence check keyed
  on `SPEC_VOCABULARY`; when the audit says `unstructured`, route an ungrounded target to the
  description path (keeping the gap→row accounting invariant satisfied), never to an absence to-do.
  A false to-do survives the approval gate (it looks like advice), so it must not reach a merchant.
  Also logged: `gpt-5-nano`/`minimal` is flaky on title-resident extraction — the reliability bar
  rose once misses became merchant-facing to-dos. _Raised: 2026-07-27 (Phase 3 step 2d acceptance,
  run 839)._

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

- **RESOLVED (2026-07-21, Gate F): end-to-end webhook HMAC delivery is verified live.** Proven
  against the dev store (commit `f0b97bc`): a bogus-HMAC POST → **401**; a real product edit →
  `products/update` **200** + `public.products` written; a real uninstall → `app/uninstalled`
  **200** + `status = uninstalled`; a real reinstall → re-ingest **20/20**. The topic-dispatch bug
  found in the process (dispatching on the REST form instead of the library-canonical
  `PRODUCTS_UPDATE`) was fixed in the same commit. _Raised: 2026-07-16; resolved: 2026-07-21._

## Compliance

- **Mandatory compliance webhooks are still commented out.** `customers/data_request`,
  `customers/redact`, and `shop/redact` are not yet subscribed in `app/shopify.app.toml`. Required
  for App Store submission (Phase 5); implement and verify before submitting.
  _Raised: 2026-07-16 (Phase 1 closeout, carried from the 2026-07-12 connect log)._
