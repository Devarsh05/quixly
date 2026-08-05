<h1 align="center">SixRise</h1>

<p align="center">
  An autonomous agent that gets Shopify merchants' products recommended by AI shopping
  assistants (ChatGPT, Perplexity, Gemini, Copilot) — then <b>measures and verifies the uplift</b>.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white"/>
  <img src="https://img.shields.io/badge/TypeScript-3178C6?style=flat-square&logo=typescript&logoColor=white"/>
  <img src="https://img.shields.io/badge/React_Router_7-CA4245?style=flat-square&logo=reactrouter&logoColor=white"/>
  <img src="https://img.shields.io/badge/Postgres_+_pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white"/>
  <img src="https://img.shields.io/badge/Redis-DC382D?style=flat-square&logo=redis&logoColor=white"/>
  <img src="https://img.shields.io/badge/Shopify-7AB55C?style=flat-square&logo=shopify&logoColor=white"/>
</p>

See [`PRD.md`](./PRD.md) for the original product spec and [`CLAUDE.md`](./CLAUDE.md) for the
authoritative working rules (schema ownership, token custody, measurement invariants,
deployment).

> **Status — Phases 0–4 live and deployed.** The full loop runs end-to-end against a live
> Shopify dev store: query the engines → measure share-of-model → audit the catalog → generate
> grounded fixes → publish behind an approval gate → re-measure the delta. GDPR compliance
> webhooks are deployed. This is currently a **portfolio / demonstration deployment** — billing
> is not yet enabled and it has not been launched to real merchants (Phase 5).

> **Naming.** The merchant-facing brand is **SixRise**; the codebase and infrastructure retain
> the original **`quixly`** codename (Docker services, internal DNS, service names). Both refer
> to the same project.

---

## What it does

AI answer engines rank on **structured, machine-readable product data**, not page design — so
merchants with thin descriptions and missing attributes stay invisible, and they can't see
whether an assistant recommends them or a competitor. SixRise runs a continuous loop that
diagnoses that gap and closes it:

**interrogate** the engines with buyer-intent queries → **extract** which brands are recommended
and why → compute **share-of-model** vs. named competitors → **audit** the catalog for the gaps
behind competitor wins → generate **grounded fixes** → **publish** to Shopify behind human
approval → **verify** the recommendation-rate delta.

## Why the engineering is interesting

- **Grounding is a hard invariant.** The optimizer never fabricates a product attribute; a
  negative-grounding guard prevents a false "absent" from contradicting the audit. Every fix
  ships with a before/after diff and a source citation.
- **Measurement is sacred.** `fixes.published_at` is the immutable anchor for every before/after
  comparison and is *never* written by the verifier. A `run_id` threads through every persisted
  measurement structure, so uplift is reproducible rather than asserted.
- **Split-schema ownership, one Postgres.** Prisma owns the `shopify` schema (Sessions); Alembic
  owns `public` (business tables). The two migration tools never touch each other's schema.
- **Token custody is separated.** The app shell holds the Shopify offline access token; the
  agent service **stores no Shopify token** and reaches Shopify only through the shell.
- **Safe write-back.** A two-layer staleness gate guards every publish; channel state is read
  back through `product.resourcePublications` (not `publications`, which silently misses agentic
  channels like Copilot); the published page is re-parsed to confirm it landed.

## Architecture

The agent is a **hand-built, graph-shaped async pipeline** — one typed node per file, composed
as async steps (not a framework DAG):

```
interrogator → engine_runner → extractor → share_of_model → audit → optimizer → publisher → verifier
```

---

## Monorepo layout

Two services, one repo — each independently runnable:

- **`app/`** — TypeScript, Shopify **React Router 7** app template. Thin Shopify-facing shell:
  OAuth, session storage, billing, webhooks, App Bridge + Polaris UI. Presentation and proxy
  only; it owns the Prisma-managed `shopify` schema (Sessions) in the shared Postgres and holds
  the Shopify access token.
- **`agent/`** — Python, **FastAPI + Arq**. All business logic: engine querying, extraction,
  share-of-model, diagnosis, grounded optimization, publishing, verification, and async workers.
  Owns the Alembic-managed `public` schema; stores no Shopify token.
- **`docker-compose.yml`** — local Postgres (pgvector) + Redis for the agent service.

> The [`PRD.md`](./PRD.md) is the original vision; the shipped architecture diverged in two ways
> worth knowing: the agent is a hand-composed async pipeline (**not** LangGraph), and production
> runs on **Northflank + Neon + Upstash** rather than Railway.

## Prerequisites

- **Node** `>=20.19 <22 || >=22.12` and the [Shopify CLI](https://shopify.dev/docs/api/shopify-cli) (for `app/`)
- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/) (for `agent/`)
- **Docker** + Docker Compose (for local Postgres/Redis)

## Setup

```bash
# 1. Local infra (Postgres + Redis)
docker compose up -d

# 2. Environment files (copy templates, then fill in — never commit real values)
cp app/.env.example app/.env
cp agent/.env.example agent/.env
```

Required env vars are documented in each `.env.example`: the Shopify API key/secret (app), the
Perplexity Sonar and OpenAI keys (agent), and the Postgres/Redis URLs.

## Run each service

**Agent service** (`agent/`):

```bash
cd agent
uv sync                                 # install deps
uv run uvicorn app.main:app --reload    # http://localhost:8000/health
uv run pytest                           # tests
uv run ruff check .                     # lint
uv run alembic upgrade head             # DB migrations (needs docker compose up)
uv run arq app.worker.WorkerSettings    # background worker
```

> If `uv` isn't on your `PATH`, run the same commands with `python -m` — e.g. `python -m pytest`,
> `python -m alembic upgrade head`, `python -m arq app.worker.WorkerSettings`.

**App shell** (`app/`):

```bash
cd app
npm install
npm run dev      # embedded app via Shopify CLI (requires a Partner login + dev store)
npm run build
npm run lint
```

## Production

Compute on **Northflank** (`quixly` + `quixly-app`), Postgres + pgvector on **Neon**, Redis on
**Upstash**. Engine access via **Perplexity Sonar** (queries + citations) and **OpenAI**
(structured extraction).

## CI

[`.github/workflows/ci.yml`](./.github/workflows/ci.yml) runs a lint + boot check for both
services on every push/PR.
