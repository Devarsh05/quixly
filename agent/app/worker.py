"""Arq worker configuration for async scans and scheduled runs."""

from arq.connections import RedisSettings

from app.jobs.fix import run_fix_task
from app.jobs.ingest_catalog import ingest_catalog
from app.jobs.publish import run_publish_task
from app.jobs.scan import run_scan_task
from app.jobs.verify import run_verify_task
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

    functions = [ingest_catalog, run_scan_task, run_fix_task, run_publish_task, run_verify_task]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)

    # Arq's default poll_delay of 0.5s costs a Redis command every half-second per worker
    # process — ~5.2M commands/month completely idle, against a 500K/month free Upstash
    # ceiling. That exhausts the quota in about three days of doing nothing at all. At 15s
    # the idle floor lands near 35% of the ceiling, leaving headroom for actual work.
    #
    # The cost is up to 15s of latency before a queued job is picked up. Every job here is
    # a background scan / fix / publish / verify measured in minutes, so that is invisible.
    #
    # This budget assumes EXACTLY ONE arq process. Never run 2 replicas: the poll cost is
    # per-process, so a second replica doubles the idle command rate straight through the
    # ceiling — and the jobs are keyed per shop, not sharded, so a second one buys nothing.
    poll_delay = 15
