# Quixly — Project Memory

Autonomous agent that gets Shopify merchants' products recommended by AI shopping
engines (ChatGPT, Google AI Mode, Perplexity, Copilot, Gemini), then verifies the
uplift. Full spec: see `PRD.md` (read it before large changes).

## Architecture at a glance
- `app/` — TypeScript, **Shopify React Router app template**. Handles OAuth, session storage,
  billing, webhooks, App Bridge + Polaris embedded UI. Thin Shopify-facing shell only.
- `agent/` — Python, **FastAPI + LangGraph**. The brain: engine querying, shopping-agent
  simulation, diagnosis, grounded optimization, publishing, verification. Async workers here.
- Postgres (+ pgvector) = primary store. Redis = queue/locks. Browserbase = browser sims.
- App shell ↔ agent service over an internal authenticated API. Agent also exposes an MCP server.
- Deploy: Railway (both services + Postgres + Redis).

## Commands
- Infra (both services): `docker compose up -d` (Postgres + Redis)
- App shell: `cd app && npm install`, `npm run dev`, `npm run build`, `npm run lint`
- **Dev with live webhooks (needs a stable public URL).** Bare `npm run dev` (`shopify app dev`)
  spawns a **rotating Cloudflare quick-tunnel** — a new URL every restart — so Shopify keeps
  delivering to the last-released `application_url` and webhooks never reach local. Use the
  reserved ngrok domain instead: start ngrok first (forwarding to `:3000`), then
  `shopify app dev --tunnel-url="https://debating-persuaded-patrol.ngrok-free.dev:3000"`.
  The app's dev port is pinned to **3001** in `app/shopify.web.toml` (kept local): with
  `--tunnel-url=…:3000` the CLI proxy binds 3000 and forwards to its own declared app port, so
  leaving the app on 3000 makes the proxy forward to itself. If that file is ever committed, keep
  the port at **3001**, not 3000.
- **Windows dev cleanup.** `Ctrl-C` on `shopify app dev` often orphans `node` processes that keep
  holding ports (3000/3001/3457), causing "port in use" bumps and a proxy self-forward connection
  storm on the next launch. Before relaunching, kill them: `taskkill /IM node.exe /F` (or the
  specific PIDs from `netstat -ano | findstr ":3000 :3001"`).
- Agent: `cd agent && uv sync` (or `pip install -e .`), `uvicorn app.main:app --reload`
- Tests: app `npm test` (vitest); agent `pytest` (needs Postgres up)
- Worker (agent): `arq app.worker.WorkerSettings`
- DB migrations: agent `alembic upgrade head`; app `npx prisma migrate dev` (Session only)
- **Alembic drift check** (run after ANY schema change, either side):
  `cd agent && uv run alembic revision --autogenerate -m "drift"` must produce an **EMPTY**
  diff while `shopify.Session` exists. If it emits a `DROP`, the schema fence is broken —
  fix it and delete the generated file. Never commit the drift-check migration.
- `npm run typecheck` (`react-router typegen && tsc --noEmit`) runs in the app CI job and must
  be green. `<s-app-nav>` (App Bridge) is typed via an ambient `JSX.IntrinsicElements`
  declaration in `app/app/app-bridge.d.ts` — `@shopify/polaris-types` covers the other `s-*`
  elements but not this one. Our `ci.yml` never carried the upstream template's
  `javascript`-branch typecheck skip; typecheck simply wasn't wired into CI until now.
  (Observed, not hypothetical.)

## Working rules
- **Plan first** for anything spanning multiple files, new agent nodes, DB schema changes,
  or Shopify Admin API writes. Show the plan; wait for approval before editing.
- Prefer existing service wrappers before adding new abstractions.
- Python: typed (pydantic) everywhere, structured LLM outputs, no bare LLM calls in routes.
- TS: keep the React Router app thin — business/agent logic belongs in `agent/`, not `app/`.

## Schema ownership (one Postgres database, two migration tools)
Both services share one database. Each owns exactly one schema, and **neither tool may
touch the other's** — they have independent, mutually-destructive drift detection.

| Schema | Owner | Contents |
|---|---|---|
| `shopify` | **Prisma** (`app/`) | `Session` + `_prisma_migrations`. **Nothing else, ever.** |
| `public`  | **Alembic** (`agent/`) | `shops`, `products`, `ingest_runs`, … everything else |

