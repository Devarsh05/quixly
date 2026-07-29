# 2026-07-29 — Phase 0 deploy, Stage A (pre-flight fixes)

Scope: pre-flight only. No external accounts created, nothing deployed, no migration run
against any remote database, no scope or `shopify.app.toml` change, no engine spend. The
webhook-deadlock fix is deliberately **not** here — it is a separate change.

Branch: `phase-0-deploy-stage-a`, cut from `phase-4-verifier`.

---

## Work-tree resolution

Three dirty files and one untracked file were carried in from the Phase 4 session.

- **`CLAUDE.md`** — status prose only: the Phase 4 Step 1 checklist item flipped from "not yet
  run live" to live-verified with the observed acceptance values, and the "Next action"
  paragraph rewritten to what carries forward. No rule, invariant or convention was touched.
  Committed to `phase-4-verifier` with the session log it refers to.
- **`docs/session-log/2026-07-29-phase-4-verifier-acceptance.md`** — untracked; committed
  alongside the above.
- **`app/shopify.web.toml`** — working copy had `port = 3001`, committed tree had `3000`.
  3001 is correct: `shopify app dev` runs with `--tunnel-url=…:3000`, so the CLI proxy binds
  3000 and forwards to the app port declared in this file. At 3000 the proxy forwards to
  itself. Committed at 3001 so a fresh clone cannot reintroduce the loop.
- **`app/shopify.app.toml`** — left dirty, deliberately. Its diff is local dev-tunnel state
  (ngrok `application_url`, `automatically_update_urls_on_dev = false`, webhook block
  reordering). Out of scope by instruction; not committed.

---

## 1. Secret leak in the app image — FIXED

**The leak was real and was reproduced before fixing.** A probe image (`FROM alpine; COPY . .`)
built against `app/` under the then-current `.dockerignore`:

```
=== sensitive paths present in the image ===
/app/.env
/app/.shopify/localhost-key.pem
/app/.shopify/project.json
/app/.react-router: types
/app/tests: admin-token-rotation.test.ts  agent-client.test.ts  app.audit.test.ts …

=== grep the image filesystem for the INTERNAL_API_KEY value ===
/app/.env
```

**Correction to the premise.** `app/.env` does **not** contain `SHOPIFY_API_SECRET` — the
Shopify CLI injects that at dev time and it was never on disk here. What `app/.env` does
carry, and what did bake into the layer, is:

- `INTERNAL_API_KEY` — the shared secret with the agent (48 chars, confirmed present in-layer)
- `DATABASE_URL` — with Postgres credentials

Also swept into the context: `app/.shopify/localhost-key.pem`, a **TLS private key**.

Rotating `SHOPIFY_API_SECRET` in Partners remains worth doing, but the credentials that
actually need rotating on the strength of this finding are `INTERNAL_API_KEY` (both sides,
they must match exactly) and the database password.

`app/.dockerignore` now excludes `.env`, `.env.*`, `.shopify`, `.react-router`, `tests`.

**Verification.** Filesystem of the real `quixly-app:stage-a` image:

```
absent  /app/.env
absent  /app/.shopify
absent  /app/tests
absent  /app/.react-router
absent  /app/app
absent  /app/vite.config.ts

contents of /app: build  node_modules  package-lock.json  package.json  prisma

PASS: neither secret found anywhere in the image filesystem
```

**Two methodology traps hit on the way, both worth recording:**

1. A plain `docker save … | grep` over the tarball reports **zero hits on a known-leaky
   image**. Layer blobs are gzip-compressed under the containerd image store, so the
   plaintext is not there to find. A `0` from that method is meaningless.
2. After decompressing per-blob, the scan *still* reported a clean PASS on the known-leaky
   image. Cause: `grep -q` exits on first match → the decompressor takes SIGPIPE → under
   `set -o pipefail` the pipeline returns non-zero → the `if` reads as "no match". A real
   leak presented as a green PASS. Counting with `grep -c` (which consumes the stream) fixed
   it.

Only after the scanner was validated against the known-leaky image (`LEAK: needle present in
1 layer(s)`, listing `app/.env` and `app/.shopify/localhost-key.pem`) were the real results
trustworthy:

```
image=quixly-app:stage-a    blobs=16  readable_layers=10  layers_containing_needle=0  PASS
image=quixly-agent:stage-a  blobs=20  readable_layers=14  layers_containing_needle=0  PASS
```

---

## 2. App Dockerfile — the predicted build failure DOES NOT EXIST

The premise was that `npm ci --omit=dev` followed by `npm run build` fails because
`react-router build` needs vite/typescript from `devDependencies`. **It does not.** The
unmodified upstream Dockerfile was built first, deliberately, to check:

```
#12 5.546 vite v7.3.6 building ssr environment for production...
#12 5.914 ✓ built in 368ms
#13 naming to docker.io/library/quixly-app:baseline done
```

