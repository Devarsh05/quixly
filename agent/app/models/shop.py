"""The ``shops`` table.

PRD §8 originally specified an ``access_token_ref`` column. It is deliberately absent:
Shopify offline access tokens now expire after 60 minutes, and obtaining a new one
retires the previous token and invalidates its refresh token immediately. There can
therefore be exactly ONE refresh authority, and that is the app shell. The agent holds
no long-lived credential and fetches short-lived tokens via ``TokenProvider``.
"""

import enum

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ShopStatus(enum.StrEnum):
    active = "active"
    uninstalled = "uninstalled"
    # Refresh tokens expire after 90 days of disuse. A shop whose refresh chain has
    # lapsed lands here and must re-auth — never silently 401.
    reauth_required = "reauth_required"


class Shop(Base, TimestampMixin):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    plan: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[ShopStatus] = mapped_column(
        Enum(ShopStatus, name="shop_status", native_enum=False, length=32),
        default=ShopStatus.active,
        nullable=False,
    )

    products: Mapped[list["Product"]] = relationship(  # noqa: F821
        back_populates="shop", cascade="all, delete-orphan"
    )
    ingest_runs: Mapped[list["IngestRun"]] = relationship(  # noqa: F821
        back_populates="shop", cascade="all, delete-orphan"
    )