- The boundary is enforced on both sides. Prisma is scoped by `?schema=shopify` on the
  app's `DATABASE_URL`; Alembic is scoped by `include_object` in `agent/alembic/env.py`.
  Without those guards each tool sees the other's tables as drift and emits `DROP`s.
- **Never add a model to `app/prisma/schema.prisma`.** New tables are Alembic migrations
  in `agent/`. Prisma exists only because the Shopify session-storage adapter needs it.
- **Do NOT set `version_table_schema` in `agent/alembic/env.py`.** Pinning it makes Alembic
  compare its own bookkeeping table against the reflected table's `None` schema, its
  self-exclusion misses, and autogenerate emits `DROP TABLE alembic_version` — destroying
  the migration history. `alembic_version` is excluded **by name** in `include_object`
  instead. (Observed, not hypothetical.)
- After changing either schema, run the **drift check** (see Commands) and confirm the diff
  is **empty**. A non-empty diff means a guard is broken.
- `prisma migrate dev` is **LOCAL ONLY** — it can reset the database. Deployed
  environments run `prisma migrate deploy`.
- Same DB, two URL grammars: app uses `postgresql://`, agent uses `postgresql+asyncpg://`.

## Shopify token custody (single refresh authority, serialized per shop)
Offline access tokens expire (~60 min). Minting a new one **retires the previous token and
invalidates its refresh token immediately** — so two rotations racing don't lose an update,
they **break the chain and force the merchant to reinstall**.

- **`app/lib/admin-token.server.ts` is the SINGLE refresh authority.** It performs the
  refresh itself and is the only sanctioned way to obtain an admin token.
- **Refresh is serialized per shop** by a Postgres advisory lock (`lib/shop-lock.server.ts`),
  and session state is re-read *after* acquiring it. Advisory, not in-process: the app shell
  runs as multiple processes. **Adding a second refresh path anywhere — a webhook, a job, a
  route — races and invalidates the chain.**
- `authenticate.webhook()` **is** such a path (it refreshes via `ensureValidOfflineSession`),
  so webhook routes authenticate through `lib/webhook-auth.server.ts`, under the same lock.
  `unauthenticated.admin/storefront` also refresh and are deliberately **not re-exported**
  from `shopify.server.ts`. Known residual: `authenticate.admin()` rotates by *token
  exchange* and is outside the lock — merchant-present only; do not add more.
- Known residual: the **webhook refresh path is pool-coupled and can deadlock** under
  concurrent same-shop webhook refreshes. The lock holds one pooled connection for its whole
  `$transaction`; the admin-token path runs its inner session I/O on *that* connection, but
  `authenticate.webhook()` → `ensureValidOfflineSession` refreshes through the library's
  global-client storage, which can't be pinned to the tx. With a Prisma pool ≤ concurrent
  refreshes, the lock winner can't get the extra connection it needs and deadlocks. Fix
  pending (see `docs/backlog.md` → Token custody). Not hypothetical: the admin-token path had
  this exact deadlock (now fixed by tx-pinning), observed on CI where `admin-token-rotation.test.ts`
  fires 10 concurrent refreshes against the runner's small default Prisma pool (`num_cpus*2+1`);
  it masked on higher-core dev machines. There is no `connection_limit`-capped test, and the
  webhook residual itself is not independently test-covered.
- Do not disable `future.expiringOfflineAccessTokens` — public apps created after
  2026-04-01 must use it.
- The **agent stores no Shopify token or refresh token.** Ever. No `shops.access_token_ref`
  column (PRD §8 is superseded on this point). It pulls short-lived tokens from
  `POST /internal/shops/:shop/admin-token`.
- **All agent-side Admin API calls go through `TokenProvider`** (`agent/app/services/`).
  Never fetch or cache a token anywhere else.
- **Writes use the same custody path as reads — the invariant is a single refresh
  AUTHORITY, not a single *caller*.** The **agent** performs the Admin API write
  (`graph/publisher.py` → `ShopifyAdminClient` → `TokenProvider`), on a short-lived token
  minted by the app shell. A write is the same token as a read with a different verb: it
  adds no token path, no new internal endpoint, and the agent still stores nothing. Do
  **not** move the write into the shell — that either drags staleness gating, per-type
  verification and status transitions into the thin layer, or turns the shell into a
  GraphQL proxy re-implementing 401-retry and leaky-bucket handling: a second, untested
  copy of the riskiest client code, on the riskiest path.
