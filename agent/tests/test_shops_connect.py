"""POST /shops/connect must be safe to call twice.

Shopify retries OAuth callbacks, and merchants reinstall. Neither may produce a duplicate
shop row or a second concurrent ingest job.

Driven through httpx.ASGITransport rather than TestClient: TestClient runs the app on its
own event loop in a worker thread, which an asyncpg connection created on the test's loop
cannot be shared with.
"""

import httpx
import pytest
from sqlalchemy import func, select

from app.api import shops as shops_api
from app.db import get_db
from app.main import app
from app.models import IngestRun, Shop, ShopStatus
from tests.conftest import TEST_API_KEY

SHOP = "idempotency-test.myshopify.com"
HEADERS = {"X-Internal-Api-Key": TEST_API_KEY}


@pytest.fixture
def enqueued(monkeypatch) -> list[tuple[str, int]]:
    """Capture enqueued jobs instead of hitting a real Arq queue."""
    calls: list[tuple[str, int]] = []

    async def fake_enqueue(shop_domain: str, run_id: int) -> None:
        calls.append((shop_domain, run_id))

    monkeypatch.setattr(shops_api, "_enqueue", fake_enqueue)
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


async def test_requires_the_internal_key(client):
    response = await client.post("/shops/connect", json={"shop_domain": SHOP})
    assert response.status_code == 401


async def test_connect_returns_202_with_a_run_id(client, enqueued):
    response = await client.post("/shops/connect", json={"shop_domain": SHOP}, headers=HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] > 0
    assert body["already_running"] is False
    assert enqueued == [(SHOP, body["run_id"])]


async def test_calling_twice_does_not_duplicate_the_shop_or_double_enqueue(client, db, enqueued):
    first = (
        await client.post("/shops/connect", json={"shop_domain": SHOP}, headers=HEADERS)
    ).json()
    second = (
        await client.post("/shops/connect", json={"shop_domain": SHOP}, headers=HEADERS)
    ).json()

    # The lock is still held by the first run, so the second call rides along with it.
    assert second["run_id"] == first["run_id"]
    assert second["already_running"] is True
    assert second["shop_id"] == first["shop_id"]

    # Exactly one job queued, exactly one shop.
    assert enqueued == [(SHOP, first["run_id"])]

    shop_count = await db.scalar(
        select(func.count()).select_from(Shop).where(Shop.shop_domain == SHOP)
    )
    assert shop_count == 1

    run_count = await db.scalar(
        select(func.count()).select_from(IngestRun).where(IngestRun.shop_id == first["shop_id"])
    )
    assert run_count == 1, "the abandoned second run row should have been cleaned up"


async def test_reinstall_reactivates_an_uninstalled_shop(client, db, enqueued):
    first = (
        await client.post("/shops/connect", json={"shop_domain": SHOP}, headers=HEADERS)
    ).json()

    shop = await db.get(Shop, first["shop_id"])
    shop.status = ShopStatus.uninstalled
    await db.commit()

    await client.post("/shops/connect", json={"shop_domain": SHOP}, headers=HEADERS)

    await db.refresh(shop)
    assert shop.status == ShopStatus.active
