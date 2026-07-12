# 2026-07-12 — Phase 1: Connect

**Branch:** `phase-1-connect`
**Goal:** PRD §15 Phase 1 — Shopify OAuth, embedded app in the dev store, session storage on
Postgres, catalog ingestion into `products`, webhooks.

---

## Key decisions

### 1. The agent stores no Shopify credential (supersedes PRD §8)

The plan originally called for encrypting the offline access token at rest (Fernet) in
`shops.access_token_ref`. That design does not survive the template as it actually ships.

`app/app/shopify.server.ts` sets `future.expiringOfflineAccessTokens: true`. Verified in the
installed library source (`@shopify/shopify-app-react-router@1.2.1`):

```
unauthenticated.admin(shop)
  └─ helpers/ensure-valid-offline-session.js
       └─ helpers/ensure-offline-token-is-not-expired.js
            → refreshes within 5 min of expiry, then sessionStorage.storeSession(rotated)
```

Offline tokens live ~60 minutes, and minting a new one **retires the previous token and
invalidates its refresh token immediately**. So there can be exactly **one refresh authority**.
The app shell already is one. A token copy pushed to the agent during a merchant visit would be
dead long before the next headless weekly scan — which is the core product.

**Resolution:** dropped `access_token_ref` and the Fernet requirement entirely.

- `app/`: new internal route `POST /internal/shops/:shop/admin-token`, shared-secret guarded,
  returns a short-lived token from `unauthenticated.admin()`.
- `agent/`: one `TokenProvider` chokepoint. Redis-cached (TTL = expiry − 5 min), refetch-once on
  401. Every Admin API call goes through it.
- PRD §7, §8, §9 and CLAUDE.md updated to match.

Also split token errors into **permanent** (`TokenUnavailableError`, only on a 404 → shop flagged
`reauth_required`) vs **transient** (`TokenFetchError` → retried). Without this, one blip from the
app shell would permanently brand a healthy shop as needing re-auth.

### 2. Prisma and Alembic share a database, fenced by schema

`schema.prisma` hardcoded `url = "file:dev.sqlite"` — it never read `DATABASE_URL` at all.
Repointed at Postgres with `?schema=shopify`, and the SQLite-flavoured migration was deleted and
regenerated.

| Schema | Owner | Contents |
|---|---|---|
| `shopify` | Prisma (`app/`) | `Session`, `_prisma_migrations` |
| `public` | Alembic (`agent/`) | `shops`, `products`, `ingest_runs` |

Both drift-detectors had to be fenced, not just Prisma's. **This caught a real bug:** the first
autogenerate run emitted `op.drop_table('alembic_version')` — Alembic's built-in self-exclusion
does not fire in this version once `version_table_schema` is set explicitly. Pinning
`version_table_schema="public"` makes Alembic compare it against the reflected table's `None`
schema, the exclusion misses, and it tries to drop its own migration history. Fixed by removing
that setting and excluding the version table by name in `include_object`.

### 3. `ingest_runs`, not `agent_runs`

PRD §8's `agent_runs(node_logs_json, tokens, model, …)` is shaped for LangGraph node execution
(Phase 2). Ingest progress is a different concern; welding them together would give both a bad
schema. `ingest_runs` carries a `cursor`, so a run that dies at SKU 1,900 leaves 1,900 rows and a
resumable position rather than an empty table.

---

## Verification (all run, all passing)

- `alembic upgrade head` → `downgrade base` → `upgrade head`: migration is reversible.
- **Schema fence proven:** with `shopify.Session` physically present,
  `alembic revision --autogenerate` produces an **empty** upgrade — no `DROP` against Prisma's
  table. (This is the check that caught the `alembic_version` bug above.)
- `psql \dt`: `shopify` holds Session + _prisma_migrations; `public` holds shops, products,
  ingest_runs, alembic_version. Clean separation.
- agent: **31 pytest passing**, `ruff check` clean. Covers internal-key rejection (missing, wrong,
  prefix-of-correct, fail-closed-when-unset), TokenProvider cache reuse / force-refresh / 401
  retry-once / permanent-vs-transient, **token never appears in logs or error messages**,
  `/shops/connect` double-call idempotency, and ingest upsert / resume-from-cursor / reauth-flagging.
- app: **7 vitest passing**, `npm run lint` clean, `npm run build` succeeds.

## Open items

- **`shopify app config link` + `shopify app dev` are still outstanding** — they need an
  interactive Partner-account login. `shopify.app.toml` still has `client_id = ""`. Until then the
  embedded app has not been loaded against a real dev store, and the live OAuth → ingest → webhook
  path is unverified end-to-end. Everything below that line is tested.
- `npm run typecheck` reports two **pre-existing** errors in `app/routes/app.tsx`
  (`Property 's-app-nav' does not exist on type 'JSX.IntrinsicElements'`) — untouched template
  code, surfaced only because nothing ran typecheck before. Left alone as out of Phase 1 scope.
  CI runs lint/test/build, not typecheck.
- Compliance webhooks (`customers/data_request`, `customers/redact`, `shop/redact`) are still
  commented out in `shopify.app.toml`. Required for App Store submission (Phase 5).
- CI gained Postgres + Redis service containers; it had none, so any DB-backed test would have
  failed there.