- **`/internal/shops/:shop/admin-token` status codes are load-bearing.** The agent decides
  permanence from them, so they must not be loosened:
  - **404 = PERMANENT** → agent flags `reauth_required` and stops. Covers *both* "no session
    row" *and* a dead refresh chain (`invalid_grant` / expired `refreshTokenExpires`).
  - **502 = TRANSIENT** → agent retries. Shopify 5xx, network, throttling.
  Mapping a dead chain to 502 would retry a 90-day-idle shop forever and never surface it.
  Note `unauthenticated.admin()` cannot make this distinction — it flattens every OAuth
  error except `invalid_subject_token` into an anonymous 500 — which is why
  `app/lib/admin-token.server.ts` performs the refresh itself via `api.auth.refreshToken`.
- Jobs fetch tokens at the *start of the task*, never at enqueue time — a queued job can
  outlive a 60-minute token.
- Refresh tokens die after **90 days of disuse**. Weekly scans keep the chain warm; a shop
  idle 90+ days gets `shops.status = reauth_required` and must re-auth. Never let this
  degrade into a silent 401.

## Risk zones (extra care — explain before touching)
- **Never publish to a merchant's Shopify store without an explicit approval gate.**
  Publishing flows through `fixes.status = approved` only. The gate is `agent/app/api/fixes.py` +
  `app/app/routes/app.fixes.tsx`; its invariants (server-side approvability, 409 on non-`proposed`,
  404-not-403 ownership, one approved row per target) are in "Conventions" and are load-bearing for
  the Publisher. Do not add a second writer of `fixes.status` from a merchant action.
- **Never fabricate product attributes, specs, GTINs, or reviews.** Optimizer may only
  enrich/restructure from verified source data; every fix carries a before/after diff + source.
- **Category assignment is a publish-class write.** Assigning a Standard-taxonomy category (the
  `category` fix type) has tax/channel consequences and is approval-gated like a store publish —
  never bundled as a low-risk metafield. It is the precondition for every taxonomy attribute write.
- **Schema ownership.** `shopify` schema = Prisma (`Session` + `_prisma_migrations`).
  `public` = Alembic (everything else). **Neither tool may touch the other's.** Do NOT set
  `version_table_schema` in Alembic's `env.py` — it disables Alembic's self-exclusion and
  makes autogenerate emit `DROP TABLE alembic_version`; the table is excluded by name
  instead. Full detail in "Schema ownership" above.
- **Token authority.** The agent **NEVER** stores a Shopify token or refresh token.
  `app/lib/admin-token.server.ts` is the SINGLE refresh authority, and refresh is serialized
  per shop with a Postgres advisory lock. Adding a second refresh path anywhere (webhooks,
  jobs, routes) races and invalidates the chain. Permanent failures (no session row, dead
  refresh chain) → **404 → `reauth_required`**. Transient (5xx/network) → **502 → retry**.
  **Never conflate the two** — conflating them retries a dead shop forever, or brands a
  healthy one. Full detail in "Shopify token custody" above.
- Do not edit OAuth, session storage, billing, or webhook-verification code without first
  explaining the risk.
- Secrets: never hardcode or print API keys / Shopify tokens. Use env vars; keep local
  secrets in gitignored `.env` / `CLAUDE.local.md`. `INTERNAL_API_KEY` must match exactly
  across `app/.env` and `agent/.env`.
- The internal API (`/internal/*` on the app shell; `/shops`, `/webhooks` on the agent) is
  service-to-service and shared-secret authenticated. Never link it from the UI or expose
  it publicly.

## Verification
- After DB or route changes: run the relevant tests and `npm run build` / `uvicorn` boot check.
- **Mocks hide missing properties.** Any module touching `@shopify/shopify-api` needs a
  **no-mock smoke test that imports the real module** and asserts the properties it uses
  actually exist. A mocked `shopify.server` once let 17 tests pass green against
  `shopify.api`, a property that does not exist and would have been `undefined` in
  production. Green tests over a mock are not evidence.
