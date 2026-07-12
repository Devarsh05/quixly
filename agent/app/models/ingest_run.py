"""The ``ingest_runs`` table.

Deliberately separate from PRD §8's ``agent_runs``. That table is shaped for LangGraph
node execution (``node_logs_json``, ``tokens``, ``model``) and lands in Phase 2; catalog
ingest progress is a different concern, and welding the two together would give both a
bad schema.

``cursor`` holds the Shopify pagination cursor of the last successfully committed batch,
so a run that dies at SKU 1,900 leaves 1,900 rows and a resumable position — not an
empty table.
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class IngestStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    complete = "complete"
    failed = "failed"


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True, nullable=False
    )

    status: Mapped[IngestStatus] = mapped_column(
        Enum(IngestStatus, name="ingest_status", native_enum=False, length=32),
        default=IngestStatus.queued,
        nullable=False,
    )
    products_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    products_written: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    shop: Mapped["Shop"] = relationship(back_populates="ingest_runs")  # noqa: F821
