# 2026-07-16 — Phase 1: closeout (API alignment, lock deadlock, CI typecheck, visibility_state)

**Merged to `main` this cycle:** #1 `phase-1-connect`, #2 `ci-typecheck-gate`,
#3 `visibility-state-normalizer`. (This log written on `phase-1-closeout-docs`.)
**Goal:** land the Phase 1 correctness/consistency fixes and reconcile the notes-of-record
(`CLAUDE.md`, `docs/backlog.md`) against the code before Phase 2.

---

## Shopify API version aligned to 2026-07

The three services had drifted to three different Admin API versions. All now pinned to
**2026-07** (the latest *stable* release; 2026-10 was a release candidate we moved **off** of):

- **agent** — `app/settings.py` (`shopify_api_version = "2026-07"`), consumed by
  `services/shopify_admin.py` for the GraphQL URL; `agent/.env.example` documents it.
- **app shell** — `shopify.app.toml` (`api_version` and `[webhooks] api_version`),
  `app/shopify.server.ts` (×3, `ApiVersion.July26`), and codegen via `.graphqlrc.ts`
  (`ApiVersion.July26`).
- **webhooks** — `[webhooks] api_version = "2026-07"` in `shopify.app.toml`.

No `2025-10` / `2026-10` reference remains anywhere in source or config. Moving to 2026-07 also
made Shopify's `unlisted` product status reachable, which motivated the `visibility_state` work
below.

## Advisory-lock pool-starvation deadlock — fixed

`withShopRefreshLock` (`app/lib/shop-lock.server.ts`) holds one pooled connection for the whole
`pg_advisory_xact_lock` transaction. `refreshUnderLock` previously read/wrote the session through
the **global** Prisma client, borrowing a *second* pooled connection inside the lock. With N
concurrent same-shop refreshes and a Prisma pool ≤ N, every connection ends up held by a
transaction blocked on the advisory lock, and the lock winner cannot get the extra connection it
needs to finish — a deadlock (surfaces as a hang/timeout).

Fix (`admin-token.server.ts`): thread the transaction client into the callback and run the inner
re-read + persist through a **tx-bound** `PrismaSessionStorage(tx)`, so the critical section
borrows **zero** additional pool connections. Lock key, ordering, timeouts, and the 404/502
permanence mapping are unchanged.