- After a schema change (either side): run the Alembic drift check (see Commands).
- After any Optimizer change: run the grounding test suite (asserts no attribute is emitted
  that isn't present in source fixtures).
- After a Publisher change: run against the dev store only; re-read the published page and
  confirm it parses.

## Conventions
- Monorepo; keep `app/` and `agent/` independently runnable.
- Agent graph nodes live in `agent/app/graph/` — one file per node.
- **Optimizer write channels (step 2d) — `custom.*` is RETIRED as a write target.** The
  AI-legibility channels are exactly two: **taxonomy attributes** (`shopify` namespace,
  `list.metaobject_reference`) for the four **taxonomy-home** families — `coffee-roast`, `country`
  (origin), `coffee-product-form`, `decaffeination-method` — and the product **description** (the
  HTML-preserving append-list composer `_compose_description`) for every other grounded family.
  Never reintroduce a `custom.*` write unless the L5 Catalog-ingestion question (does Catalog read
  non-mapped `custom.*`?) resolves **positive** — see `docs/decisions/2c-write-target.md`.
- **Negative claims must be grounded (Optimizer).** A merchant to-do is an assertion about the
  product, and a false one survives the approval gate because it reads as advice, not a diff. So
  absence is guarded exactly like presence. An **"absent" to-do is permitted only when** no
  `SPEC_VOCABULARY` token for the family is literally present in **any** source field **and** the
  deterministic audit did not classify that family `unstructured` (`audits.gaps_json[].state`).
  **Extraction returning nothing is NOT evidence of absence** — it is routinely a flaky-LLM miss, so
  before any family is written off, `audit_rubric.recover_spec_value` tries to read the value
  straight out of source with no LLM. Recovery **proposes** into the unchanged `ground_attribute` +
  `validate_spec_value` guards — it is never a second, weaker path into a fix. A family that is
  mentioned but yields no readable value gets the truthful `mentioned_no_value` to-do tier (which
  carries its evidence), never an absence claim. The audit is the **authority for absence** and only
  ever raises the bar, never lowers it. **One vocabulary, no fork:** `SPEC_FAMILIES` is the single
  definition; `detect` (via `detect_hit`) answers presence, `values`/`kind` (via
  `validate_spec_value`) answer validity. Cross-family-ambiguous value phrases (today exactly
  `espresso`, a roast AND a brew method) are refused by recovery for both families — never trade a
  false negative for a false positive. (Observed: run 839 shipped 14 false absence claims across 9
  products before this guard.)
- **Gap→row accounting (Optimizer).** Every family in the Optimizer's `spec_targets` resolves to
  **exactly one** of {taxonomy metafield fill, description line, merchant to-do}, pairwise disjoint
  — asserted in `run_optimizer` before persist. A target producing **zero** rows is always a
  routing bug. Stated over `spec_targets` (extraction-based), **not** `audit.gaps_json` (the
  deterministic detect proxy, which is allowed to diverge from extraction).
- **The Optimizer is Shopify-free.** It makes no live Shopify calls. A taxonomy fix carries the
  canonical `TaxonomyValue` GID at propose-time; the **value → per-shop-metaobject-entry-GID**
  resolution is **publish-time** (the Publisher, Step 4). That resolution is currently **unproven
  and blocked** — see the metaobject-scope note below. Propose-time behaviour is unaffected: the
  canonical GID it carries is live-validated.
- **Taxonomy write mechanics.** A taxonomy attribute write needs a **per-shop metaobject-entry
  GID**, NOT the canonical `TaxonomyValue` GID (Shopify rejects the latter with `INVALID_VALUE`).
  The standard-taxonomy metaobject surface is invisible **even with `read_metaobjects` granted** —
  the blocker is `read_metaobject_definitions` (next bullet). Evidence:
  `docs/decisions/2c-write-target.md` (L1/L2/L6).
- **Audit coverage = two channel-specific numbers, never blended.** `audits.taxonomy_coverage`
  (headline machine-readable score, over applicable **taxonomy-home** families — 3, or 4 for a
  decaf product) and `audits.spec_coverage` (prose channel, over applicable spec families — 8, or 9
  for decaf). The old `structured_coverage` (which counted `custom.*`) is **DROPPED** (migration
  `c1f2a3b4d5e6`). Three-state classification (structured/unstructured/absent) is unchanged — only
  *which write makes a family `structured`* moved (taxonomy attribute, not `custom.*`).
- **Shopify scope set — GRANTED; the taxonomy write path is BLOCKED, and the blocker is a SCOPE,
  not a query shape.** `app/shopify.app.toml` declares `write_products + read_metaobjects +
  read_publications` (granted 2026-07-27; `read_products` is implied by `write_products`). Scopes
  are owned by **Shopify managed installation**, so the toml is the single source of truth —
  `app/app/shopify.server.ts` deliberately leaves `scopes` undefined and `SCOPES` unset; do not set
  either. Editing that line triggers a merchant consent on next app load.
  The two metaobject scopes are **distinct and both required**:
  - **`read_metaobjects`** (instance-level, **GRANTED**) reads metaobject **entries** — but only if
    you already know the `type` string.
  - **`read_metaobject_definitions`** (schema-level, **NOT granted**) is what enumerates
    `metaobjectDefinitions` and thus **discovers** that `type` string. Without it the definition
    surface returns `[]` *even with `read_metaobjects`*, so the `metaobjects` query cannot be
    aimed. Confirmed live after the grant (L6) — the surface was still empty.
  - **`standardMetaobjectDefinitionTemplates` does not exist on `QueryRoot` at API `2026-07`**
    despite current docs describing it, so the documented "enable a standard definition" route is
    unavailable at our pin.
  **Consequence:** the taxonomy metafield write path needs a **third merchant consent** (install →
  the 2026-07-27 grant → `read_metaobject_definitions`) **AND** proof the entry surface populates
  end-to-end before that consent is spent. **Deferred — see `docs/backlog.md`.** The canonical half
  IS proven: every GID in `agent/app/services/taxonomy_map.py` validates against the live
  `taxonomy` root (L6). `read_publications` does NOT resolve `shops.plan` (that comes off
  `shop { plan { … } }`); it is what makes publication state readable.
- **Channel discovery: use `resourcePublications`, NEVER `publications`.** The agentic channel
  (Microsoft Copilot) is an ACTIVE publication on the dev store with `autoPublish: true` and
  product 113 published to it — yet `publications(first:40)` returns **3** and **omits it under
  every `catalogType`**, and `catalogs` likewise omits its `AppCatalog`. Both collection queries
  agree with their own count fields (`publicationsCount` 3, `catalogsCount` 4) — they are
  *consistently* blind, so **no count-vs-list mismatch warns you**. **Any code that discovers
  channels by enumerating `publications` will silently conclude the agentic channel does not
  exist**, with no error raised. Discover channel membership via `product.resourcePublications`;
  resolve known channels by **direct ID**.
  **Never gate on a count field:** for product 113, `resourcePublications` returns **4 nodes
  (including Copilot)** while `resourcePublicationsCount` reports **3** — the count omits the
  agentic publication that the list itself returns. This matters most for **Phase 4's Verifier**.
  Evidence: L7.
- **Proven write paths for the Publisher (Step 4) — SHIPPED and live-verified (2026-07-28).**
  - **`category` fix** — a scalar `TaxonomyCategory` GID via `productUpdate`. **Proven writable**
    (L1, re-confirmed live L12); involves no metaobject, so it is *not* blocked. Still
    approval-gated as a publish-class write.
  - **`description` fix** — reaches all 9 families, needs **no extra scope**, and is the field the
    Copilot agentic channel reads. **This is the primary legibility channel**, not a fallback.
  - **taxonomy metafield fix** — **BLOCKED** on `read_metaobject_definitions` (above). Deferred.
  `shops.plan` stays NULL; its owner is the connect/ingest path, never a diagnostic (backlog).
- **Publisher (step 4) — the invariants that keep a merchant safe.** `agent/app/graph/publisher.py`
  is the ONLY code that writes to a merchant's store; `POST .../fixes/publish` is the only route
  that reaches it, and it takes **no fix ids** — the work set is exactly the shop's `approved` rows.
  - **A 200 is NOT a success; the Publisher is RE-READ-VERIFIED.** `productUpdate` returns HTTP
    200 with non-empty `userErrors` when it refuses, and a clean write may still not land.
    Nothing reaches `verified` without a **separate re-read** of the live product; the mutation's
    own return payload is never evidence. The re-read is per-type and specific — **description**:
    the Details block is present *and* the merchant's body survives intact; **category**: live
    `category.id` equals the GID written. A 200-but-wrong is **`publish_failed`, never
    `published`**, and `published_at` is set **only** on the confirmed re-read.
  - **Publish audit columns: one meaning per column.** `fixes.published_at` (TIMESTAMPTZ NULL) is
    set **only** on a confirmed re-read — it is the "went live at" anchor Phase 4's Verifier
    measures uplift against, so writing it anywhere else corrupts that measurement.
    `fixes.publish_error` (TEXT NULL) is why a write did not land — which write, and what Shopify
    returned — and is surfaced to the merchant. `fixes.reason` keeps pure grounding/to-do
    semantics and **the Publisher never writes it**. `FixStatus.publish_failed` is code-only (no
    enum in the DB, no migration). Migration `530075ef94b8` added the two columns.
  - **Shopify echoes `descriptionHtml` byte-for-byte** (L9), so exact equality is the real
    verification path and the re-read's structural fallback (merchant copy present + every
    appended `<li>` parsed) is a **safety net, not the routine path**. If it ever fires, the
    warning is a signal worth investigating, not expected noise.
  - **A refused write arrives in TWO shapes** (observed live, L10): a bad `TaxonomyCategory` GID is a
    **top-level GraphQL error** (`INVALID_PRODUCT_TAXONOMY_NODE_ID`), while a nonexistent product is
    **200 + `userErrors`**. Checking either alone misses the other. Any new write path must handle
    both; both must raise.
  - **Staleness is two hard layers, both before any write.** Layer 1 is exact per-fix `before_json`
    equality (live `descriptionHtml` / live `category.id`) — this is what stops an append onto a
    changed body and a category assigned over the merchant's own choice. Layer 2 is
    `base_source_hash` recomputed from the live read, catching drift in fields the fix *grounded on*
    but does not *write*.
  - **Double-append is structurally impossible.** An `approved` fix has exactly **two** paths out:
    live already equals `after_json` (the write landed before a crash → reconcile to `verified`,
    **no mutation**), or live still equals `before_json` (safe to write). A body matching
    **neither** is `stale`. This is why **reconciliation runs before the staleness gate** — the
    rewind stops a landed sibling from staling the fixes queued behind it. Never reorder these two
    steps. Proven live by the product's `updatedAt` **not moving** across a replay — *not* by body
    equality, since `after_json` is fixed at propose time and a re-write would produce identical
    bytes (identical bytes prove nothing).
  - **Invariant breaches abort the run, they are not skipped.** An approved `metafield`/
    `merchant_todo` row, or two approved rows on one `(product_id, target)`, mean the approval gate
    or its supersede failed — write nothing and fail loudly.
  - **Staged rollout is in code:** `PUBLISH_ALLOWED_SHOPS` defaults to the dev store alone and the
    job refuses anything else before reading. Widening it is a deliberate commit.
