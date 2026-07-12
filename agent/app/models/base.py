"""Declarative base for all Alembic-owned tables.

Every model here lives in the ``public`` schema. The ``shopify`` schema belongs to
Prisma (Session only) and must never be modelled or migrated from here — see
CLAUDE.md, "Schema ownership".
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for the agent's ORM models."""


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
