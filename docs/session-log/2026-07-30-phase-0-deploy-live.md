# 2026-07-30 — Phase 0 deploy, Stages B–F: the stack is LIVE

Stage A (2026-07-29) built the images and fixed the pre-flight problems but deployed nothing.
This session provisioned the hosting, cut the URLs over, and **live-verified the deployed stack
end-to-end: 20 products ingested from the dev store into hosted Postgres, through the hosted
agent, on a hosted token fetch.** Phase 0's deploy checkbox is closed.

Three separate failures cost most of the session, and all three were configuration, not code.
Each has a section below written so the next deploy does not repeat it: the **service-naming
trap**, the **PgBouncer silent-write failure**, and the **two-active-Shopify-secrets trap**.

No application code changed. `app/shopify.app.toml` changed (URL cutover, Stage E) and is
called out at the end.

---

## The deployed stack

**Compute — Northflank**, project `quixly`, region **US-Central (Council Bluffs)**, free tier.
Two services, **`replicas = 1` on both, which is a hard rule, not a default**:

| Northflank service | what it actually runs | port | visibility |
|---|---|---|---|
| **`quixly`** | the **AGENT** — uvicorn + arq under `docker-entrypoint.sh` | 8000 | **private** |
| **`quixly-app`** | the **APP SHELL** — React Router / Node | 3000 | **public** |

Public URL (app shell): `https://p01--quixly-app--5x5p4hmrvgxk.code.run`

`replicas = 1` is load-bearing on the agent side for the reason Stage A recorded: `poll_delay = 15`
sizes arq's idle Redis command rate against a 500K/month free ceiling, and that cost is **per
process** — a second replica doubles it straight through, and buys nothing, since the jobs are
keyed per shop rather than sharded.

**Database — Neon**, project `quixly`, branch `production`, database `neondb`, region
**us-east-2**. Free tier: 0.5 GB storage, 100 CU-hours/month, **5-minute autosuspend**.
`pgvector` is *available* on Neon but is **NOT enabled** — no vector column exists yet. When
Phase 5+ needs one, enable it in an **Alembic migration**, never by hand in `psql` or the web SQL
editor; a manually-created extension is invisible to the migration history and will not exist in
the next environment.

**Redis — Upstash**, `rediss://` (TLS), **db 0 only** (Upstash exposes a single logical database;
nothing may assume db-index separation).

---

## Internal wiring — both values were wrong at first, for the same reason

This is the **service-naming trap**, and it is the single highest-value thing in this document.

**The agent service is named `quixly`, not `quixly-agent`.** Its internal DNS name is therefore
`quixly:8000`. The app shell — the thing that *sounds* like it should be `quixly` — is
`quixly-app:3000`. The names are the reverse of what the code layout suggests, and both
directions of the internal API got it wrong on the first pass.

| variable | service it is set on | correct value |
|---|---|---|
| `AGENT_SERVICE_URL` | `quixly-app` (app → agent) | `http://quixly:8000` |
| `APP_SHELL_URL` | `quixly` (agent → app) | `http://quixly-app:3000` |

**The failure `APP_SHELL_URL` produced.** It was set to `http://quixly:3000` — the right port,
the wrong service — which pointed **the agent at itself**. Nothing listens on 3000 inside the
agent container, so `TokenProvider` could not reach `/internal/shops/:shop/admin-token`:
`TokenFetchError` / `ConnectTimeout`, surfacing to the user as ingest failing with
**"Could not reach the app shell."** The fix was purely the service name.

Worth internalising: **a wrong internal hostname here does not look like a networking error, it
looks like a token-custody error.** The agent stores no token by design and fetches one per task,
so every internal-DNS mistake arrives disguised as a `TokenFetchError`. Check the hostname before
suspecting anything in the custody path.

`INTERNAL_API_KEY` is set **byte-identical on both services** and was **rotated to a fresh
production value** — the Stage A leak baked the old one into an image layer, and a layer keeps
what it was given forever. Header is `X-Internal-Api-Key` (`agent/app/api/deps.py`).

---

## Connection strings — the split is per-tool and is not optional

One Neon database, **two different endpoints and two different URL grammars**, because Prisma and
asyncpg need opposite things.

**App shell (Prisma) — POOLED host (`-pooler`), and `pgbouncer=true` is MANDATORY:**

```
postgresql://…@ep-…-pooler.us-east-2.aws.neon.tech/neondb
  ?schema=shopify&sslmode=require&pgbouncer=true&connection_limit=10&pool_timeout=3
```

