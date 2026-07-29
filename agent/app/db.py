"""Async database engine and session factory.

The app runs async (asyncpg); Alembic runs sync (psycopg) — see ``alembic/env.py``.

TLS is passed through ``connect_args``, never through the URL. A managed Postgres needs
TLS, but ``?sslmode=require`` is rejected by asyncpg and ``?ssl=require`` is rejected by
psycopg — and ``alembic/env.py`` builds its sync URL with a naive ``+asyncpg`` →
``+psycopg`` replace that carries the query string across to the driver that cannot parse
it. So DATABASE_URL stays TLS-free and each leg is configured for its own driver: this
module for asyncpg, ``PGSSLMODE`` (read by libpq) for the psycopg leg Alembic runs on.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.settings import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    # A managed/pooled Postgres will drop idle connections out from under the pool;
    # pre_ping turns that into a transparent reconnect instead of a failed request.
    pool_pre_ping=True,
    connect_args={"ssl": _settings.database_ssl},
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with SessionLocal() as session:
        yield session
