"""Alembic environment.

Resolves the database URL from the app settings / environment and runs migrations
with a synchronous psycopg driver (the app itself runs async on asyncpg).

Schema ownership (see CLAUDE.md): Alembic owns ``public``. Prisma owns the
``shopify`` schema and the Session table inside it. The guards below keep
autogenerate from ever emitting DDL against anything outside ``public`` — without
them it would see ``shopify.Session`` as an unknown table and try to DROP it.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.models import Base
from app.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

OWNED_SCHEMA = "public"

# Alembic's own bookkeeping table. It is not part of ``Base.metadata``, so autogenerate
# sees it as an unmodelled table and emits a DROP for it — verified, not theoretical.
# Excluded explicitly rather than relying on Alembic's built-in self-exclusion.
VERSION_TABLE = "alembic_version"


def include_object(object, name, type_, reflected, compare_to) -> bool:
    """Ignore every database object Alembic does not own.

    Two distinct hazards, both real:

    1. ``shopify.Session`` / ``shopify._prisma_migrations`` belong to **Prisma**. Without
       this filter, autogenerate would see them as unknown tables and DROP them.
    2. ``alembic_version`` is Alembic's own history and is not in ``Base.metadata``, so
       it too gets a spurious DROP.

    ``object.schema`` is None for tables declared without an explicit schema, which
    resolves to ``public`` — those are ours.
    """
    if type_ == "table" and name == VERSION_TABLE:
        return False

    schema = getattr(object, "schema", None)
    return schema in (None, OWNED_SCHEMA)


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
        include_schemas=False,
        include_object=include_object,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=False,
            include_object=include_object,
            version_table_schema=OWNED_SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