**Without `pgbouncer=true`, Prisma reads work fine and session WRITES fail silently** against
Neon's PgBouncer endpoint. The observable symptom is **an OAuth 401 loop**: install completes,
the session never persists, every subsequent request re-authenticates. No database error is
raised or logged anywhere — the write simply does not land. **This cost hours.** The flag makes
Prisma stop using prepared statements, which is what PgBouncer's transaction pooling cannot carry
across.

Note the two failure modes it shares with the Shopify-secret trap below: **a 401 loop with no DB
error is ambiguous between "session not stored" and "session token not verifiable."** Both were
live at once today. Distinguish them by reading `shopify.Session` directly — if the row is
absent, it is this bug; if the row is present with a token, it is the secret bug.

`connection_limit=10` and `pool_timeout=3` carry forward from Stage A item 6 and are unchanged in
meaning: raise the pool above the concurrency the advisory-lock path needs, and fail fast inside
Shopify's 5-second webhook budget rather than hanging.

**Agent (asyncpg + Alembic) — DIRECT host (no `-pooler`), `+asyncpg` scheme, and NO query
parameters at all:**

```
postgresql+asyncpg://…@ep-….us-east-2.aws.neon.tech/neondb
DATABASE_SSL=require
PGSSLMODE=require
```

TLS is env-driven for the reason Stage A measured: `?sslmode=` breaks asyncpg, `?ssl=` breaks
psycopg, and `alembic/env.py` derives its sync URL by swapping `+asyncpg` → `+psycopg`, carrying
any query string across to the driver that cannot read it. So TLS lives in `DATABASE_SSL`
(asyncpg, via `connect_args` in `app/db.py`) and `PGSSLMODE` (the libpq/psycopg leg). Defaults are
`prefer` so local and CI keep working against a container with `ssl = off`; **both must be set to
`require` in the deployed environment** — that is a provisioning step, not a default.

**`psycopg` (v3) must be installed in the agent image** for Alembic's sync leg. Without it
`alembic upgrade head` fails at entrypoint, and — correctly, per the entrypoint's own contract —
takes the whole container down rather than serving without migrations.

---

## Shopify auth — the two-secret trap

**Rotating the client secret in the Partners dashboard does not replace the old secret; it creates
a New one alongside the Old, and BOTH stay active.** That is the whole trap.

**Shopify signs session tokens with the secret the *currently released app version* was created
under.** The live app version dated **2026-07-14**, before the rotation, so Shopify was signing
with **Old**. The deployed app shell was configured with **New**. Every session-token HMAC
verification failed → **401 loop, with no database error of any kind** (the sessions were being
written correctly by then; nothing in the DB was wrong).

**Fix:** `SHOPIFY_API_SECRET` on `quixly-app` is set to the **OLD** secret.

**STANDING CONSTRAINT — do NOT revoke the Old secret.** Authentication currently depends on it.
The post-Stage-F cleanup task "revoke the old secret" that was planned earlier is **CANCELLED**.

Migrating to New is a **separate, deliberate, ordered task** and has not been done: release a new
app version *under* New (`shopify app deploy`), confirm the release is live, *then* swap
`SHOPIFY_API_SECRET` on `quixly-app`. Doing the swap first reproduces exactly today's 401 loop.
Carried to `docs/backlog.md`.

Scopes were **unchanged throughout the cutover**. Because scopes are owned by Shopify managed
installation and declared in `app/shopify.app.toml`, `shopify app deploy` released a **URL-only**
change and **no merchant consent prompt appeared** — the desired outcome, and confirmation that a
URL cutover is not a consent event. `SHOPIFY_APP_URL` on `quixly-app` must equal
`application_url` in the toml; they are two copies of one fact.

---

## URL cutover (Stage E)

`app/shopify.app.toml`:

- `application_url` → `https://p01--quixly-app--5x5p4hmrvgxk.code.run`
- `redirect_urls` → `https://p01--quixly-app--5x5p4hmrvgxk.code.run/auth/callback`
- `automatically_update_urls_on_dev = false` (a dev run must not stomp the deployed URL)

**The callback path was wrong in the tree and had to be corrected.** It still carried the
template placeholder `…/api/auth`; the app's real callback is **`/auth/callback`**, because
`authPathPrefix` is `/auth`. A stale `/api/auth` redirect URL fails OAuth at the redirect step
with a mismatch error rather than anything that names the path.

