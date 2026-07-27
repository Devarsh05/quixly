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

- **`gpt-5-nano` / `reasoning_effort=minimal` is flaky on title-resident attribute extraction.**
  The Optimizer's extraction client misses specs that are literally in the product title — run 839
  returned nothing for `origin` on *"Ethiopia Yirgacheffe 340 g"*. Step 2e made the pipeline
  **robust to** this (deterministic recovery + the negative grounding guard, so a miss can no longer
  become a false merchant-facing claim), but it did **not** fix the flakiness itself: a miss still
  costs the *taxonomy* fill wherever the deterministic scanner cannot recover a value — notably
  `tasting_notes` (open-vocabulary, never recovered) and any value needing sentence context rather
  than a token. Measured worst case: with extraction returning nothing at all, 29 of the catalog's
  families recover deterministically and the rest degrade to honest to-dos. Candidate work: raise
  `reasoning_effort`, retry-on-empty for families the audit says are `unstructured`, or a
  larger model for the extraction step — decide before Phase 4 sets a quality bar on fill rate.
  _Raised: 2026-07-27 (Phase 3 step 2e; split out of the resolved Optimizer false-to-do entry)._

- **`verbatim` is DISCARDED, not stored — and returns the whole answer.** Two problems, corrected
  here: (1) the extractor model populates each `ExtractedBrand.verbatim` with the **full answer
  text** rather than the brand's local snippet; and (2) the Extractor does **not persist verbatim at
  all** — `cited_brands_json` stores only `{rank, brand, product}`
  (`agent/app/graph/extractor.py`), and there is **no `engine_runs.verbatim` column**. Harmless to
  grounding today — `_is_grounded` matches on the brand **name**, not `verbatim`. But consuming
  verbatim later (e.g. an answer-snippet evidence join) requires BOTH persisting it AND narrowing it
  to the brand's local sentence/window. _Raised: 2026-07-17; corrected: 2026-07-22._

## Optimizer

- **RESOLVED (2026-07-27, Phase 3 step 2e): the false "absent" to-do is fixed; negative claims are
  now grounded.** The Optimizer emitted merchant to-dos claiming a spec was absent whenever
  extraction flakily missed it — e.g. run 839 `spec:origin` "No origin stated in any source field"
  on product 113, title *"Ethiopia Yirgacheffe 340 g"*, contradicting both the literal title and the
  audit's own `unstructured` classification. **Root cause:** positive claims (fills) were grounded
  by literal presence; negative claims were not — and extraction returning nothing is not evidence
  of absence. The bug was **systemic, not a one-off**: re-checking run 839's persisted rows found
  **14 false absence claims across 9 products and 6 families** (origin ×8, brew_method ×2,
  process, roast_level, coffee_product_form).
  **Fix (three parts, all Optimizer-side — the audit was right and is unchanged):**
  (1) deterministic **recovery** (`audit_rubric.recover_spec_value`) reads the value straight out of
  source with no LLM before any family is written off; (2) a **negative literal-presence guard**
  permits the absence claim only when no `SPEC_VOCABULARY` token is in ANY source field **and** the
  audit did not classify the family `unstructured`; (3) a third truthful to-do tier
  (`mentioned_no_value`) for "mentioned but no value readable", which carries its evidence. The
  gap→row accounting invariant is unchanged — recovery only moves a family between existing routes.
  **Evidence:** run 873 (real LLM) → 0 false claims of 69; and with a stub extractor grounding
  *nothing at all* on all 18 products, 29 families still recover and false claims stay **0**.
  _Raised: 2026-07-27 (step 2d acceptance, run 839); resolved: 2026-07-27._

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
