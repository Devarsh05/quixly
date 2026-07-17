"""The ``engine_runs`` table (PRD §8).

One row per (panel query, engine) execution. EngineRunner writes ``response_raw`` (the full
untouched engine payload, or an ``{"error": ...}`` envelope on failure) and ``cited_sources_json``
(a normalized list of source objects). ``cited_brands_json`` and ``our_mentions_json`` are left
NULL here and filled by the Extractor (step 3).

``engine`` is a plain string, not an enum: adding an engine is a code-only change, mirroring how
``products.visibility_state`` avoids an enum/CHECK. Rows always INSERT (never upsert) and are
stamped with ``ts`` so period-over-period runs accumulate.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class EngineRun(Base):
    __tablename__ = "engine_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("query_panels.id", ondelete="CASCADE"), index=True, nullable=False
    )

    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    response_raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cited_sources_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Filled by the Extractor (step 3); NULL until then.
    cited_brands_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    our_mentions_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    panel: Mapped["QueryPanel"] = relationship(back_populates="engine_runs")  # noqa: F821