Mechanism: `vite` is a **non-optional `peerDependency` of `@react-router/dev`**, and
`@react-router/dev` sits in `dependencies`. npm auto-installs non-optional peers, so vite
survives `--omit=dev`. `typescript` is an *optional* peer and does get pruned — but
`react-router build` does not need it (only `react-router typegen` does, which is a CI gate,
not a build step).

The multi-stage build was implemented anyway, on its own merits rather than the stated one:

- the runtime image no longer contains the source tree, `vite.config.ts`, or the toolchain —
  only `build/`, `node_modules/`, `package.json`, `prisma/`;
- the build stage gets the full dependency set, so the build stops depending on a transitive
  peer-dependency accident that a future npm or dependency change could quietly remove.

Reverting to the single stage is a live option now that the premise is known false; the
multi-stage version is the recommendation.

**GATE — `docker build` succeeded:**

```
#15 6.817 ✓ built in 343ms
#16 [runtime 7/7] COPY --from=build /app/build ./build
#16 DONE 0.2s
#17 naming to docker.io/library/quixly-app:stage-a done
#17 unpacking to docker.io/library/quixly-app:stage-a 6.0s done
#17 DONE 30.2s
=== docker build exit: 0 ===
```

**Open item for provisioning (not fixed here).** `app/.gitignore` ignores
`package-lock.json` (the Shopify template ships none — which is why CI uses `npm install`).
A build from a clean git checkout therefore has no lockfile and resolves floating ranges at
build time. The Dockerfile falls back to `npm install` when the lockfile is absent so the
build still works, but **a deployed build is not reproducible until the lockfile is
committed.** That is a decision, not a Dockerfile fix.

---

## 3. Agent Dockerfile + .dockerignore + entrypoint — NEW

- `agent/Dockerfile` — `python:3.12-slim`, deps from `pyproject.toml` + `uv.lock` via
  `uv sync --frozen --no-dev` (a drifted lock fails the build rather than silently resolving
  something else), two-step so dependencies layer separately from source, runs as uid 10001,
  `EXPOSE 8000` and nothing published.
- `agent/.dockerignore` — excludes `.env`, `.env.*`, `.venv`, `__pycache__`, `*.py[cod]`,
  `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `*.egg-info`, `.shopify`, `.git`.
- `agent/docker-entrypoint.sh` — `alembic upgrade head` **once**, before anything serves and
  outside the supervisor; then uvicorn and arq under `wait -n` with a SIGTERM trap. Ten-line
  sh wrapper, not supervisord. Needs bash — `wait -n` is not POSIX and Debian's `/bin/sh` is
  dash.

**GATE — `docker build` succeeded:**

```
#15 [10/11] RUN uv sync --frozen --no-dev     && chmod +x docker-entrypoint.sh
#15 2.305       Built quixly-agent @ file:///srv
#15 2.895  + quixly-agent==0.1.0 (from file:///srv)
#16 [11/11] RUN useradd --create-home --uid 10001 agent     && chown -R agent:agent /srv
#17 naming to docker.io/library/quixly-agent:stage-a done
#17 DONE 14.2s
=== docker build exit: 0 ===
```

**The hard requirement was not just claimed — it was run, both directions.** The container
was started against local Postgres/Redis and each child killed in turn from outside:

```
===================== CASE: kill arq =====================
entrypoint: applying database migrations (alembic upgrade head)
entrypoint: migrations applied
entrypoint: uvicorn started (pid 7)
entrypoint: arq worker started (pid 8)
--- killing arq, pid 8, inside the container ---
Running=false  ExitCode=137
entrypoint: arq worker exited (status 137)
entrypoint: taking the container down so the platform restarts it

===================== CASE: kill uvicorn =====================
entrypoint: uvicorn started (pid 8)
entrypoint: arq worker started (pid 9)
--- killing uvicorn, pid 8, inside the container ---
Running=false  ExitCode=137
entrypoint: uvicorn exited (status 137)
entrypoint: taking the container down so the platform restarts it
```

Both directions die, both exit non-zero, and the log names the right child each time. No
half-alive container.

---

## 4. TLS out of the agent DATABASE_URL

`DATABASE_URL` carries no TLS parameter. TLS is configured per-driver:

- `agent/app/db.py` — `create_async_engine(url, pool_pre_ping=True, connect_args={"ssl": …})`
- `PGSSLMODE` — plain env var for the libpq/psycopg leg Alembic runs on

`_sync_database_url()`'s `+asyncpg` → `+psycopg` replace is untouched.

**Deviation from the instruction, with evidence.** The instruction specified a literal
`connect_args={"ssl": "require"}`. Hardcoding `require` breaks local development **and CI** —
both run a plain Postgres container with `ssl = off`, which rejects the upgrade outright:

```
ssl='prefer'     -> OK (select 1 -> 1)
ssl='require'    -> FAILED: ConnectionError: PostgreSQL server at "localhost:5432"
                            rejected SSL upgrade
