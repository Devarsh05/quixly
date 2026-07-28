"""Baseline selection + measured-set resolution — the inputs a verification is pinned to.

Kept OUT of ``graph/verifier.py`` (mirroring ``services/panels.py`` / ``services/runs.py``) so the
route can validate *before* creating a run and spending a full panel of engine calls.

**THE MEASURED SET IS SNAPSHOTTED HERE, ONCE, AND IS NEVER RE-RESOLVED.**
``resolve_measured_set`` runs at the route. The list it returns is threaded through the enqueue
into the job and on into the verifier node, which consumes it verbatim: the settle gate,
``published_at_max`` and the persisted ``measured_fixes_json`` all read that one snapshot. The job
does NOT re-query ``fixes``. If it did, a publish landing between route validation and aggregation
would make the gate decision (baseline validity, the settle 409) disagree with the manifest that
gets persisted — the same TOCTOU shape as a same-day run collision, but silent, because both
halves would look internally consistent.

The other half of that contract: **this module SELECTs ``fixes`` and writes nothing.**
``fixes.published_at`` is the "went live at" anchor the whole measurement hangs off; the Publisher
is its only writer.
"""

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, AgentRunStatus, Fix, FixStatus, Product, ShareOfModel


class MeasuredFix(BaseModel):
    """One published fix that was live during the measurement window.

    Annotation, not attribution — see ``models/verification.py``. Serialized with
    ``model_dump(mode="json")`` for both the Arq payload and the JSONB manifest.
    """

    fix_id: int
    product_id: int
    type: str
    target: str
    published_at: datetime


async def resolve_measured_set(
    session: AsyncSession,
    shop_id: int,
    *,
    after: datetime | None = None,
) -> list[MeasuredFix]:
    """Snapshot the fixes that went live for ``shop_id`` (optionally after ``after``).

    Three exclusions, each deliberate:

    * ``status`` must be ``verified``, **not** ``published``. ``published`` is the intermediate
      state between the Shopify write and the confirming re-read — a fix sitting there may not be
      live at all. ``publish_failed`` / ``stale`` / ``rejected`` / ``approved`` / ``proposed``
      never went live.
    * ``published_at IS NOT NULL``. Redundant under the Publisher's invariant (it stamps the column
      only on a confirmed re-read) but kept as a defensive guard — this predicate is what makes
      "went live" mean something, and it should not depend on another module's discipline.
    * ``published_at > after`` when a baseline is already chosen: a fix that went live before the
      baseline scan finished is already baked INTO the baseline, so counting it as measured would
      credit the window with a change it never saw.
    """
    statement = (
        select(Fix)
        .join(Product, Product.id == Fix.product_id)
        .where(
            Product.shop_id == shop_id,
            Fix.status == FixStatus.verified,
            Fix.published_at.is_not(None),
        )
        .order_by(Fix.published_at, Fix.id)
    )
    if after is not None:
        statement = statement.where(Fix.published_at > after)

    rows = (await session.execute(statement)).scalars().all()
    return [
        MeasuredFix(
            fix_id=fix.id,
            product_id=fix.product_id,
            type=fix.type,
            target=fix.target,
            published_at=fix.published_at,
        )
        for fix in rows
    ]


async def select_baseline_run(
    session: AsyncSession,
    shop_id: int,
    *,
    before: datetime,
    baseline_run_id: int | None = None,
) -> AgentRun | None:
    """The latest completed SCAN run for ``shop_id`` that finished before ``before``.

    **"Completed" does not identify a scan, and neither does ``panel_id``.** Every run on the dev
    store — the fix run and four publish runs included — is ``completed`` and carries the same
    ``panel_id``, because ``agent_runs.panel_id`` is NOT NULL and non-scan runs must borrow one.
    One run is ``completed`` with zero ``engine_runs`` and zero ``share_of_model`` rows. So a run
    qualifies as a baseline only by what it PRODUCED: ``EXISTS (share_of_model WHERE run_id = …)``.
    This mirrors ``api/fixes.py`` deriving "the latest fix run" from the fixes themselves.

    ``before`` is ``max(published_at)`` over the shop's published history — the MOST RECENT
    publish, not the earliest. A baseline has to predate the changes being measured, and anchoring
    on the earliest publish would make a shop that published once long ago permanently
    unverifiable: no scan could ever be old enough. Anchoring on the latest guarantees at least
    that publish falls inside the window; the caller then filters the measured set down to fixes
    that landed after the chosen baseline, so anything already baked into it is excluded.

    With ``baseline_run_id`` set, that run is validated against the same predicates instead of
    being taken on trust — an explicit override must not be a way around the rules.
    """
    statement = (
        select(AgentRun)
        .where(
            AgentRun.shop_id == shop_id,
            AgentRun.status == AgentRunStatus.completed,
            AgentRun.completed_at.is_not(None),
            AgentRun.completed_at < before,
            # The scan-ness test: it produced aggregates, so EngineRunner + Extractor + the
            # aggregator all ran under it.
            exists().where(ShareOfModel.run_id == AgentRun.id),
        )
        .order_by(AgentRun.completed_at.desc(), AgentRun.id.desc())
        .limit(1)
    )
    if baseline_run_id is not None:
        statement = statement.where(AgentRun.id == baseline_run_id)

    return (await session.execute(statement)).scalar_one_or_none()
