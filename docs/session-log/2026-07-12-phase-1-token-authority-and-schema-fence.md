# 2026-07-12 — Phase 1: token authority, schema fence, and three bugs

**Branch:** `phase-1-connect`
**Follows:** `2026-07-12-phase-1-connect.md`

---

## State of Phase 1 — read this first

**Phase 1 code is complete and unit-tested. It is NOT verified against a real Shopify store.**

`shopify app config link` has still not been run: `shopify.app.toml` has `client_id = ""`.
Nothing in this phase has ever spoken to Shopify. That means the following are **UNPROVEN
end to end**, no matter how green the suite is:

- OAuth / install flow
- Webhook HMAC verification
- Catalog ingestion against a real catalog
- The whole token path (refresh, rotation, `reauth_required`)

Runbook gates **A, D, E, F are outstanding.** Every Shopify interaction in the test suite is
faked. Do not read "54 tests passing" as "it works".

**Tests:** 21 vitest + 33 pytest passing; ruff + eslint clean; `npm run build` succeeds.

---

## Three bugs found, and why each mattered

### 1. `version_table_schema` made Alembic try to drop its own history

Setting `version_table_schema="public"` in `alembic/env.py` — which looked like tightening the
schema fence — made Alembic compare its bookkeeping table against the reflected table's `None`
schema. The self-exclusion missed, and autogenerate emitted `op.drop_table('alembic_version')`.

Running that migration would have destroyed the migration history. Caught by the drift check
(autogenerate must produce an empty diff), which is exactly why that check exists. Fixed by
removing the setting and excluding `alembic_version` **by name** in `include_object`.

### 2. A dead refresh chain was being retried forever

`unauthenticated.admin()` refreshes via the library's `helpers/refresh-token.js`, which rethrows
**only** `InvalidJwtError` and `HttpResponseError(400, "invalid_subject_token")`. Everything else
— including the 400 `invalid_grant` that an OAuth token endpoint returns for an expired or
already-rotated refresh token — is flattened into an anonymous `throw new Response(500)`.

So a permanently dead refresh chain was indistinguishable from a network blip. It mapped to 502
→ `TokenFetchError` → transient → retried forever. A shop idle 90+ days would **never** reach
`reauth_required` and would never be surfaced to the merchant.

Fixed: `app/lib/admin-token.server.ts` performs the refresh itself so the real OAuth error
survives, and classifies it. Both permanent cases now return the same **404**.

### 3. A mock hid a property that does not exist

The first fix for (2) called `shopify.api`. **`shopifyApp()`'s returned object has no `api` key** —
it would have been `undefined` at runtime and crashed in production. **All 17 vitest tests passed
anyway**, because they mocked `../app/shopify.server`.

Caught by typecheck, then confirmed by a no-mock smoke test importing the real module. This is now
a standing rule in CLAUDE.md: anything touching `@shopify/shopify-api` needs a no-mock test.
Green tests over a mock are not evidence.

---

## Token rotation is now serialized per shop

Rotating a shop's offline token retires the previous token and invalidates its refresh token
immediately. Concurrent rotations therefore do not merely duplicate work — they **break the chain
and force a reinstall**. Nothing prevented that: ten concurrent calls to the token route issued
ten refreshes, nine of them destructive. (Verified by removing the lock and watching the test go
to ten.)

`withShopRefreshLock()` takes a Postgres advisory lock keyed on the shop, held for the enclosing
transaction — advisory rather than in-process because the app shell runs as multiple processes,
and xact-scoped so it releases even if a process dies mid-refresh. `getAdminToken()` re-reads the
session *after* acquiring the lock, so a caller that waited observes someone else's rotation
instead of issuing its own.

**Audit of every library path that can rotate a token:**

| Path | Mechanism | Status |
|---|---|---|
| `getAdminToken()` | `refresh_token` grant | **Ours. Under the lock.** |
| `authenticate.webhook()` | `refresh_token` grant via `ensureValidOfflineSession` | **Was a second authority.** Now under the same lock (`lib/webhook-auth.server.ts`). |
| `unauthenticated.admin/storefront` | `refresh_token` grant | Unused. Re-export **removed** so it cannot be reintroduced. |
| `authenticate.admin()` | **token exchange** (mints a new offline token) | **Known residual — outside the lock.** |
| `authenticate.flow` / `fulfillmentService` / `public.appProxy` | `refresh_token` grant | Not used by us. Do not adopt without the lock. |

The `authenticate.webhook` finding was the real one: webhooks arrive asynchronously with **no
merchant present**, concurrently with the agent's headless jobs hitting the token route. That
raced in production conditions we will actually hit.

`authenticate.admin` is left alone deliberately: it rotates by token exchange rather than the
refresh grant, it is Shopify's sanctioned embedded path, and it only runs with a merchant
present. Documented, not fixed. Do not add more paths.

---

## Known debt

- **`npm run typecheck` has 2 pre-existing errors** in `app/routes/app.tsx`
  (`Property 's-app-nav' does not exist on type 'JSX.IntrinsicElements'`). Upstream template
  code, untouched by us; surfaced only because nothing had ever run typecheck.
  **Typecheck is currently OUT of CI** as a result. **Fix this first thing in Phase 2 and put
  typecheck back into CI.** It is the only reason bug (3) was catchable at all.

## Deferred (deliberate, not forgotten)

- **Compliance webhooks** (`customers/data_request`, `customers/redact`, `shop/redact`) remain
  commented out in `shopify.app.toml`. Required for App Store submission → **Phase 5**.
- **`TokenProvider` does not short-circuit on `shops.status`.** By design. A shop flagged
  `reauth_required` will still hit the app shell once per job before failing. The Phase 2
  scheduler filters on `status = 'active'` instead, which is the right place for it — the
  token layer should not encode scheduling policy.

## Next session

1. **Run the runbook gates against the dev store** (`shopify app config link` → `shopify app dev`
   → install → gates A, D, E, F). Nothing here is trusted until this is done.
2. Fix the `s-app-nav` typecheck errors and restore typecheck to CI.
3. Then start **Phase 2 (Audit)** — query panel, EngineRunner, Extractor, ShareOfModel,
   read-only report UI.
