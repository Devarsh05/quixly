"""Shared test fixtures.

Token/auth tests are pure unit tests: they fake Redis and stub HTTP, so they need no
services. Tests that take the ``db`` fixture need Postgres with migrations applied
(``alembic upgrade head``); CI provides it as a service container.
"""

import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app import redis as redis_module
from app.settings import get_settings

TEST_API_KEY = "test-internal-key"


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    """Point settings at test values and clear the lru_cache around them."""
    monkeypatch.setenv("INTERNAL_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("APP_SHELL_URL", "http://app-shell.test")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def fake_redis(monkeypatch):
    """Swap the shared Redis client for an in-memory fake."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "_redis", client)
    yield client
    await client.aclose()
    monkeypatch.setattr(redis_module, "_redis", None)


@pytest.fixture
async def db(settings):
    """A session inside a transaction that is rolled back after the test.

    ``join_transaction_mode="create_savepoint"`` means the code under test can call
    ``commit()`` for real — it releases a SAVEPOINT — while the outer transaction still
    rolls everything back. So we exercise the real commit path without leaving rows behind.

    The engine is built per test with NullPool rather than reusing ``app.db.engine``:
    pytest-asyncio gives each test a fresh event loop, and a pooled asyncpg connection
    held across loops raises "Event loop is closed" on teardown.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)

    connection = await engine.connect()
    transaction = await connection.begin()

    session = AsyncSession(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )

    yield session

    await session.close()
    await transaction.rollback()
    await connection.close()
    await engine.dispose()
