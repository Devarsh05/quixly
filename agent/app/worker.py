"""Arq worker configuration for async scans and scheduled runs.

Phase 0 scaffold: an empty worker so ``arq app.worker.WorkerSettings`` resolves.
Job functions and cron schedules are added in later phases; none are defined yet.
"""

from arq.connections import RedisSettings

from app.settings import get_settings


class WorkerSettings:
    """Arq worker settings. No jobs registered yet (scaffold)."""

    functions: list = []
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