- **`base_source_hash` is a WRITER-STABLE projection, never a raw column hash.**
  `services.catalog.stable_source_hash` is the single definition. `products` has two writers that
  store `variants_json` in different shapes — the GraphQL ingest job and the REST `products/update`
  webhook — and our own publish fires that webhook moments later, so hashing the raw JSONB made the
  digest depend on which writer touched the row last and the staleness gate would have refused every
  write. The projection keeps only what both writers spell identically. `category` is deliberately
  excluded (assigning one is itself a fix). **`_build_source_fields` is a different thing** — it
  feeds extraction/grounding and must not be conflated with the hash. Generalises: any cross-writer
  comparison goes through a normalised projection. (Observed, not hypothetical — see L11.)
- **One node→row mapping.** `services.catalog.product_row_from_node` is shared by the ingest job and
  the Publisher's re-read, so the Publisher hashes exactly what ingest stored. Don't add a second.
- **Approval gate (step 3) — the invariants the Publisher must not break.** `agent/app/api/fixes.py`
  is the ONLY writer of `fixes.status` from a merchant decision. It makes **no Shopify calls** and
  writes **exactly one column**; approval is a status transition (`proposed → approved | rejected`),
  never a publish.
  - **Approvability is enforced server-side, not hidden in the UI.** `APPROVABLE_TYPES` is exactly
    `{description, category}`. A taxonomy `metafield` fix (blocked on `read_metaobject_definitions`)
    and a `merchant_todo` (no `after_json`) are refused with **409**, and the shell renders them
    with no approve control at all. The invariant is **no approvable path to a write that cannot
    execute** — when Step 4 unblocks a fix type, widen `APPROVABLE_TYPES`, never the UI alone.
  - **Non-`proposed` states 409; they are never a silent no-op.** Repeating the *same* decision is
    idempotent, anything else conflicts. A silent success would hide exactly the double-submit and
    stale-UI bugs the gate exists to catch.
  - **Ownership is a join, and a mismatch is 404, not 403** — `fixes → products → shops`, so fix ids
    cannot be probed for existence across shops. Never look a fix up by bare id.
  - **At most ONE approved fix per `(product_id, target)`.** Approving marks the other `proposed` /
    `approved` rows on that target `stale`, because the description composer **appends** — two
    approved rows for one body would append the Details block twice.
  - **A new run never touches `approved` / `rejected` rows from an earlier run.** Those are merchant
    decisions; re-running the Optimizer must not silently drop consent.
