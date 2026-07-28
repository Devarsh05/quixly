"""Verification routes (PRD §6, §9) — measure share-of-model uplift after a publish.

Mirrors ``api.scan``'s shape: ``by-domain`` keying (the cross-service identity the app shell
holds), shared-secret dependency, 202-then-poll for the async job, and a read route that returns
persisted rows rather than recomputing.

**Everything that can refuse a measurement is checked HERE, before the run exists** — an empty
measured set, no usable baseline, an unmet settle window. A full panel fan-out is real money and
real rate-limit budget; discovering the measurement was untrustworthy afterwards would spend it
for nothing.

**Gate 1 — the measured set is snapshotted here, once.** ``resolve_measured_set`` runs at
validation time and the resulting list is handed to the job, which consumes it verbatim. It is
NOT re-resolved at aggregation time. A publish landing in between would otherwise let the gate
decision the merchant just got (the 409, the baseline choice, ``published_at_max``) disagree with
the manifest that finally lands — both internally consistent, silently describing different
windows.

**Panel binding: bind, never rebuild.** Unlike ``start_scan``, this route does NOT call
``build_query_panel()``. It creates the run with the BASELINE's ``panel_id``, so pre and post run
byte-identical queries. Calling the builder here would silently bind a post-scan to a *new* panel
row after any Interrogator edit, and the delta would be confounded with nothing raised.
"""

from datetime import UTC, datetime
from typing import Annotated

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db import get_db
from app.models import AgentRun, AgentRunStatus, Shop, Verification
from app.services.baselines import resolve_measured_set, select_baseline_run
from app.services.runs import create_agent_run
from app.settings import get_settings

router = APIRouter(prefix="/shops", tags=["verify"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


class VerifyRunResponse(BaseModel):
    run_id: int
    baseline_run_id: int
    status: AgentRunStatus
    measured_fix_count: int
    settle_satisfied: bool


class MeasuredFixView(BaseModel):
    fix_id: int
    product_id: int
    type: str
    target: str
    published_at: datetime


class CompetitorDeltaView(BaseModel):
    pre_rate: float | None
    post_rate: float | None
    delta: float | None


class EngineDeltaView(BaseModel):
    engine: str
    # NULL = no data on that side (never 0.0); ``delta`` is NULL if either side is.
    pre_rate: float | None
    post_rate: float | None
    delta: float | None
    pre_mentions: int | None
    post_mentions: int | None
    pre_total_queries: int | None
    post_total_queries: int | None
    competitors: dict[str, CompetitorDeltaView]


class VerificationResponse(BaseModel):
    """One verification run's result.

    ``delta`` is a **correlational** before/after observation on a fixed panel (PRD §13), never a
    causal claim about any individual fix. ``measured_fixes`` is provenance — what was live during
    the window — not attribution.
    """

    run_id: int
    baseline_run_id: int | None
    status: AgentRunStatus
    panel_fingerprint: str | None
    published_at_max: datetime | None
    settle_hours: float | None
    settle_satisfied: bool | None
    measured_fixes: list[MeasuredFixView]
    engines: list[EngineDeltaView]


async def _enqueue(
    run_id: int, baseline_run_id: int, measured_fixes: list[dict], force: bool
) -> None:
    pool: ArqRedis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job("run_verify_task", run_id, baseline_run_id, measured_fixes, force)
    finally:
        await pool.aclose()


async def _resolve_shop(db: AsyncSession, shop_domain: str) -> Shop:
    shop = (
        await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    ).scalar_one_or_none()
    if shop is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shop not found.")
    return shop


@router.post(
    "/by-domain/{shop_domain}/verify",
    response_model=VerifyRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_internal_api_key)],
)
async def start_verification(
    shop_domain: str,
    db: DbSession,
    baseline_run_id: int | None = None,
    force: bool = False,
) -> VerifyRunResponse:
    """Enqueue a post-publish scan + delta, returning 202 with the run id.

    ``force`` measures anyway when the settle window is unmet. It is not a bypass: the row lands
    with ``settle_satisfied = false`` and the real elapsed ``settle_hours``, so an early
    measurement is permanently labelled and can never be read back as a settled one.
    """
    shop = await _resolve_shop(db, shop_domain)

    # THE SNAPSHOT (Gate 1). Resolved once, here, and threaded through the queue unchanged.
    #
    # Two steps, deliberately. First the shop's whole published history, to learn ``max`` — the
    # baseline must predate the MOST RECENT publish, not the earliest. Anchoring on the earliest
    # would make a shop that published once long ago permanently unverifiable: no scan could ever
    # be old enough, and every request would 409 with a usable baseline sitting right there.
    # Then the set is re-filtered against the chosen baseline, so a fix already baked INTO it is
    # not counted as measured.
    published = await resolve_measured_set(db, shop.id)
    if not published:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No fixes have been published and verified for this shop; nothing to measure.",
        )

    baseline = await select_baseline_run(
        db,
        shop.id,
        before=max(fix.published_at for fix in published),
        baseline_run_id=baseline_run_id,
    )
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No usable pre-publish baseline scan: a baseline must be a completed scan that "
                "produced share-of-model rows and finished before the first published fix."
            ),
        )

    measured = [fix for fix in published if fix.published_at > baseline.completed_at]
    if not measured:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Every published fix predates the baseline scan, so it is already reflected in "
                "the baseline; there is no change to measure."
            ),
        )

    published_at_max = max(fix.published_at for fix in measured)
    required = get_settings().verify_settle_hours
    elapsed_hours = (datetime.now(UTC) - published_at_max).total_seconds() / 3600
    settle_satisfied = elapsed_hours >= required
    if not settle_satisfied and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only {elapsed_hours:.1f}h since the last publish; {required:.0f}h are required "
                "for engines to re-crawl. Pass force=true to measure anyway — the result will be "
                "recorded as unsettled."
            ),
        )

    # Bind to the baseline's panel — never build_query_panel(). See the module docstring.
    run = await create_agent_run(db, shop.id, baseline.panel_id)
    await db.commit()  # load-bearing: the run must exist before the job is enqueued

    await _enqueue(
        run.id,
        baseline.id,
        [fix.model_dump(mode="json") for fix in measured],
        force,
    )

    return VerifyRunResponse(
        run_id=run.id,
        baseline_run_id=baseline.id,
        status=run.status,
        measured_fix_count=len(measured),
        settle_satisfied=settle_satisfied,
    )


