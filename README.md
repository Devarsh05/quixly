# Quixly

Autonomous agent that gets Shopify merchants' products recommended by AI shopping engines
(ChatGPT, Google AI Mode, Perplexity, Copilot, Gemini), then verifies the uplift. See
[`PRD.md`](./PRD.md) for the full product spec and [`CLAUDE.md`](./CLAUDE.md) for working rules.

> **Status:** Phase 0 — scaffold only. No agent logic, engine querying, catalog ingestion, or
> Shopify OAuth is implemented yet.

## Monorepo layout

Two services, one repo — each independently runnable:

- **`app/`** — TypeScript, Shopify **React Router** app template. Thin Shopify-facing shell:
  OAuth, session storage, billing, webhooks, App Bridge + Polaris UI. (Uses its own Prisma +
  SQLite session store, independent of the agent's Postgres.)
- **`agent/`** — Python, **FastAPI + LangGraph**. The brain: engine querying, simulation,
  diagnosis, grounded optimization, publishing, verification, async workers.
- **`docker-compose.yml`** — local Postgres (pgvector) + Redis for the agent service.

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

Required env vars are documented in each `.env.example`: Shopify API key/secret (app), the
Perplexity / OpenAI / Gemini keys (agent), and the Postgres/Redis URLs.

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

**App shell** (`app/`):

```bash
cd app
npm install
npm run dev      # embedded app via Shopify CLI (requires a Partner login + dev store)
npm run build
npm run lint
```

## CI

[`.github/workflows/ci.yml`](./.github/workflows/ci.yml) runs a lint + boot check for both
services on every push/PR.
