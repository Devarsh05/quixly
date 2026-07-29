"""Verifier — the share-of-model delta between a pre-publish baseline and a post-publish scan.

The measurement node Phase 4 exists for. It makes **no external calls of any kind**: it is pure
aggregation over ``share_of_model`` rows two scans already persisted, so every test is DB-backed —
the same contract as ``graph/share_of_model.py``.

**Shopify-free by exclusion, and that is worth stating.** This node reads no publication state at
all. If a later step wants agentic-channel readback ("is the product still published to Copilot?")
it must go through ``product.resourcePublications`` and must NOT gate on
``resourcePublicationsCount`` — the count reports 3 for a product whose list returns 4 *including*
Copilot, and enumerating ``publications`` concludes the agentic channel does not exist with no
error raised.

**``fixes.published_at`` is measurement-sacred.** Nothing here touches ``fixes``: the measured set
arrives pre-resolved from ``services/baselines.py`` and is consumed verbatim (see Gate 1 note
there). This node imports no ``Fix`` model.

Four rules, all load-bearing:

1. **The panel is bound, never rebuilt.** Pre and post must have run byte-identical queries or the
   delta is confounded. The post run is created with the baseline's ``panel_id`` and this node
   re-checks that binding AND recomputes the panel's content fingerprint — the one way an
   immutable-by-construction row can still drift is a hand edit or a bad migration.
2. **No-data is decided by ``total_queries``, NOT by ``our_rate``.** See ``_side_rates``.
3. **NULL propagates.** ``delta`` is NULL if either side is NULL; it is never ``coalesce(…, 0)``.
4. **Engines are unioned, not intersected.** An engine that ran in the baseline and produced
   nothing post still gets a row (``post_rate`` NULL), so a regression to zero coverage is visible
   rather than silently absent from the report.
"""

import logging
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.interrogator import PanelQuery, QueryPanel, _fingerprint
from app.models import AgentRun, ShareOfModel, Verification
from app.models import QueryPanel as QueryPanelRow
from app.services.baselines import MeasuredFix
from app.settings import get_settings

logger = logging.getLogger(__name__)


class VerificationAborted(RuntimeError):
    """The measurement cannot be trusted, so NOTHING is written.

    Raised on a panel mismatch, a fingerprint mismatch, a missing/invalid baseline, an empty
    measured set, or an unmet settle window without ``force``. Deliberately not a partial result:
    a delta nobody can trust is worse than no delta, because it looks like one.
    """


class CompetitorDelta(BaseModel):
    """One competitor's pre/post mention rate and the change between them."""

    pre_rate: float | None
    post_rate: float | None
    delta: float | None


class EngineDelta(BaseModel):
    """Per-engine verification result held in graph state."""

    engine: str
    pre_rate: float | None  # NULL = no data on that side — never 0.0
    post_rate: float | None
    delta: float | None  # NULL if either side is NULL
    pre_mentions: int | None
    post_mentions: int | None
    pre_total_queries: int | None
    post_total_queries: int | None
    competitors: dict[str, CompetitorDelta]


class VerificationReport(BaseModel):
    """Verifier's typed return.

    ``delta`` is a **correlational** before/after observation on a fixed panel (PRD §13), never a
    causal claim about any individual fix. ``measured_fixes`` says what was live during the
    window — provenance, not attribution.
    """

    run_id: int
    baseline_run_id: int
    panel_id: int
    panel_fingerprint: str
    published_at_max: datetime
    settle_hours: float
    settle_satisfied: bool
    measured_fixes: list[MeasuredFix]
    engines: list[EngineDelta]


class _SideRates(BaseModel):
    """One side (pre or post) of one engine, with no-data already normalized to NULL."""

    rate: float | None
    mentions: int | None
    total_queries: int | None
    competitors: dict[str, float | None]