ssl unset        -> OK (select 1 -> 1)
```

So the value is env-driven: new `DATABASE_SSL` setting, default `prefer` (which is asyncpg's
own default — zero behavioural change locally), with deployed environments setting
`DATABASE_SSL=require` and `PGSSLMODE=require`. This is what item 4's own closing constraint
asks for — "direct-vs-pooled and TLS stay env/driver config" — and no provider endpoint is
hardcoded anywhere. **Setting both variables is a required provisioning step**; it is
documented in `agent/.env.example`.

---

## 5. `poll_delay = 15`

Set on `WorkerSettings` with the reasoning in-comment: arq's 0.5s default costs ~5.2M Redis
commands/month completely idle against a 500K free ceiling (exhausted in ~3 days of doing
nothing); 15s lands near 35%. Cost is up to 15s of pickup latency on jobs measured in
minutes. The comment states the constraint that makes the budget hold: **exactly one arq
process, never 2 replicas** — the poll cost is per-process.

---

## 6. App-shell pool parameters — DOCUMENTED ONLY

No code change. `app/.env.example` now documents that deployed environments append
`&connection_limit=10&pool_timeout=3` to `DATABASE_URL`, with the reasoning: the known
pool-coupled webhook deadlock needs a second connection while the advisory lock holds one for
its whole `$transaction`, and Prisma's default pool is `num_cpus * 2 + 1` (~5 on a small
container). `connection_limit=10` raises the threshold; `pool_timeout=3` makes a starved
request fail fast inside Shopify's 5s webhook budget instead of hanging.

Recorded there in the same words used here: **this is a mitigation, not the fix.** The actual
fix is a separate change.

---

## 7. `version_table_schema` — RECONCILED, fence intact

`CLAUDE.md` said never to set `version_table_schema`; `agent/alembic/env.py:88` sets it to
`OWNED_SCHEMA`. Resolved by measurement against **local** Postgres (at head `2bf30ca663a2`,
with `shopify.Session` and `shopify._prisma_migrations` present, `alembic_version` in
`public`) — never against a remote database.

**The required drift check is EMPTY:**

```python
def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    pass
    # ### end Alembic commands ###
```

Answering the actual question — *does the line-48 name exclusion fully neutralize the DROP?*
**Yes, and it is the only thing doing so.** Three cases, same database:

| `version_table_schema` | by-name exclusion (env.py:48) | autogenerate result |
|---|---|---|
| `OWNED_SCHEMA` | present | **empty** — current code |
| `OWNED_SCHEMA` | removed | `op.drop_table('alembic_version')` |
| unset | removed | empty |

So CLAUDE.md's causal claim was right — pinning the schema *does* break Alembic's built-in
self-exclusion — but its instruction described a state the code was not in. The code is safe
because the by-name guard compensates. `CLAUDE.md` is reconciled to the code in both places
it appears (Schema ownership, Risk zones), with the coupling stated: **pinning the schema
while removing the name guard is the unsafe combination.**

`env.py` was restored byte-identical to HEAD after the probes (`git diff --stat` empty) and no
generated migration was left behind (`git status` on `agent/alembic/` clean).

---

## 8. CI gates — all green

```
########## app: npm run lint ##########          === lint exit: 0 ===
########## app: npm run typecheck ##########     === typecheck exit: 0 ===
########## app: npm test ##########
 Test Files  5 passed (5)
      Tests  53 passed (53)                      === npm test exit: 0 ===
########## app: npm run build ##########
✓ built in 1.43s                                 === build exit: 0 ===

########## agent: python -m ruff check . ##########
All checks passed!                               === ruff exit: 0 ===
########## agent: python -m pytest ##########
394 passed, 7 skipped, 1 warning in 110.79s      === pytest exit: 0 ===
```

Agent gates were run **after** the `db.py` / `settings.py` / `worker.py` edits, so the 394
passing tests exercise the new `connect_args={"ssl": …}` path.

---

## Carried forward into Stage B

1. **Rotate `INTERNAL_API_KEY` and the database password** — the leak was real, and the
   `.dockerignore` fix only stops the next one.
2. **Commit `app/package-lock.json`**, or accept non-reproducible deployed builds.
3. **Set `DATABASE_SSL=require` and `PGSSLMODE=require`** on the agent at provisioning —
   defaults are `prefer` so local and CI work.
4. **Append `&connection_limit=10&pool_timeout=3`** to the app shell's deployed
   `DATABASE_URL`.
5. **Exactly one arq replica.** The `poll_delay` budget assumes it.
6. The **webhook-deadlock fix** is still outstanding and is a separate change.
7. `docker-entrypoint.sh` needs its executable bit preserved in git.
