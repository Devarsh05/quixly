"""The ``audits`` table (PRD §8) — one deterministic product-audit result.

Produced by ``app.graph.audit`` from the rubric in ``services.audit_rubric``: the gaps found in a
product's catalog data (missing description / GTIN / metafields, not discoverable, absent spec
attributes), a ``severity`` band, and **two** coverage ratios.

**Both coverage numbers are kept; neither replaces the other** (step 2b). ``structured_coverage``
counts spec families carried by a metafield — the headline AI-legibility score, what engines
actually read. ``spec_coverage`` counts families stated in the PROSE (title/body). The *difference*
between them is the addressable set the Optimizer can fix automatically, which is why storing only
one of them would lose the finding.

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

    # The class the rubric scored against (coffee / equipment / other), snapshotted so Phase 4's
    # Verifier compares like-for-like and the report can break down by class.
    product_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gaps_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    # BOTH coverage columns are NULLABLE for the same reason: spec scoring only applies to classes
    # with a grounded vocabulary (coffee today). Equipment / other / not-audited (draft) products
    # carry NULL — never a misleading 0.0.
    #
    # PROSE coverage — families stated in title/body.
    spec_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    # STRUCTURED coverage — families carried by a metafield. The headline AI-legibility score.
    structured_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Bands: none | low | medium | high, plus ``not_audited`` for products excluded from the
    # population (not visible). Plain String, like the other status columns.
    severity: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
