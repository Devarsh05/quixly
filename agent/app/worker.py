"""Arq worker configuration for async scans and scheduled runs."""

from arq.connections import RedisSettings

from app.jobs.fix import run_fix_task
from app.jobs.ingest_catalog import ingest_catalog
from app.jobs.scan import run_scan_task
from app.redis import close_redis
from app.services.token_provider import TokenProvider
from app.settings import get_settings


async def startup(ctx: dict) -> None:
    # One TokenProvider (and one httpx pool) per worker process. Jobs must never build
    # their own — a second token path would be a second refresh authority, which would
    # invalidate the app shell's. See app/services/token_provider.py.
    ctx["token_provider"] = TokenProvider()


async def shutdown(ctx: dict) -> None:
    await close_redis()


class WorkerSettings:
    """Arq worker settings."""

    functions = [ingest_catalog, run_scan_task, run_fix_task]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
