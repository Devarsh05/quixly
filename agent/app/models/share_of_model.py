"""The ``share_of_model`` table (PRD §6, §8).

One row per (shop, engine, period) holding the store's mention rate and each tracked
competitor's mention rate, computed by ``app.graph.share_of_model`` from the latest usable
``engine_runs`` per query.

``our_rate`` is NULLABLE by design: a fully-degraded scan (no usable latest run for any query)
stores NULL — "no data" — never ``0.0``, which would misread as a real 0% recommendation rate a
merchant would act on. ``engine`` is a plain ``String(32)``, mirroring ``engine_runs.engine``
(adding an engine is code-only). ``(shop_id, engine, period)`` is unique — the UPSERT target — so
re-running a period updates in place instead of duplicating.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ShareOfModel(Base):
    __tablename__ = "share_of_model"
    __table_args__ = (
        UniqueConstraint(
            "shop_id", "engine", "period", name="uq_share_of_model_shop_engine_period"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True, nullable=False
    )
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    period: Mapped[str] = mapped_column(String(32), nullable=False)

    # NULLABLE by decision: NULL = no usable data this period (never 0.0). See module docstring.
    our_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    our_mentions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_queries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    competitor_rates_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