@router.get(
    "/by-domain/{shop_domain}/verification",
    response_model=VerificationResponse,
    dependencies=[Depends(require_internal_api_key)],
)
async def get_verification(
    shop_domain: str,
    db: DbSession,
    run_id: int | None = None,
) -> VerificationResponse:
    """The persisted per-engine deltas for a verification run (the shop's latest if unspecified).

    READS rows; it does not recompute. Resolution is purely by ``run_id``, mirroring
    ``api.scan.get_report`` — a still-running verification has no rows yet, so it naturally
    reports ``status=running`` with ``engines: []``, no special-casing.

    With no ``run_id``, resolves the latest run that actually PRODUCED a verification. Deriving it
    from the rows (rather than from a run-kind discriminator ``agent_runs`` does not have) is exact
    for this purpose — the same approach ``api.fixes.list_fixes`` takes.
    """
    shop = await _resolve_shop(db, shop_domain)

    if run_id is None:
        run_id = await db.scalar(
            select(Verification.run_id)
            .where(Verification.shop_id == shop.id)
            .order_by(Verification.run_id.desc())
            .limit(1)
        )
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No verification run found."
        )

    run = (
        await db.execute(
            select(AgentRun).where(AgentRun.id == run_id, AgentRun.shop_id == shop.id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")

    rows = (
        await db.execute(
            select(Verification)
            .where(Verification.run_id == run_id, Verification.shop_id == shop.id)
            .order_by(Verification.engine)
        )
    ).scalars().all()

    # The manifest is denormalized across an engine's rows on purpose (see models/verification.py);
    # dedup on read, never by normalizing storage.
    first = rows[0] if rows else None

    return VerificationResponse(
        run_id=run_id,
        baseline_run_id=first.baseline_run_id if first else None,
        status=run.status,
        panel_fingerprint=first.panel_fingerprint if first else None,
        published_at_max=first.published_at_max if first else None,
        settle_hours=first.settle_hours if first else None,
        settle_satisfied=first.settle_satisfied if first else None,
        measured_fixes=[
            MeasuredFixView(**fix) for fix in (first.measured_fixes_json if first else [])
        ],
        engines=[
            EngineDeltaView(
                engine=row.engine,
                pre_rate=row.pre_rate,
                post_rate=row.post_rate,
                delta=row.delta,
                pre_mentions=row.pre_mentions,
                post_mentions=row.post_mentions,
                pre_total_queries=row.pre_total_queries,
                post_total_queries=row.post_total_queries,
                competitors={
                    name: CompetitorDeltaView(**payload)
                    for name, payload in (row.competitor_deltas_json or {}).items()
                },
            )
            for row in rows
        ],
    )
