"""The ``fixes`` table (PRD §8) — one proposed change or merchant to-do per row.

Produced by the Optimizer (``app.graph.optimizer``) from a product's audit gaps. Every fillable
fix is **grounded**: it carries a ``source_json`` citation naming the source field + verbatim
snippet each proposed value was extracted from (PRD §6 — a fix with no traceable source is a bug).

**Fillable fixes vs merchant to-dos are one table, distinguished by ``type``.** A ``merchant_todo``
has ``after_json = NULL`` and a ``reason``; it is informational and **never publishable**. Only
``metafield`` / ``description`` fixes with a non-NULL ``after_json`` are eligible to publish (the
step-4 Publisher hard-filters on that). ``missing_gtin`` is always a ``merchant_todo`` — a GTIN
cannot be derived, and proposing one would violate PRD §13.

Staleness (spike answer C): ``base_source_hash`` is the exact guard, captured at propose over the
source fields the fix grounded on; ``base_shopify_updated_at`` is a coarse guard populated in
step 4 (we do not ingest Shopify's ``updatedAt`` yet). Rollback (step 4) appends an inverse row via
``reverts_fix_id``. ``run_id`` is a nullable FK from day one (standing convention).

``type`` / ``status`` are plain ``String`` with ``StrEnum`` vocabularies (mirrors
``agent_runs.status`` / ``engine_runs.engine`` — adding a value is code-only, no migration).
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FixType(enum.StrEnum):
    metafield = "metafield"
    description = "description"
    merchant_todo = "merchant_todo"
    # ``revert`` is added by step 4 (Publisher/rollback); the reverts_fix_id column exists now.


class FixStatus(enum.StrEnum):
    proposed = "proposed"
    approved = "approved"
    published = "published"
    verified = "verified"
    rejected = "rejected"
    stale = "stale"
    reverted = "reverted"


class Fix(Base):
    __tablename__ = "fixes"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True, nullable=True
    )

    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # What the fix touches: "metafield:custom.roast_level", "body_html", or the gap for a to-do
    # ("spec:altitude", "gtin").
    target: Mapped[str] = mapped_column(String(255), nullable=False)

    # ``none_as_null=True`` on every nullable JSONB column: a Python ``None`` must persist as SQL
    # NULL, not JSONB ``'null'``. Load-bearing for ``after_json``: the step-4 Publisher filters
    # ``after_json IS NOT NULL`` to find publishable fixes, and a JSONB ``'null'`` would match a
    # merchant_todo and publish it (PRD §13). Same for querying ``source_json IS NULL`` on to-dos.
    before_json: Mapped[dict | list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    # NULL for a merchant_todo (nothing to write) — the load-bearing publishability signal.
    after_json: Mapped[dict | list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    # Grounding citation(s): [{attribute, source_field, snippet}]. NULL for a merchant_todo.
    source_json: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)

    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    base_source_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_shopify_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reverts_fix_id: Mapped[int | None] = mapped_column(
        ForeignKey("fixes.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
