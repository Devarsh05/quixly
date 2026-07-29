"""The ``verifications`` table (PRD §8, superseded) — one share-of-model delta per engine.

**PRD §8 sketches ``verifications(id, fix_id, pre_rate, post_rate, delta, ts)``. That grain is
superseded here, deliberately.** ``pre_rate`` / ``post_rate`` are shop/engine-level quantities
computed over a fixed query panel; hanging them off a ``fix_id`` asserts that *this fix* moved
*this rate*. Two fixes published 42 seconds apart into one measurement window make that
attribution unrecoverable in principle, and PRD §13 names the trap: sell the clean leading metric
(share-of-model before/after on a fixed panel), treat precise attribution as a later feature.
Writing one shop-level delta onto N fix rows would manufacture N causal claims from a single
correlational observation.

So the grain is **(verification run, engine)** — ``uq_verifications_run_engine`` — and per-fix
survives only as **annotation**: ``measured_fixes_json`` is an immutable manifest of what was live
during the window. JSONB rather than a join table for two reasons: it is a *snapshot* (a FK'd
table would CASCADE rows away and silently rewrite history a merchant was already shown), and a
relational edge invites exactly the per-fix join this grain exists to prevent. A GIN index answers
"which verifications measured fix N" without implying fix N caused anything. The manifest
duplicating across an engine's rows is intentional denormalization — dedup by ``run_id`` on the
read side, never by normalizing
storage.

**The FK asymmetry is deliberate and mirrors precedent.** ``run_id`` (the POST scan) CASCADEs like
``share_of_model.run_id`` — a derived aggregate dies with its run. ``baseline_run_id`` and
``panel_id`` are nullable and SET NULL like ``engine_runs.run_id``: every pre-side value is
**denormalized onto this row**, so deleting old run metadata must not destroy a measurement record
a merchant was shown. ``panel_fingerprint`` is the durable panel identity.

``pre_rate`` / ``post_rate`` / ``delta`` are NULLABLE for the same reason
``share_of_model.our_rate`` is: ``0/0`` is *no data*, not *0%*. A NULL side propagates to a NULL
delta and is never coerced to zero — a delta computed against a degraded scan is a fabricated
uplift, not a measurement.

``published_at_max`` / ``settle_hours`` are NOT NULL: an empty measured set is refused before any
row can exist, so both are always computable. ``settle_hours`` is signed — a fix published after
the post scan started yields a negative value, which is honest and lands ``settle_satisfied``
false.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Verification(Base):
    __tablename__ = "verifications"
    __table_args__ = (
        UniqueConstraint("run_id", "engine", name="uq_verifications_run_engine"),
        # Answers "which verifications measured fix N" without a relational edge implying cause.
        Index(
            "ix_verifications_measured_fixes",
            "measured_fixes_json",
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # The POST scan's agent_run. NOT NULL — a verification without its post run is meaningless.
    # CASCADE mirrors share_of_model.run_id: a derived aggregate dies with its run.
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The PRE scan's agent_run. NULLABLE + SET NULL: pre_* are denormalized below, so the
    # measurement record survives deletion of the baseline's run metadata.
    baseline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id", ondelete="CASCADE"), index=True, nullable=False
    )

    engine: Mapped[str] = mapped_column(String(32), nullable=False)

    # Panel identity. Both scans MUST have run this panel or the delta is confounded; the
    # fingerprint is the durable half and outlives the row.
    panel_id: Mapped[int | None] = mapped_column(
        ForeignKey("query_panels.id", ondelete="SET NULL"), nullable=True
    )
    panel_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    # NULL = no data on that side. NEVER 0.0. See module docstring.
    pre_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Factual diagnostics, passed through even when the rate is NULLed. total_queries is what the
    # reader compares to judge coverage comparability — derived at read time, not flagged here
    # (mirrors api/scan.py deriving ``coverage`` rather than persisting it).
    pre_mentions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    post_mentions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pre_total_queries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    post_total_queries: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # {name: {pre_rate, post_rate, delta}} — same NULL propagation as the store's own rate.
    competitor_deltas_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # [{fix_id, product_id, type, target, published_at}] — annotation, NOT attribution.
    measured_fixes_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    measured_fix_count: Mapped[int] = mapped_column(Integer, nullable=False)

    published_at_max: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settle_hours: Mapped[float] = mapped_column(Float, nullable=False)
    settle_satisfied: Mapped[bool] = mapped_column(Boolean, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