- **`products.visibility_state` has ONE normalizer** — `normalize_visibility_state` in
  `agent/app/services/catalog.py`, used by BOTH writers (the ingest job and the `products/update`
  webhook). Case-insensitive (GraphQL yields UPPERCASE, webhooks lowercase); maps
  `active/draft/archived/unlisted`, with `unlisted` a distinct value never collapsed to `active`.
  Ingest raises on an unknown status; the webhook logs and keeps the prior value (a raising
  webhook becomes a 500 → Shopify retry storm). Column stays `VARCHAR(32)` nullable, no enum/CHECK
  — adding a status is code-only, no migration. Don't add a write path that bypasses the normalizer.
- **Forwarded webhooks dispatch on the canonical topic form.** The app shell forwards the topic
  from `authenticate.webhook()`, which is Shopify's `topicForStorage()` form — **`UPPER_SNAKE`**
  (e.g. `PRODUCTS_UPDATE`, `APP_UNINSTALLED`), NOT the REST-header form (`products/update`). The
  agent's dispatch (`agent/app/api/webhooks.py`) must canonicalize the incoming topic and match on
  `PRODUCTS_UPDATE` / `APP_UNINSTALLED`. Dispatching on the REST form silently no-ops **every**
  forwarded webhook: the handler returns 204, the app returns 200, and no DB row is written.
  (Observed 2026-07-21 — a green 200 masking zero DB effect.)
