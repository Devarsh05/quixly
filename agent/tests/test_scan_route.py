"""POST /shops/by-domain/{shop_domain}/scan: enqueue a scan, committing a running run + panel.

Driven through httpx.ASGITransport (see test_shops_connect for why, not TestClient). The Arq
enqueue is captured, not executed — no real queue and no task run here.
"""

import httpx
import pytest
from sqlalchemy import func, select

from app.api import scan as scan_api
from app.db import get_db
from app.main import app
from app.models import AgentRun, AgentRunStatus, Shop, ShopStatus
from app.models import QueryPanel as QueryPanelRow
from tests.conftest import TEST_API_KEY

SHOP = "scan-route-test.myshopify.com"
HEADERS = {"X-Internal-Api-Key": TEST_API_KEY}


@pytest.fixture
def enqueued(monkeypatch) -> list[int]:
    """Capture enqueued run_ids instead of hitting a real Arq queue."""
    calls: list[int] = []

    async def fake_enqueue(run_id: int) -> None:
        calls.append(run_id)

    monkeypatch.setattr(scan_api, "_enqueue", fake_enqueue)
    return calls


@pytest.fixture
async def client(db):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
    app.dependency_overrides.clear()


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def test_requires_the_internal_key(client, shop):
    response = await client.post(f"/shops/by-domain/{SHOP}/scan")
    assert response.status_code == 401


async def test_scan_returns_202_and_commits_running_run_and_panel(client, db, shop, enqueued):
    response = await client.post(f"/shops/by-domain/{SHOP}/scan", headers=HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] > 0
    assert body["status"] == "running"

    # The job was enqueued exactly once, with the new run_id.
    assert enqueued == [body["run_id"]]

    # A committed running run exists...
    run = await db.get(AgentRun, body["run_id"])
    assert run is not None
    assert run.status == AgentRunStatus.running
    assert run.shop_id == shop.id

    # ...and its panel row was committed before enqueue (exactly one for the shop).
    panel = await db.get(QueryPanelRow, run.panel_id)
    assert panel is not None
    assert panel.shop_id == shop.id
    panel_count = await db.scalar(
        select(func.count()).select_from(QueryPanelRow).where(QueryPanelRow.shop_id == shop.id)
    )
    assert panel_count == 1


async def test_scan_unknown_shop_404_and_enqueues_nothing(client, enqueued):
    response = await client.post("/shops/by-domain/nope.myshopify.com/scan", headers=HEADERS)
    assert response.status_code == 404
    assert enqueued == []
