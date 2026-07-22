"""The ``audits`` table (PRD §8) — one deterministic product-audit result.

Produced by ``app.graph.audit`` from the rubric in ``services.audit_rubric``: the gaps found in a
product's catalog data (missing description / GTIN / metafields, not discoverable, absent spec
attributes), a ``severity`` band, and a ``spec_coverage`` ratio (present spec families / total).

**Run identity from day one.** ``run_id`` is a nullable FK to ``agent_runs`` (``ondelete=SET
NULL``), the same pattern as ``engine_runs.run_id`` — an audit is measurement data we preserve
even if the run metadata is later deleted. It is set when the audit is produced as part of a scan
and NULL for a standalone one-off audit. Phase 4's Verifier compares a pre-fix audit to a post-fix
audit of the same product, so audits must be run-scoped from creation (we do not retrofit run
identity a second time — cf. ShareOfModel step 6a).

``severity`` is a plain ``String(16)`` (not a DB enum), mirroring ``engine_runs.engine`` /
``agent_runs.status`` — adding a band is a code-only change.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Audit(Base):
    __tablename__ = "audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Run identity. NULLABLE + SET NULL (same as engine_runs.run_id): a standalone audit has no
    # run, and deleting run metadata must NOT destroy the audit's measurement.
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )

    gaps_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    spec_coverage: Mapped[float] = mapped_column(Float, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
