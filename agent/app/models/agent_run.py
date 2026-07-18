"""The ``agent_runs`` table (PRD §6, §8) — run identity for one scan.

One row per scan of a panel. ``engine_runs`` reference it via ``run_id`` so aggregation
(ShareOfModel) can scope to a single scan instead of guessing "the latest run per query" across
INSERT-only accumulated rows. MINIMAL by design: token/model/node-log telemetry (PRD §8) is
route-generated and lands with the scan route, not here.

``status`` is a plain ``String(32)`` (not a DB enum), mirroring ``engine_runs.engine`` — adding a
status is a code-only change. ``AgentRunStatus`` is the app-side vocabulary written into it.
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentRunStatus(enum.StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True, nullable=False
    )
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("query_panels.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
