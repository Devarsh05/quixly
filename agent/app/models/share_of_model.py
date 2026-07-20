"""The ``share_of_model`` table (PRD §6, §8).

One row per ``(run, engine)`` holding the store's mention rate and each tracked competitor's
mention rate, computed by ``app.graph.share_of_model`` from the latest usable ``engine_runs`` per
query for a single scan.

**Identity is the run, not the period.** The aggregate is keyed on ``(run_id, engine)`` — the
UPSERT target — so two scans of the same shop on the same day produce two distinct rows instead
of the second overwriting the first. ``period`` (default: the run's start date) is now a plain
human label for before/after uplift charts, not part of the key. ``shop_id`` is kept and indexed
so a shop's history can be queried across runs.

``run_id`` deletes ``CASCADE`` — the OPPOSITE of ``engine_runs.run_id`` (``SET NULL``).
``share_of_model`` is a *derived, recomputable* aggregate, meaningless without its run, so it
should die with the run; raw ``engine_runs`` are measurement data we preserve. ``shop_id`` also
CASCADEs from ``shops`` — a shop's aggregates go with the shop.

``our_rate`` is NULLABLE by design: a fully-degraded scan (no usable latest run for any query)
stores NULL — "no data" — never ``0.0``, which would misread as a real 0% recommendation rate a
merchant would act on. ``engine`` is a plain ``String(32)``, mirroring ``engine_runs.engine``
(adding an engine is code-only).
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ShareOfModel(Base):
    __tablename__ = "share_of_model"
    __table_args__ = (
        UniqueConstraint("run_id", "engine", name="uq_share_of_model_run_engine"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Aggregate identity: one row per (run, engine). CASCADE — a derived aggregate dies with
    # its run (contrast engine_runs.run_id SET NULL, which preserves raw measurement rows).
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # shop_id FK unchanged (CASCADE from shops) — kept + indexed for cross-run history; it is
    # simply no longer part of the unique key.
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True, nullable=False
    )
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    # Human before/after label (default: the run's start date), no longer part of the key.
    period: Mapped[str] = mapped_column(String(32), nullable=False)

    # NULLABLE by decision: NULL = no usable data this period (never 0.0). See module docstring.
    our_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    our_mentions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_queries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    competitor_rates_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
