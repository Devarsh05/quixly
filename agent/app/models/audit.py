"""The ``audits`` table (PRD §8) — one deterministic product-audit result.

Produced by ``app.graph.audit`` from the rubric in ``services.audit_rubric``: the gaps found in a
product's catalog data (missing description / GTIN / metafields, not discoverable, absent spec
attributes), a ``severity`` band, and **two** coverage ratios.

**Two channel-specific coverage numbers; neither replaces the other, and they are NOT averaged**
(step 2d — the 2c spike proved legibility is channel-specific). ``taxonomy_coverage`` is the
headline machine-readable score: families written to their **taxonomy attribute** (the ``shopify``
namespace channel Shopify feeds to agentic surfaces), over the **taxonomy-home families applicable
to this product** (roast, origin, coffee_product_form; + decaffeination_method for decaf — so 3 or
4). ``spec_coverage`` is PROSE coverage: families stated in title/body, over the **applicable spec
families** (8 or 9). The *difference* is the addressable set.

**Step 2d replaced ``structured_coverage`` with ``taxonomy_coverage``.** The 2b column counted
``custom.*`` metafields, which the spike proved are NOT the AI-legible channel; its old meaning is
now wrong, so it was DROPPED (not repurposed) rather than left to mislead a reader. ``custom.*`` is
no longer a write target.

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
    # PROSE coverage — families stated in title/body / applicable spec families (8 or 9).
    spec_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    # TAXONOMY coverage — families written to their ``shopify`` taxonomy attribute / applicable
    # taxonomy-home families (3 or 4). The headline AI-legibility score (the filter channel). NOT
    # blended with prose. Replaced the 2b ``structured_coverage`` (which counted ``custom.*``).
    taxonomy_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Bands: none | low | medium | high, plus ``not_audited`` for products excluded from the
    # population (not visible). Plain String, like the other status columns.
    severity: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
