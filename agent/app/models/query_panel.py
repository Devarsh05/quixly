"""The ``query_panels`` table (PRD §8).

A query panel is the deterministic set of buyer-intent queries generated for a shop's vertical
(see ``app.graph.interrogator``). The ``fingerprint`` is the panel's content hash from step 1;
``(shop_id, fingerprint)`` is unique so re-running an identical panel reuses this row rather than
duplicating it, and the ``engine_runs`` that reference it accumulate period over period.

Note: the ORM class shares the name ``QueryPanel`` with the Pydantic value object in
``app.graph.interrogator``. They live in different modules; callers that need both alias one.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class QueryPanel(Base):
    __tablename__ = "query_panels"
    __table_args__ = (
        # The upsert target: an identical panel (same content fingerprint) for a shop reuses
        # this row instead of duplicating it.
        Index("uq_query_panels_shop_fingerprint", "shop_id", "fingerprint", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True, nullable=False
    )

    category: Mapped[str] = mapped_column(String(64), nullable=False)
    queries_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    engine_runs: Mapped[list["EngineRun"]] = relationship(  # noqa: F821
        back_populates="panel", cascade="all, delete-orphan"
    )