- **`prisma migrate dev` is LOCAL ONLY** — it can reset the database. Deployed environments
  use `prisma migrate deploy` (this is what CI runs).
- Do not put running task lists or plans in this file (they go stale) — those live in the PR/issue.

## Git
- **Never add AI attribution to commits or PRs.** No `Co-Authored-By: Claude`/AI trailers, no
  "Generated with Claude Code" lines, no AI mentions in commit messages or PR bodies. Write
  commit messages as the human author, plainly describing the change.

## Current Status

Phase tree from `PRD.md` §15; step status is evidence-based (merged PRs + `docs/session-log/`).
Only items confirmed by committed code or a session log are checked.

### Phase 0 — Scaffold — complete (deploy deferred)
- [x] Monorepo: `app/` (Shopify React Router template) + `agent/` (FastAPI) independently runnable
- [x] Local infra: docker-compose Postgres (pgvector) + Redis, Alembic bootstrap, `.env.example` both sides
- [x] CI: lint + boot check both services (agent `pytest` / ruff, app `npm run build`)
- [ ] Railway deploy — deferred; local-only dev through Phase 4, required before Phase 5 (Ship). PRD §15 lists it under Phase 0, so it stays visible: "Phase 0 complete" does NOT mean a deployed environment exists

### Phase 1 — Connect — complete
- [x] Shopify OAuth + embedded app loads in the dev store (`quixly-ljymkoyb.myshopify.com`)
- [x] Session storage on Postgres via Prisma (`shopify` schema), fenced from Alembic (`public`)
- [x] Token custody: single refresh authority + per-shop advisory lock; agent `TokenProvider` stores no token
- [x] Catalog ingestion → `public.products` (20/20, resumable via `ingest_runs.cursor`)
- [x] `visibility_state` single normalizer (active/draft/archived/unlisted)
- [x] CI typecheck gate wired (`react-router typegen && tsc --noEmit`)

### Phase 2 — Audit — complete
- [x] Interrogator: query-panel builder (coffee vertical)
- [x] EngineRunner: Perplexity Sonar client + `query_panels` / `engine_runs` persistence
- [x] Extractor: LLM brand extraction + grounding guard + self-mention matching
- [x] ShareOfModel aggregator, run-scoped on `(run_id, engine)`
- [x] Agent-run identity: `agent_runs` + `run_id` threaded through EngineRunner/Extractor/aggregator
- [x] Scan route + orchestration task, keyed on `shop_domain`
- [x] Read-only report UI: embedded audit page + agent client `startScan` / `getReport`
- [x] Gate F — live webhook verification: `products/update` (HMAC + DB write) & `app/uninstalled` (status flip); topic-dispatch fix `f0b97bc`; reinstall + re-ingest 20/20

### Phase 3 — Fix — complete (taxonomy metafield path deferred)
- [x] Product audit — per-product, per-class rubric; three-state spec model
      (structured/unstructured/absent); two channel-specific coverage numbers. Gate G, re-baked as
      Gate M when the family set grew 7→9 (added `coffee-product-form`, `decaffeination-method`)
