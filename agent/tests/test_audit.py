"""The audit node ``run_audit`` (Phase 3, step 1 — Gate G).

DB-backed (real Postgres ``db`` fixture, rolled back per test). The node loads a product, scores
it with the deterministic rubric (``services.audit_rubric``), and persists one ``audits`` row.
No LLM, no external calls, so there is no live test.
"""

import pytest
from sqlalchemy import func, select

from app.graph.audit import run_audit
from app.models import AgentRun, AgentRunStatus, Audit, Product, Shop, ShopStatus
from app.models import QueryPanel as QueryPanelRow

RICH_BODY = (
    "Single-origin washed Arabica from Ethiopia. Altitude 2,000 masl. Varietal: Heirloom. "
    "Process: washed. Roast level: light. Tasting notes: bergamot, jasmine. Great as pour over "
    "or espresso."
)


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain="audit-test.myshopify.com", status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _product(db, shop_id: int, **overrides) -> Product:
    defaults = dict(
        shopify_product_id="gid://shopify/Product/1",
        title="Ethiopia Yirgacheffe",
        body=RICH_BODY,
        variants_json=[],
        gtin="0123456789012",
        metafields_json=[{"namespace": "custom", "key": "roast", "value": "light", "type": "x"}],
        visibility_state="active",
    )
    defaults.update(overrides)
    product = Product(shop_id=shop_id, **defaults)
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


async def _run(db, shop_id: int) -> AgentRun:
    panel = QueryPanelRow(
        shop_id=shop_id, category="coffee", queries_json=[{"text": "q"}], fingerprint="fp"
    )
    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    run = AgentRun(shop_id=shop_id, panel_id=panel.id, status=AgentRunStatus.running)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def test_run_audit_persists_a_row_for_a_clean_product(db, shop):
    product = await _product(db, shop.id)

    outcome = await run_audit(db, product.id)

    row = (
        await db.execute(select(Audit).where(Audit.id == outcome.audit_id))
    ).scalar_one()
    assert row.product_id == product.id
    assert row.run_id is None
    assert row.severity == "none"
    assert row.spec_coverage == 1.0
    assert row.gaps_json == []


async def test_run_audit_flags_an_empty_product_high(db, shop):
    product = await _product(
        db, shop.id, body=None, gtin=None, metafields_json=None, visibility_state="draft"
    )

    outcome = await run_audit(db, product.id)

    assert outcome.severity == "high"
    codes = {gap.code for gap in outcome.gaps}
    expected = {"missing_description", "missing_gtin", "missing_metafields", "not_discoverable"}
    assert expected <= codes


async def test_run_audit_stamps_run_id_when_scoped(db, shop):
    product = await _product(db, shop.id)
    run = await _run(db, shop.id)

    outcome = await run_audit(db, product.id, run_id=run.id)

    row = (await db.execute(select(Audit).where(Audit.id == outcome.audit_id))).scalar_one()
    assert row.run_id == run.id


async def test_run_audit_appends_rather_than_overwrites(db, shop):
    product = await _product(db, shop.id)

    await run_audit(db, product.id)
    await run_audit(db, product.id)

    count = (
        await db.execute(
            select(func.count()).select_from(Audit).where(Audit.product_id == product.id)
        )
    ).scalar_one()
    assert count == 2


async def test_run_audit_raises_for_unknown_product(db):
    with pytest.raises(ValueError):
        await run_audit(db, 999999)
