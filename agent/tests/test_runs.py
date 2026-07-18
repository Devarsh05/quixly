"""Agent-run lifecycle service.

Uses the real Postgres-backed ``db`` fixture (transaction rolled back per test). The helpers flush
+ refresh but do not commit; the caller (a route, later) owns the transaction boundary.
"""

import pytest

from app.models import AgentRunStatus, Shop, ShopStatus
from app.models import QueryPanel as QueryPanelRow
from app.services.runs import complete_agent_run, create_agent_run

SHOP = "runs-test.myshopify.com"


@pytest.fixture
async def panel(db):
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    panel = QueryPanelRow(
        shop_id=shop.id, category="coffee", queries_json=[{"text": "q0"}], fingerprint="fp1"
    )
    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    return panel


async def test_create_agent_run_writes_running_row(db, panel):
    run = await create_agent_run(db, panel.shop_id, panel.id)

    assert run.id is not None
    assert run.shop_id == panel.shop_id
    assert run.panel_id == panel.id
    assert run.status == AgentRunStatus.running
    assert run.started_at is not None
    assert run.completed_at is None


async def test_complete_agent_run_sets_completed_at_and_status(db, panel):
    run = await create_agent_run(db, panel.shop_id, panel.id)

    completed = await complete_agent_run(db, run.id)

    assert completed.id == run.id
    assert completed.status == AgentRunStatus.completed
    assert completed.completed_at is not None


async def test_complete_agent_run_failed_status(db, panel):
    run = await create_agent_run(db, panel.shop_id, panel.id)

    completed = await complete_agent_run(db, run.id, status=AgentRunStatus.failed)

    assert completed.status == AgentRunStatus.failed
    assert completed.completed_at is not None


async def test_complete_unknown_run_raises(db, panel):
    with pytest.raises(ValueError, match="not found"):
        await complete_agent_run(db, 999999)
