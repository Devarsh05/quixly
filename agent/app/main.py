"""FastAPI application entrypoint for the Quixly agent service.

Phase 1 (Connect): shop registration, catalog ingestion, and forwarded webhooks. Engine
querying, simulation, diagnosis, optimization, and verification land in later phases.

Every route under /shops and /webhooks is part of the *internal* API: authenticated with
the INTERNAL_API_KEY shared secret and reachable only by the app shell, never publicly.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import scan, shops, webhooks
from app.redis import close_redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await close_redis()


app = FastAPI(title="Quixly Agent", version="0.1.0", lifespan=lifespan)

app.include_router(shops.router)
app.include_router(scan.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