**CI caught this; local passed** — and the mechanism is worth recording precisely, because the
notes had it slightly wrong. There is **no** `connection_limit=2` "constrained-pool test." What
actually happens: `tests/admin-token-rotation.test.ts` fires **CONCURRENCY = 10** concurrent
`getAdminToken` calls against a real advisory lock. CI's `DATABASE_URL` sets no `connection_limit`,
so Prisma uses its default pool (`num_cpus*2+1` ≈ **5** on a 2-vCPU GitHub runner) — 10 > 5, so the
pre-fix code deadlocked on CI. Dev machines with more cores get a larger default pool (≥ 10), so
10 ≤ pool and the same test passed locally. After the fix the test passes regardless of pool size.
That concurrency-10 rotation test (against CI's naturally small default pool) is the regression
guard — not an explicit pool cap.

## Internal admin-token test reworked onto a real DB harness

`internal-admin-token.test.ts` previously passed against a **mocked** session storage, which
returned an empty result and hid the fact that a refresh-path assertion never reached the refresh
at all — every "permanent failure" case was really just the empty-table 404. Now that the inner
I/O runs on the lock's real transaction connection, the suite runs against **real Postgres** with
only Shopify's HTTP refresh call faked:

- every refresh-path case **seeds a real session row** and asserts `refreshToken` was **actually
  called** (`toHaveBeenCalledTimes(1)`);
- the pre-refresh 404s (missing row, expired-with-no-refresh-token, refresh-token-expired) seed
  the exact triggering condition and assert `refreshToken` was **not** called;
- the load-bearing invariant is pinned: `invalid_grant` (dead chain) returns the **same 404** as a
  missing row, so the agent can't retry a 90-day-idle shop forever.

## TypeScript typecheck wired into CI (first time)

**PR #1 merged with no typecheck running in CI at all** — the pipeline was lint/test/build only.
PR #2 added `- run: npm run typecheck` (`react-router typegen && tsc --noEmit`) to the app job,
**unconditionally** (the root `ci.yml` never carried the upstream template's `javascript`-branch
skip). `<s-app-nav>` (App Bridge) is not covered by `@shopify/polaris-types`, so it's typed via an
ambient `JSX.IntrinsicElements` declaration in `app/app-bridge.d.ts`; without it `tsc` errors
TS2339 in `app/routes/app.tsx`.

## visibility_state normalization consolidated (+ unlisted)

Two write paths for `products.visibility_state` had diverged: the GraphQL ingest job normalized
Shopify's UPPERCASE `ProductStatus` via a local map and raised on unmapped values, while the
`products/update` webhook wrote `payload.get("status")` **raw and unvalidated**. Neither handled
`unlisted` (now reachable on 2026-07 — `UNLISTED` via GraphQL, `unlisted` via webhook); on ingest
it raised and killed the import, on webhook it was stored raw.

Consolidated to a single case-insensitive `normalize_visibility_state` in
`agent/app/services/catalog.py` (the module both writers already share), mapping
`active/draft/archived/unlisted` — `unlisted` kept as its **own distinct value**, not collapsed to
`active`, because "not in search/collections/recommendations" is exactly the discoverability signal
this product cares about. **Ingest** delegates and still raises on unknown values (a failed batch
surfaces the problem). **Webhook** routes status through it too, but on an unknown value **logs a
warning and omits `visibility_state`** from the update — preserving the prior value and applying
the other fields — so it never 500s into a Shopify retry storm. The old raw
`payload.get("status")` write is gone. This also fixed a latent bug where a missing `status` wrote
`NULL`. Code-only: the column is `VARCHAR(32)` nullable with no enum/CHECK, so no migration; the
Alembic drift check stayed empty.

## Connect-gate fixtures — still confirmed

Unchanged from the 2026-07-15 Connect gates: **20 products** (18 active / 2 draft), 25 variants
nested in `variants_json`. GTIN: **14 barcoded variants across 11 products** → 11 product-level
GTINs (9 products legitimately barcode-free). `extract_gtin` selects the **first barcoded** variant
in `variants_json` order — not index 0, and not guaranteed to be the primary/default variant (see
backlog).

## Deferred / residual — open going into Phase 2

- **Webhook-refresh pool-coupling deadlock (unfixed residual).** `authenticate.webhook()` →
  `ensureValidOfflineSession` refreshes through the library's global-client storage, which can't be
  pinned to the lock's transaction, so the webhook path keeps the exact deadlock the admin-token
  fix removed. Not independently test-covered. `docs/backlog.md` → Token custody.
- **GTIN selection rule: first-barcoded vs. primary/default variant** (Phase 3 / Optimizer, which
  relies on product-level GTIN for grounding). Selection-rule change only — every barcode is
  already in `variants_json`. `docs/backlog.md` → Ingest.
- **No ingest prune path for deleted store products** — upsert-only, so a product removed on the
  store leaves a stale `products` row. `docs/backlog.md` → Ingest.
- **End-to-end webhook HMAC delivery from Shopify** — untestable under `--use-localhost`; deferred
  to a tunnel session (HMAC verify + full `app/uninstalled` delivery chain).
- **`tsconfig.json` `baseUrl` deprecation** — the template's `compilerOptions.baseUrl` is deprecated
  ahead of TS 7. Not yet failing, but will need migrating (e.g. to `paths` without `baseUrl`).

## Verification

- app: `npm run lint`, `npm run typecheck`, `npm test`, `npm run build` — all green in CI.
- agent: `ruff check .`, `pytest` (51 passing incl. new `test_catalog.py`) — green.
- Alembic drift check after the visibility_state change: **empty** diff (no migration).
