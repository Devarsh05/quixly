"""Visibility scan orchestration + report (PRD §9).

Both routes are keyed on ``shop_domain`` (the cross-service identity the app shell holds),
mirroring ``GET /shops/by-domain/{shop_domain}/ingest/latest`` — the integer PK is agent-internal.

``POST /shops/by-domain/{shop_domain}/scan`` is QUEUED: one run makes tens of engine + extractor
calls, far too long to hold a request open. The route creates and COMMITS the panel + agent_run,
then enqueues the Arq job and returns 202 with the run_id. A crash before/at task start is
therefore visible as a stuck ``running`` run, never a missing one.

``GET /shops/by-domain/{shop_domain}/report`` READS the persisted ``share_of_model`` rows for a run — it does
NOT re-aggregate. Resolution is purely by ``run_id`` (``share_of_model`` is keyed on
``(run_id, engine)`` since step 6a): a still-running run has no rows yet, so it naturally reports
``status=running`` with ``engines: []`` — no special-casing — and two same-day scans never bleed
into each other. ``coverage`` is derived from the run's panel (not persisted); a NULL ``our_rate``
(fully-degraded engine) serializes as JSON ``null``, never ``0.0``.
"""

from datetime import datetime
from typing import Annotated

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db import get_db
from app.graph.interrogator import build_query_panel
from app.models import AgentRun, AgentRunStatus, ShareOfModel, Shop
from app.models import QueryPanel as QueryPanelRow
from app.services.panels import upsert_panel
from app.services.runs import create_agent_run
from app.settings import get_settings

router = APIRouter(prefix="/shops", tags=["scan"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


class ScanResponse(BaseModel):
    run_id: int
    status: AgentRunStatus


class EngineReport(BaseModel):
    engine: str
    our_rate: float | None  # NULL = no data (never 0.0)
    our_mentions: int | None
    total_queries: int | None
    coverage: float
    competitor_rates: dict


class ReportResponse(BaseModel):
    run_id: int
    status: AgentRunStatus
    period: str | None
    started_at: datetime | None
    completed_at: datetime | None
    engines: list[EngineReport]


async def _enqueue(run_id: int) -> None:
    pool: ArqRedis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job("run_scan_task", run_id)
    finally:
        await pool.aclose()


@router.post(
    "/by-domain/{shop_domain}/scan",
    response_model=ScanResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_internal_api_key)],
)
async def start_scan(shop_domain: str, db: DbSession) -> ScanResponse:
    """Create + commit a panel and a ``running`` agent_run, enqueue the scan job, return 202.

    Keyed on ``shop_domain`` — the cross-service identity the app shell holds — mirroring
    ``GET /shops/by-domain/{shop_domain}/ingest/latest``. The integer PK is agent-internal.
    """
    shop = (
        await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    ).scalar_one_or_none()
    if shop is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shop not found.")

    # coffee is the only supported vertical today; build_query_panel is deterministic.
    panel = build_query_panel()
    panel_id = await upsert_panel(db, panel, shop.id)
    run = await create_agent_run(db, shop.id, panel_id)
    await db.commit()  # run row + panel exist before the job is enqueued

    await _enqueue(run.id)

    return ScanResponse(run_id=run.id, status=run.status)


@router.get(
    "/by-domain/{shop_domain}/report",
    response_model=ReportResponse,
    dependencies=[Depends(require_internal_api_key)],
)
async def get_report(
    shop_domain: str,
    db: DbSession,
    run_id: int | None = None,
) -> ReportResponse:
    """The persisted share-of-model rates for a run (the shop's latest run if unspecified).

    Keyed on ``shop_domain`` (see ``start_scan``). Resolves the shop, then the run.
    """
    shop = (
        await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    ).scalar_one_or_none()
    if shop is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shop not found.")

    if run_id is not None:
        run = (
            await db.execute(
                select(AgentRun).where(AgentRun.id == run_id, AgentRun.shop_id == shop.id)
            )
        ).scalar_one_or_none()
    else:
        run = (
            await db.execute(
                select(AgentRun)
                .where(AgentRun.shop_id == shop.id)
                .order_by(AgentRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No scan run found.")

    # Resolution is purely by run_id — NOT (shop_id, period). A running run has no rows yet.
    rows = (
        await db.execute(
            select(ShareOfModel)
            .where(ShareOfModel.run_id == run.id)
            .order_by(ShareOfModel.engine)
        )
    ).scalars().all()

    # coverage is derived from the run's panel (usable queries / panel query count), not persisted.
    panel = await db.get(QueryPanelRow, run.panel_id)
    panel_query_count = len(panel.queries_json) if panel else 0

    # period is a label; all of a run's rows share one. Fall back to the run's start date.
    period = rows[0].period if rows else run.started_at.date().isoformat()

    engines = [
        EngineReport(
            engine=row.engine,
            our_rate=row.our_rate,
            our_mentions=row.our_mentions,
            total_queries=row.total_queries,
            coverage=(
                row.total_queries / panel_query_count
                if panel_query_count and row.total_queries is not None
                else 0.0
            ),
            competitor_rates=row.competitor_rates_json,
        )
        for row in rows
    ]

    return ReportResponse(
        run_id=run.id,
        status=run.status,
        period=period,
        started_at=run.started_at,
        completed_at=run.completed_at,
        engines=engines,
    )
