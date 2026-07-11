"""Alembic environment.

Resolves the database URL from the app settings / environment and runs migrations
with a synchronous psycopg driver. No models are defined yet (Phase 0 scaffold),
so ``target_metadata`` is ``None`` and there are no migrations in ``versions/``.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models yet — autogenerate has nothing to target in Phase 0.
target_metadata = None


def _sync_database_url() -> str:
    """Return the DATABASE_URL as a sync (psycopg) URL for Alembic."""
    url = get_settings().database_url
    return url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _sync_database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