- [x] Write-target spike — proved the AI-legibility channel is taxonomy + description, not
      `custom.*`; settled the GID form and the two scope walls (`docs/decisions/2c-write-target.md`)
- [x] Grounded Optimizer — structural targeting; taxonomy-attribute / description / `category`
      routing (`custom.*` retired); grounding guard + gap→row accounting invariant; demo seed
      retired (Gates H/L/M). Before/after diff + source per fix
- [x] Negative grounding (step 2e) — absence claims are guarded like fills: deterministic recovery
      + negative literal-presence guard + the `mentioned_no_value` to-do tier. Run 839's 14 false
      absence claims → 0, and 0 even when extraction grounds nothing at all
- [x] Step 4-preamble — `read_metaobjects` + `read_publications` granted (`6b9b17a`); channel
      reality RESOLVED (Copilot active, 113 published); canonical GID map live-validated. Resolver
      contract **NOT confirmed** — blocked on `read_metaobject_definitions`
      (`docs/decisions/2c-write-target.md` L6/L7)
- [x] Preview/approve UI behind the mandatory approval gate (`fixes.status = approved`) — Step 3.
      Agent `api/fixes.py` (run / list / approve / reject) + shell `app.fixes.tsx`. Reads `fixes`,
      writes exactly one column (`fixes.status`); zero Shopify calls. Invariants in Conventions
- [x] Publisher (Admin API writes) — Step 4, **live-verified on the dev store 2026-07-28**.
      `graph/publisher.py` + `jobs/publish.py` + `POST .../fixes/publish`. Ships on **`category` +
      `description`**; taxonomy metafield still deferred (an approved one **aborts** the run).
      Two-layer staleness gate, separate-re-read verification, reconciliation-before-staleness so a
      replay cannot double-append, `PUBLISH_ALLOWED_SHOPS` rollout guard. Migration `530075ef94b8`
      (`publish_error`, `published_at`). Live: description + category both landed and re-read
      confirmed; a replay left Shopify's `updatedAt` untouched; a product edited after approval was
      refused as `stale`. Invariants in Conventions; evidence `2c-write-target.md` L9–L12

### Phase 4 — Verify — not started
- [ ] Verifier loop. **A first-party channel EXISTS — uplift verification is NOT forced onto the
      engine panel alone** (2026-07-27, reversing the earlier UNKNOWN): the dev store carries an
      ACTIVE Microsoft Copilot agentic publication with product 113 published to it, so a
      Catalog/agentic round-trip is possible. The **description** field — which Copilot reads — is
      the proven-writable legibility channel. Read membership via `product.resourcePublications`;
      enumeration hides the channel (see Conventions). Evidence: `2c-write-target.md` L7.
      (L5 — whether non-mapped `custom.*` reaches Catalog — remains unresolved and needs a live
      agentic query; moot since `custom.*` is retired)
- [ ] Uplift chart
- [ ] Scheduled weekly scans (also keep the refresh chain warm)
- [ ] Browserbase shopping-agent simulation

### Phase 5 — Ship — not started
- [ ] Shopify Billing API tiers
- [ ] Onboarding flow
- [ ] App Store submission (incl. compliance webhooks `customers/data_request`/`redact`, `shop/redact`)
- [ ] MCP server

**Next action:** Phase 3 is complete end-to-end — the Optimizer proposes, the gate approves, and the
Publisher writes to a live store and verifies by re-reading (steps 2b–4, all live-verified on
`quixly-ljymkoyb`). Two things carry forward rather than being finished:
- the **taxonomy metafield** write path stays deferred behind `read_metaobject_definitions` (a third
  merchant consent) plus proof the metaobject entry surface populates — see `docs/backlog.md`;
- `PUBLISH_ALLOWED_SHOPS` still lists only the dev store, by design.

**Phase 4 (Verify) is the next build.** It has what it needs: a proven-writable legibility channel
(`description`), a live agentic publication to read back (Microsoft Copilot on the dev store), and
now `fixes.published_at` as the "went live at" timestamp to measure uplift against. Read channel
membership via `product.resourcePublications` and **never** by enumerating `publications` or gating
on a count field (see Conventions). Phase 0's Railway deploy is still outstanding and is required
before Phase 5.

## Session Log

Per-session notes of record live in `docs/session-log/` — one file per session, named
`YYYY-MM-DD-phase-<n>-<slug>.md`. Read the latest before large changes.