**URLs are file-managed.** For a managed-installation app the Partners dashboard does **not**
expose `application_url` / `redirect_urls` as editable fields — the toml is the source of truth
and `shopify app deploy` is the only push mechanism. Do not go looking for the dashboard fields;
they are not there.

---

## Workstation environment gotchas (they cost real time and are not obvious)

- **York/campus network blocks outbound port 5432.** Direct `psql`, `prisma`, or `alembic`
  against Neon from campus fails with **`P1001` / connection timeout** — which reads exactly like
  a wrong password or a dead endpoint. Two workarounds: **phone hotspot** for direct DB access,
  or **Neon's web SQL Editor**, which is HTTPS and works from anywhere. This only affects the
  workstation — once deployed, the app connects from Northflank, where 5432 is open.
- **A stale `$env:DATABASE_URL` in a PowerShell session silently overrides `.env`.** It broke
  Alembic by handing it the wrong driver scheme, with a failure that pointed at the code rather
  than the environment. **Clear session env vars between steps**; do not assume a fresh `.env`
  read.

---

## Verification evidence — what actually proves it works

The claim is "the deployed stack ingested a real catalog," and it rests on three independent
observations, not on the UI's success banner alone:

1. **Session persisted to Neon.** `shopify.Session` holds one **offline** row for the dev store
   with `has_token = t` — so OAuth completed *and* the write landed through PgBouncer. This is
   the check that distinguishes the PgBouncer bug from the secret bug.
2. **The agent fetched a token from the app shell across Northflank's internal network.**
   `POST /internal/shops/…/admin-token` → **200** with an `access_token` in the body. This
   exercises `AGENT_SERVICE_URL`, `APP_SHELL_URL`, `INTERNAL_API_KEY`, and the whole custody path
   in one call — and it is what was failing while `APP_SHELL_URL` pointed at `quixly:3000`.
3. **The catalog landed in hosted Postgres.** `ingest_runs` row `completed`;
   `select count(*) from public.products` = **20**; the UI reported **"Catalog imported: 20
   products."** Same 20/20 as local, against an entirely hosted stack.

---

## Carried to `docs/backlog.md`

- **Migrate `SHOPIFY_API_SECRET` Old → New, deliberately.** Currently pinned to **Old**, and Old
  **cannot be revoked** until this is done. Order: release an app version under New, confirm,
  then swap the env var.
- **Rotate the local Postgres password.** Local hygiene from the Stage A leak. The image that
  baked the credential was confirmed **local-only and never pushed to any registry**, so this is
  low urgency — but the credential is in a layer permanently.
- **`host-key.pem` — CLOSED, nothing to rotate.** It was a transient Shopify-CLI/ngrok artifact,
  never in the tree, and already covered by `.dockerignore`. Recorded here so it is not
  re-raised.
- **Delete `D:\quixly-deploy-secrets.txt`** once every value in it is confirmed present in
  Northflank.

---

## Deliberately not done

- **No code change.** Docs and `app/shopify.app.toml` only.
- **No scope change, no consent prompt** — deliberate; the cutover was URL-only.
- **`pgvector` not enabled** on Neon. No vector column exists; enabling it belongs to the
  migration that first needs one.
- **The Old → New secret migration** — a separate task, not folded into the cutover.
- **The settled Verifier re-measurement** is still clock-blocked until **2026-08-04**. It now
  runs against the **deployed** environment rather than local, which changes where it is invoked
  from but not the run's semantics — the panel, the baseline (run 137) and the measured set are
  properties of the shared database, and the database is the same one, migrated to Neon.

## Working-tree note

`app/shopify.app.toml` carries the Stage E cutover and is **committed** — separately from the docs,
in the follow-up commit to this log. Three changes: `application_url` and `redirect_urls` to the
`.code.run` host (with the callback path corrected from the template's `/api/auth` to the real
`/auth/callback`), and `automatically_update_urls_on_dev = false`.

It matters that this file is committed rather than left dirty: it is the **source of truth for the
released URLs**, so a fresh clone with the old contents would re-release the
`shopify.dev/apps/default-app-home` placeholder on the next `shopify app deploy` and break OAuth
for the live app.

**Consequence of `automatically_update_urls_on_dev = false`, worth knowing before the next dev
session:** `shopify app dev` will no longer rewrite `application_url` to the tunnel. Local dev
against live webhooks now needs the ngrok URL passed explicitly (`--tunnel-url=…`, per Commands in
`CLAUDE.md`) and a deliberate deploy to point Shopify back at the tunnel — the flag protects the
deployed URL from a dev run, in exchange for making the dev path manual.
