"""FastAPI application entrypoint for the Quixly agent service.

Phase 0 scaffold: a placeholder health route only. Engine querying, simulation,
diagnosis, optimization, and verification routes/nodes land in later phases.
"""

from fastapi import FastAPI

app = FastAPI(title="Quixly Agent", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
