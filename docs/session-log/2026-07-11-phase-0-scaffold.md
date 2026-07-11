# 2026-07-11 — Phase 0: Scaffold

> Session spanned 2026-07-10 (scaffold) → 2026-07-11 (rename fix + first commit + session log).
> First entry in `docs/session-log/`; convention = one file per session, named
> `YYYY-MM-DD-phase-<n>-<slug>.md`.

## Goal

Phase 0 from PRD §15 — **scaffold only**. Stand up the two-service monorepo shell with no
agent logic, engine querying, catalog ingestion, or OAuth flows:

- `app/` + `agent/` as independently runnable services
- `docker-compose.yml` with Postgres (pgvector) + Redis for local dev
- Empty Alembic setup, `.env.example` for both services, root README
- Basic CI (lint + boot check for both services)

## Key decisions

- **React Router 7 over Remix.** Shopify's official app template migrated from Remix to
  React Router 7 (`@shopify/shopify-app-react-router`); `shopify-app-template-remix` is now
  legacy. Chose the current template and updated the "Remix" wording in `CLAUDE.md` / `PRD.md`
  to "React Router".
- **Cloned the template repo instead of `shopify app init`.** `git clone --depth 1` of
  `Shopify/shopify-app-template-react-router` into `app/`, then stripped `.git`. Deterministic
  and non-interactive — avoids the CLI's semi-interactive Partner login during a scaffold-only
  phase.
- **No committed lockfile for `app/`.** Kept in line with the upstream Shopify template, which
  does **not** commit a lockfile. Verified against upstream (`main`) this session:
  `raw.githubusercontent.com/.../package-lock.json` → HTTP **404** (while `package.json` → 200),
  and the upstream `.gitignore` explicitly ignores `package-lock.json`, `yarn.lock`,
  `pnpm-lock.yaml`. Consequence: CI uses `npm install` (not `npm ci`), since `npm ci` requires
  a committed lockfile.

## Surfaced → Quixly rename gap (caught & fixed mid-session)

The project was renamed **Surfaced → Quixly** earlier, but the rename hadn't fully propagated
when `agent/` was scaffolded. A full case-insensitive sweep caught and fixed the stale refs:

- `agent/pyproject.toml` — package name + description
- `agent/app/main.py` — FastAPI `title` + module docstring
- `agent/app/settings.py` — default `database_url`
- `docker-compose.yml` — healthcheck still authenticated as user/db `surfaced` while
  `POSTGRES_USER`/`POSTGRES_DB` had already been set to `quixly` (the dangerous one — a silent
  connection-consistency mismatch)

Also swept: `agent/.env.example` (`DATABASE_URL`), `agent/README.md`, `agent/uv.lock`
(regenerated), and the `# Surfaced` titles in `README.md` / `PRD.md` / `CLAUDE.md`. Post-fix
`grep -ri surfaced` across the repo → **no matches**. Credentials now consistent across
`docker-compose.yml`, `agent/app/settings.py`, and `agent/.env.example`
(`quixly:quixly@localhost:5432/quixly`).

## Verified

- **Docker:** `docker compose down -v && up -d` (fresh volumes) → `quixly-db-1` and
  `quixly-redis-1` both report **healthy** (not just running) — exercises the healthcheck fix,
  since Postgres re-initialized the `quixly` role and `pg_isready -U quixly -d quixly` passed.
- **Alembic against live DB:** `alembic upgrade head` → exit 0; connected to the renamed DB and
  created `alembic_version` (owner `quixly`). No migrations authored yet.
- **Agent boot:** `uvicorn app.main:app` → `GET /health` = 200 `{"status":"ok"}`; `GET /docs`
  = 200 with OpenAPI `info.title` = **Quixly Agent**.
- **Agent quality gates:** `ruff check` clean; `pytest` → 1 passed.
- **App build:** `npm install` → `prisma generate` → `npm run lint` → `npm run build` all clean.
- **`shopify app dev` deferred:** correctly left to Phase 1 — it needs a Partner login + dev
  store, out of scope for scaffold.

## Open / deferred

- **Lockfile decision:** currently no committed lockfile for `app/` (matches upstream). Revisit
  if we want reproducible CI installs / Dependabot on a pinned tree — would mean committing a
  lockfile and switching CI back to `npm ci`.
- **Phase 1 prerequisites:** needs a Shopify Partner account + a dev store before OAuth,
  embedded app load, and catalog ingestion (`products`) can be built/tested.

## Commit

`10a7123` — "Phase 0: scaffold Quixly monorepo (app/ + agent/)". Per the `CLAUDE.md` git rule,
no AI attribution in the commit message. Not pushed.