def _side_rates(row: ShareOfModel | None) -> _SideRates:
    """Read one ``share_of_model`` row, gating no-data on ``total_queries``.

    **This deliberately does not inherit the aggregator's NULL guarantee.** Today
    ``graph/share_of_model.py`` does write ``our_rate = NULL`` (never 0.0) when an engine yields
    nothing usable — but that invariant lives in one branch of one function, ``total_queries`` and
    ``our_mentions`` are nullable columns, and no CHECK constraint ties the two together. A row
    holding ``our_rate = 0.0`` with ``total_queries = 0`` is representable, and reading its rate
    literally would compute ``0.0 - pre_rate`` and show the merchant a **fabricated regression**
    for what was really just a flaky engine.

    So: a missing row, ``total_queries IS NULL``, or ``total_queries = 0`` is NO DATA, and the rate
    is NULL regardless of what ``our_rate`` literally holds. Competitor rates on that side go NULL
    with it — they are computed over the same empty denominator. ``mentions`` / ``total_queries``
    pass through unchanged: they are factual and useful for diagnosing why a side is NULL.
    """
    if row is None:
        return _SideRates(rate=None, mentions=None, total_queries=None, competitors={})

    has_data = row.total_queries is not None and row.total_queries > 0
    competitors: dict[str, float | None] = {}
    for name, payload in (row.competitor_rates_json or {}).items():
        rate = payload.get("mention_rate") if isinstance(payload, dict) else None
        competitors[name] = rate if has_data else None

    return _SideRates(
        rate=row.our_rate if has_data else None,
        mentions=row.our_mentions,
        total_queries=row.total_queries,
        competitors=competitors,
    )


def _delta(pre: float | None, post: float | None) -> float | None:
    """``post - pre``, or NULL if either side is no-data. NEVER coalesces a NULL to zero."""
    if pre is None or post is None:
        return None
    return post - pre


async def _load_run(session: AsyncSession, run_id: int, label: str) -> AgentRun:
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise VerificationAborted(f"{label} run {run_id} not found.")
    return run


async def _assert_panel_binding(
    session: AsyncSession, post_run: AgentRun, baseline_run: AgentRun
) -> QueryPanelRow:
    """Prove both runs queried the same panel, and that the panel's content still hashes true."""
    if post_run.panel_id != baseline_run.panel_id:
        raise VerificationAborted(
            f"Panel mismatch: post run {post_run.id} ran panel {post_run.panel_id}, baseline run "
            f"{baseline_run.id} ran panel {baseline_run.panel_id}. The delta would be confounded."
        )

    row = await session.get(QueryPanelRow, post_run.panel_id)
    if row is None:
        raise VerificationAborted(f"Panel {post_run.panel_id} not found.")

    # ``upsert_panel`` never rewrites ``queries_json`` for an existing fingerprint, so a mismatch
    # here means the row was edited out of band — not something the normal path can produce.
    panel = QueryPanel(
        category=row.category,
        queries=[PanelQuery(**query) for query in row.queries_json],
        fingerprint=row.fingerprint,
    )
    recomputed = _fingerprint(panel.category, panel.queries)
    if recomputed != row.fingerprint:
        raise VerificationAborted(
            f"Panel {row.id} content does not match its fingerprint "
            f"({recomputed[:16]}… != {row.fingerprint[:16]}…); it was modified out of band."
        )
    return row


