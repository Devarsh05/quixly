"""Shop connection + ingest status.

Note on the route shape: PRD §9 lists this as ``POST /shops/{id}/connect``, which cannot
work — at connect time no internal shop id exists yet. The shop is keyed on its domain.

Note on the payload: it carries **no access token**. The agent stores no Shopify
credential; it fetches short-lived tokens from the app shell on demand. See
``app/services/token_provider.py``.
"""

from datetime import UTC, datetime
from typing import Annotated

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db import get_db
from app.models import IngestRun, IngestStatus, Shop, ShopStatus
from app.redis import acquire_ingest_lock
from app.settings import get_settings

router = APIRouter(prefix="/shops", tags=["shops"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


class ConnectRequest(BaseModel):
    shop_domain: str = Field(..., examples=["quixly-dev.myshopify.com"])


class ConnectResponse(BaseModel):
    shop_id: int
    run_id: int
    status: IngestStatus
    already_running: bool = False


class IngestRunResponse(BaseModel):
    run_id: int
    status: IngestStatus
    products_seen: int
    products_written: int
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None


async def _enqueue(shop_domain: str, run_id: int) -> None:
    pool: ArqRedis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job("ingest_catalog", shop_domain, run_id)
    finally:
        await pool.aclose()


@router.post(
    "/connect",
    response_model=ConnectResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_internal_api_key)],
)
async def connect_shop(
    payload: ConnectRequest,
    db: DbSession,
) -> ConnectResponse:
    """Register (or re-register) a shop and kick off catalog ingestion.

    Idempotent on ``shop_domain``: reinstalls and Shopify's OAuth retries must not create
    duplicate shops or a second concurrent ingest.
    """
    # Upsert the shop. A reinstall of a previously-uninstalled shop flips it back active.
    statement = (
        insert(Shop)
        .values(shop_domain=payload.shop_domain, status=ShopStatus.active)
        .on_conflict_do_update(
            index_elements=[Shop.shop_domain],
            set_={"status": ShopStatus.active, "updated_at": datetime.now(UTC)},
        )
        .returning(Shop.id)
    )
    shop_id = (await db.execute(statement)).scalar_one()
    await db.commit()

    run = IngestRun(shop_id=shop_id, status=IngestStatus.queued)
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # If an ingest is already in flight for this shop, hand back its run_id and throw
    # away the row we just made rather than enqueueing a second job.
    existing_run_id = await acquire_ingest_lock(payload.shop_domain, run.id)
    if existing_run_id is not None and existing_run_id != run.id:
        await db.delete(run)
        await db.commit()
        existing = await db.get(IngestRun, existing_run_id)
        return ConnectResponse(
            shop_id=shop_id,
            run_id=existing_run_id,
            status=existing.status if existing else IngestStatus.running,
            already_running=True,
        )

    await _enqueue(payload.shop_domain, run.id)

    return ConnectResponse(shop_id=shop_id, run_id=run.id, status=run.status)


def _to_response(run: IngestRun) -> IngestRunResponse:
    return IngestRunResponse(
        run_id=run.id,
        status=run.status,
        products_seen=run.products_seen,
        products_written=run.products_written,
        error=run.error,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.get(
    "/{shop_id}/ingest/{run_id}",
    response_model=IngestRunResponse,
    dependencies=[Depends(require_internal_api_key)],
)
async def get_ingest_run(
    shop_id: int,
    run_id: int,
    db: DbSession,
) -> IngestRunResponse:
    """Ingest progress, for the embedded app to poll after install."""
    run = (
        await db.execute(
            select(IngestRun).where(IngestRun.id == run_id, IngestRun.shop_id == shop_id)
        )
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingest run not found.")

    return _to_response(run)


@router.get(
    "/by-domain/{shop_domain}/ingest/latest",
    response_model=IngestRunResponse,
    dependencies=[Depends(require_internal_api_key)],
)
async def get_latest_ingest_run(
    shop_domain: str,
    db: DbSession,
) -> IngestRunResponse:
    """The shop's most recent ingest run.

    The embedded app reads this on load. It is read-only on purpose: having the page
    call /shops/connect instead would re-enqueue an ingest on every render.
    """
    run = (
        await db.execute(
            select(IngestRun)
            .join(Shop, Shop.id == IngestRun.shop_id)
            .where(Shop.shop_domain == shop_domain)
            .order_by(IngestRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No ingest run yet.")

    return _to_response(run)
