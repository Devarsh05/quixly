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
      credit the window with a change it never saw. On ``select_baseline_run``'s primary tier this
      filter excludes nothing (the baseline already predates every published fix); on its fallback
      tier it is what correctly narrows M to the measurable subset, and it also carries the
      pinned-``baseline_run_id`` case. It keeps "measured means after the baseline" true by
      construction rather than by the selector's good behaviour.
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


async def _latest_scan_before(
    session: AsyncSession,
    shop_id: int,
    cutoff: datetime,
    baseline_run_id: int | None,
) -> AgentRun | None:
    """The most recent completed SCAN run for ``shop_id`` finishing strictly before ``cutoff``.

    **"Completed" does not identify a scan, and neither does ``panel_id``.** Every run on the dev
    store — the fix run and four publish runs included — is ``completed`` and carries the same
    ``panel_id``, because ``agent_runs.panel_id`` is NOT NULL and non-scan runs must borrow one.
    One run is ``completed`` with zero ``engine_runs`` and zero ``share_of_model`` rows. So a run
    qualifies only by what it PRODUCED: ``EXISTS (share_of_model WHERE run_id = …)``. This mirrors
    ``api/fixes.py`` deriving "the latest fix run" from the fixes themselves.
    """
    statement = (
        select(AgentRun)
        .where(
            AgentRun.shop_id == shop_id,
            AgentRun.status == AgentRunStatus.completed,
            AgentRun.completed_at.is_not(None),
            AgentRun.completed_at < cutoff,
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


async def select_baseline_run(
    session: AsyncSession,
    shop_id: int,
    *,
    published: list[MeasuredFix],
    baseline_run_id: int | None = None,
) -> AgentRun | None:
    """The pre-publish baseline scan for ``shop_id``, by one two-tier rule.

        The latest scan predating ``min(published_at)``, if one exists;
        otherwise the latest scan predating ``max(published_at)``.

    **The anchor is derived HERE, never passed in.** Eligibility ("a baseline must predate a real
    publish to be a valid pre-state") and selection ("which eligible scan") are different things,
    and a caller that computes the anchor is a caller that can get it wrong — one did.

    **Tier one, the primary rule, is ``min``.** Anchoring on the LAST publish systematically
    understates uplift on any shop with staggered publishes: a baseline sitting between two
    publishes bakes the earlier fix into the pre-rate *and* drops it from the measured set, so the
    very change being measured is counted as part of the "before". Anchoring on the earliest makes
    the chosen baseline predate every published fix, so the caller's ``published_at > completed_at``
    filter excludes nothing and M is as large as it honestly can be.

    **Tier two exists because ``min`` alone silently bricks a real install pattern** — install →
    publish → first scan only later. Nothing predates that first publish, so a min-only rule
    returns no baseline and the shop is **permanently unmeasurable**, including for every fix it
    publishes afterwards. Falling back to ``max`` picks the latest scan that is still a genuine
    pre-state for at least the most recent publish.

    In the fallback path M is deliberately a **SUBSET**: the caller's ``after`` filter drops any
    fix published before the chosen baseline. That is the honest outcome, not a compromise —
    there is no pre-state anywhere in the data to measure those fixes against, so the alternative
    is not a better number but a fabricated one. The manifest persisted with the result names only
    what was actually measured, so a subset is never reported as the whole.

    Takes the caller's already-resolved ``published`` snapshot rather than re-querying ``fixes``,
    so the route still reads the measured set exactly once (Gate 1).

    With ``baseline_run_id`` set, that run is validated against the same predicates instead of
    being taken on trust — an explicit override must not be a way around the rules.
    """
    if not published:
        # Nothing published means no publish to predate, so no run can be a valid pre-state.
        return None

    timestamps = [fix.published_at for fix in published]
    return await _latest_scan_before(
        session, shop_id, min(timestamps), baseline_run_id
    ) or await _latest_scan_before(session, shop_id, max(timestamps), baseline_run_id)
