"""The ``fixes`` table (PRD §8) — one proposed change or merchant to-do per row.

Produced by the Optimizer (``app.graph.optimizer``) from a product's audit gaps. Every fillable
fix is **grounded**: it carries a ``source_json`` citation naming the source field + verbatim
snippet each proposed value was extracted from (PRD §6 — a fix with no traceable source is a bug).

**Fillable fixes vs merchant to-dos are one table, distinguished by ``type``.** A ``merchant_todo``
has ``after_json = NULL`` and a ``reason``; it is informational and **never publishable**. Only
``metafield`` / ``description`` fixes with a non-NULL ``after_json`` are eligible to publish (the
step-4 Publisher hard-filters on that). ``missing_gtin`` is always a ``merchant_todo`` — a GTIN
cannot be derived, and proposing one would violate PRD §13.

Staleness: ``base_source_hash`` is the coarse per-product guard, captured at propose over a
**writer-stable** projection of the source fields the fix grounded on
(``services.catalog.stable_source_hash`` — see its docstring for why the projection, not the raw
columns). The *exact* per-fix guard is ``before_json``: the Publisher refuses to write unless the
live field still equals it byte-for-byte. ``base_shopify_updated_at`` remains unpopulated (we do not
ingest Shopify's ``updatedAt``). Rollback appends an inverse row via ``reverts_fix_id`` — not yet
built. ``run_id`` is a nullable FK from day one (standing convention).

**Publish audit trail (step 4) — one meaning per column, do not overload either.**
``published_at`` is set ONLY on a confirmed post-write re-read, never on the mutation's HTTP 200:
it means "this fix is verified live on the store", and it is what Phase 4's Verifier and any
staged-rollout audit read. ``publish_error`` carries **why a publish did not land** — a refusal
(``stale``: the product moved under us) or a failure (``publish_failed``: the write errored or the
re-read did not confirm) — and is the text the merchant is shown. It is also what distinguishes a
Publisher-refused ``stale`` row from one Step 3's supersede marked stale, which carries NULL.
``reason`` keeps purely grounding/to-do semantics and is never written by the Publisher.

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
    # ``metafield`` now means a TAXONOMY metafield (``shopify`` namespace,
    # ``list.metaobject_reference``) — step 2d retired the ``custom.*`` write target. Its
    # ``after_json`` carries the canonical ``taxonomy_value_gid`` + ``value``; the Step-4 Publisher
    # resolves the per-shop metaobject-entry GID at write time (spike L1).
    metafield = "metafield"
    description = "description"
    # ``category`` assigns a Standard-taxonomy product category (step 2d). It is the hard
    # precondition for every taxonomy metafield write and is APPROVAL-GATED like a publish — a wrong
    # category has real tax/channel consequences — so it is never bundled with low-risk fills.
    category = "category"
    merchant_todo = "merchant_todo"
    # ``revert`` is added by step 4 (Publisher/rollback); the reverts_fix_id column exists now.


class FixStatus(enum.StrEnum):
    proposed = "proposed"
    approved = "approved"
    # ``published`` is a real INTERMEDIATE state, committed after the Shopify write and before the
    # verifying re-read. A crash between the two leaves ``published``, which the next publish run
    # reconciles by re-reading — it is never re-written. Only a confirmed re-read reaches
    # ``verified``; a 200 with a wrong or absent result is a failure, not a success.
    published = "published"
    verified = "verified"
    rejected = "rejected"
    stale = "stale"
    # ``publish_failed`` (step 4): the write errored, its userErrors were non-empty, or the
    # post-write re-read did not confirm. TERMINAL and never auto-retried — a silent retry of an
    # append-only description write is exactly the double-append this state exists to prevent. The
    # cause lives in ``publish_error``. Code-only addition: ``status`` is a plain ``String(16)``.
    publish_failed = "publish_failed"
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

    # Publish audit trail (step 4) — see the module docstring. NULL on every non-published row.
    publish_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