async def run_verifier(
    session: AsyncSession,
    run_id: int,
    *,
    baseline_run_id: int,
    measured_fixes: list[MeasuredFix],
    force: bool = False,
    settle_hours_required: float | None = None,
) -> VerificationReport:
    """Compute and persist the per-engine share-of-model delta for the post scan ``run_id``.

    ``measured_fixes`` is the snapshot taken at route time (``services/baselines.py``) and is
    consumed verbatim — the settle gate, ``published_at_max`` and the persisted manifest all read
    it, and ``fixes`` is NOT re-queried here. Raises ``VerificationAborted`` and writes nothing if
    the measurement cannot be trusted.
    """
    if not measured_fixes:
        # A delta with nothing published is engine-panel noise, not a measurement. Persisting one
        # would put a meaningless number in front of a merchant.
        raise VerificationAborted("No published fixes to measure; refusing to record a delta.")

    post_run = await _load_run(session, run_id, "Post")
    baseline_run = await _load_run(session, baseline_run_id, "Baseline")
    if baseline_run.shop_id != post_run.shop_id:
        raise VerificationAborted(
            f"Baseline run {baseline_run_id} belongs to shop {baseline_run.shop_id}, "
            f"not shop {post_run.shop_id}."
        )

    panel = await _assert_panel_binding(session, post_run, baseline_run)

    published_at_max = max(fix.published_at for fix in measured_fixes)
    required = (
        settle_hours_required
        if settle_hours_required is not None
        else get_settings().verify_settle_hours
    )
    # Anchored on the post run's START, not its completion: every engine query happened at or
    # after it, so this is a deliberate LOWER BOUND on the true settle time. Conservative is the
    # right bias for a gate, and it is available here (``completed_at`` is still NULL — the job
    # stamps it after this node returns).
    settle_hours = (post_run.started_at - published_at_max).total_seconds() / 3600
    settle_satisfied = settle_hours >= required
    if not settle_satisfied and not force:
        raise VerificationAborted(
            f"Only {settle_hours:.1f}h since the last publish; {required:.0f}h are required for "
            "engines to re-crawl. Pass force to measure anyway (the row is labelled unsettled)."
        )

    pre_rows = {
        row.engine: row
        for row in (
            await session.execute(
                select(ShareOfModel).where(ShareOfModel.run_id == baseline_run_id)
            )
        ).scalars()
    }
    post_rows = {
        row.engine: row
        for row in (
            await session.execute(select(ShareOfModel).where(ShareOfModel.run_id == run_id))
        ).scalars()
    }

    manifest = [fix.model_dump(mode="json") for fix in measured_fixes]
    engines_out: list[EngineDelta] = []

    # UNION, not intersection: an engine that ran pre and vanished post must still produce a row,
    # or a total regression reads as "no result" instead of a regression.
    for engine in sorted(set(pre_rows) | set(post_rows)):
        pre = _side_rates(pre_rows.get(engine))
        post = _side_rates(post_rows.get(engine))

        competitors = {
            name: CompetitorDelta(
                pre_rate=pre.competitors.get(name),
                post_rate=post.competitors.get(name),
                delta=_delta(pre.competitors.get(name), post.competitors.get(name)),
            )
            # Union here too: the alias set is code and can change between scans.
            for name in sorted(set(pre.competitors) | set(post.competitors))
        }

        result = EngineDelta(
            engine=engine,
            pre_rate=pre.rate,
            post_rate=post.rate,
            delta=_delta(pre.rate, post.rate),
            pre_mentions=pre.mentions,
            post_mentions=post.mentions,
            pre_total_queries=pre.total_queries,
            post_total_queries=post.total_queries,
            competitors=competitors,
        )

        statement = insert(Verification).values(
            run_id=run_id,
            baseline_run_id=baseline_run_id,
            shop_id=post_run.shop_id,
            engine=engine,
            panel_id=panel.id,
            panel_fingerprint=panel.fingerprint,
            pre_rate=result.pre_rate,
            post_rate=result.post_rate,
            delta=result.delta,
            pre_mentions=result.pre_mentions,
            post_mentions=result.post_mentions,
            pre_total_queries=result.pre_total_queries,
            post_total_queries=result.post_total_queries,
            competitor_deltas_json={n: c.model_dump() for n, c in competitors.items()},
            measured_fixes_json=manifest,
            measured_fix_count=len(measured_fixes),
            published_at_max=published_at_max,
            settle_hours=settle_hours,
            settle_satisfied=settle_satisfied,
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_verifications_run_engine",
            set_={
                "baseline_run_id": statement.excluded.baseline_run_id,
                "pre_rate": statement.excluded.pre_rate,
                "post_rate": statement.excluded.post_rate,
                "delta": statement.excluded.delta,
                "pre_mentions": statement.excluded.pre_mentions,
                "post_mentions": statement.excluded.post_mentions,
                "pre_total_queries": statement.excluded.pre_total_queries,
                "post_total_queries": statement.excluded.post_total_queries,
                "competitor_deltas_json": statement.excluded.competitor_deltas_json,
                "measured_fixes_json": statement.excluded.measured_fixes_json,
                "measured_fix_count": statement.excluded.measured_fix_count,
                "published_at_max": statement.excluded.published_at_max,
                "settle_hours": statement.excluded.settle_hours,
                "settle_satisfied": statement.excluded.settle_satisfied,
                # created_at intentionally NOT touched — first-seen timestamp survives a recompute
                # (mirrors share_of_model's upsert).
            },
        )
        await session.execute(statement)
        engines_out.append(result)

    await session.commit()

    if not settle_satisfied:
        logger.warning(
            "Verification run %s recorded UNSETTLED (%.1fh < %.0fh required)",
            run_id,
            settle_hours,
            required,
        )

    return VerificationReport(
        run_id=run_id,
        baseline_run_id=baseline_run_id,
        panel_id=panel.id,
        panel_fingerprint=panel.fingerprint,
        published_at_max=published_at_max,
        settle_hours=settle_hours,
        settle_satisfied=settle_satisfied,
        measured_fixes=measured_fixes,
        engines=engines_out,
    )
