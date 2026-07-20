"""GET /shops/{shop_id}/report: read persisted share_of_model rates, resolved by run_id.

Rows are seeded directly (no task run). Resolution is purely by run_id since step 6a, so a
running run with no rows reports status without a 500, and two same-day runs stay distinct.
"""

import httpx
import pytest

from app.db import get_db
from app.main import app
from app.models import AgentRun, AgentRunStatus, ShareOfModel, Shop, ShopStatus
from app.models import QueryPanel as QueryPanelRow
from tests.conftest import TEST_API_KEY

SHOP = "report-route-test.myshopify.com"
HEADERS = {"X-Internal-Api-Key": TEST_API_KEY}


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


async def _panel(
    db, shop_id: int, *, query_count: int = 4, fingerprint: str = "fp1"
) -> QueryPanelRow:
    panel = QueryPanelRow(
        shop_id=shop_id,
        category="coffee",
        queries_json=[{"text": f"q{i}"} for i in range(query_count)],
        fingerprint=fingerprint,
    )
    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    return panel


async def _run(db, shop_id: int, panel_id: int, *, status=AgentRunStatus.completed) -> AgentRun:
    run = AgentRun(shop_id=shop_id, panel_id=panel_id, status=status)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _som(
    db, run_id: int, shop_id: int, *, engine: str = "perplexity", period: str = "2026-07-20",
    our_rate: float | None = 0.5, our_mentions: int | None = 2, total_queries: int | None = 4,
    competitors: dict | None = None,
) -> None:
    db.add(
        ShareOfModel(
            run_id=run_id, shop_id=shop_id, engine=engine, period=period,
            our_rate=our_rate, our_mentions=our_mentions, total_queries=total_queries,
            competitor_rates_json=(
                competitors
                if competitors is not None
                else {"Blue Bottle": {"mention_rate": 0.75, "mentions": 3}}
            ),
        )
    )
    await db.commit()


async def test_requires_the_internal_key(client, shop):
    response = await client.get(f"/shops/{shop.id}/report")
    assert response.status_code == 401


async def test_completed_run_returns_persisted_rates(client, db, shop):
    panel = await _panel(db, shop.id)
    run = await _run(db, shop.id, panel.id)
    await _som(db, run.id, shop.id)

    response = await client.get(f"/shops/{shop.id}/report?run_id={run.id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run.id
    assert body["status"] == "completed"
    assert len(body["engines"]) == 1
    engine = body["engines"][0]
    assert engine["engine"] == "perplexity"
    assert engine["our_rate"] == 0.5
    assert engine["total_queries"] == 4
    assert engine["coverage"] == 1.0  # 4 usable / 4 panel queries
    assert engine["competitor_rates"]["Blue Bottle"]["mention_rate"] == 0.75


async def test_running_run_returns_status_without_500(client, db, shop):
    panel = await _panel(db, shop.id)
    run = await _run(db, shop.id, panel.id, status=AgentRunStatus.running)
    # No share_of_model rows written yet — a running run has none by construction.

    response = await client.get(f"/shops/{shop.id}/report?run_id={run.id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["engines"] == []


async def test_null_our_rate_serializes_as_null(client, db, shop):
    panel = await _panel(db, shop.id, query_count=2)
    run = await _run(db, shop.id, panel.id)
    await _som(db, run.id, shop.id, our_rate=None, our_mentions=0, total_queries=0, competitors={})

    response = await client.get(f"/shops/{shop.id}/report?run_id={run.id}", headers=HEADERS)
    body = response.json()
    engine = body["engines"][0]
    assert engine["our_rate"] is None  # JSON null — "no data", never 0.0
    assert engine["coverage"] == 0.0


async def test_latest_run_is_the_default(client, db, shop):
    panel = await _panel(db, shop.id)
    older = await _run(db, shop.id, panel.id)
    await _som(db, older.id, shop.id, our_rate=0.1)
    newer = await _run(db, shop.id, panel.id)
    await _som(db, newer.id, shop.id, our_rate=0.9)

    response = await client.get(f"/shops/{shop.id}/report", headers=HEADERS)
    body = response.json()
    assert body["run_id"] == newer.id
    assert body["engines"][0]["our_rate"] == 0.9


async def test_two_same_period_runs_resolve_distinctly(client, db, shop):
    # The end-to-end payoff of 6a + this step: two runs of the same shop on the SAME period,
    # different rates, each addressable by run_id through the read path.
    panel = await _panel(db, shop.id)
    run_a = await _run(db, shop.id, panel.id)
    run_b = await _run(db, shop.id, panel.id)
    await _som(db, run_a.id, shop.id, period="2026-07-20", our_rate=1.0)
    await _som(db, run_b.id, shop.id, period="2026-07-20", our_rate=0.0)

    body_a = (
        await client.get(f"/shops/{shop.id}/report?run_id={run_a.id}", headers=HEADERS)
    ).json()
    body_b = (
        await client.get(f"/shops/{shop.id}/report?run_id={run_b.id}", headers=HEADERS)
    ).json()
    assert body_a["engines"][0]["our_rate"] == 1.0
    assert body_b["engines"][0]["our_rate"] == 0.0


async def test_unknown_shop_or_run_404(client, db, shop):
    # Unknown shop (no runs at all).
    assert (await client.get("/shops/999999/report", headers=HEADERS)).status_code == 404
    # Known shop, unknown run_id.
    panel = await _panel(db, shop.id)
    await _run(db, shop.id, panel.id)
    response = await client.get(f"/shops/{shop.id}/report?run_id=888888", headers=HEADERS)
    assert response.status_code == 404
