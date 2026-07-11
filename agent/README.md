# `agent/` — Quixly agent service

Python (FastAPI + LangGraph) service: the brain of Quixly — engine querying,
shopping-agent simulation, diagnosis, grounded optimization, publishing, and
verification. Async workers run here. See root `README.md` and `PRD.md` for the
full picture.

Phase 0 (scaffold) ships only a `/health` route, settings management, an empty
Alembic setup, and an empty `app/graph/` node directory. No agent logic yet.

## Layout

```
app/
  main.py       # FastAPI app + GET /health
  settings.py   # pydantic-settings (env-driven)
  worker.py     # arq WorkerSettings stub (no jobs yet)
  graph/        # one LangGraph node per file (none yet — see graph/README.md)
alembic/        # migrations (empty — none yet)
tests/          # pytest
```

## Commands

```bash
uv sync                              # install deps (or: pip install -e .)
uv run uvicorn app.main:app --reload # run the API (http://localhost:8000/health)
uv run pytest                        # tests
uv run ruff check .                  # lint
uv run alembic upgrade head          # DB migrations (needs Postgres up)
uv run arq app.worker.WorkerSettings # background worker
```

Set up your environment first: `cp .env.example .env` and fill in values.
Postgres/Redis come from the root `docker-compose.yml`.
