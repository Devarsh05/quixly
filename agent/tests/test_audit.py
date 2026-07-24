"""The audit node ``run_audit`` (Phase 3, step 1 — Gate G).

DB-backed (real Postgres ``db`` fixture, rolled back per test). The node loads a product, derives
its class from merchant fields (``classify_product``), scores it with the per-class rubric, and
persists one ``audits`` row carrying the class, gaps, BOTH nullable coverage numbers
(``spec_coverage`` = prose, ``structured_coverage`` = metafields), and severity.
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
        variants_json=[{"barcode": "0123456789012"}],
        gtin="0123456789012",
        metafields_json=None,
        visibility_state="active",
        product_type="Coffee",
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


async def test_audit_persists_both_coverage_numbers_for_a_rich_prose_coffee(db, shop):
    """RICH_BODY states all seven families in prose but carries no metafields, so the two numbers
    diverge — and BOTH are persisted. Storing only one would lose the addressable-set finding."""
    product = await _product(db, shop.id)

    outcome = await run_audit(db, product.id)

    row = (await db.execute(select(Audit).where(Audit.id == outcome.audit_id))).scalar_one()
    assert row.product_class == "coffee"
    assert row.spec_coverage == 1.0         # prose
    assert row.structured_coverage == 0.0   # nothing machine-readable
    assert row.severity == "medium"         # seven unstructured (auto-fixable) families
    assert {g["state"] for g in row.gaps_json} == {"unstructured"}
    assert outcome.structured_coverage == 0.0
    assert outcome.audited is True


async def test_audit_persists_no_gaps_for_a_fully_structured_coffee(db, shop):
    product = await _product(
        db, shop.id,
        metafields_json=[
            {"namespace": "custom", "key": key, "value": "x"}
            for key in ("roast_level", "origin", "process", "variety", "tasting_notes",
                        "altitude", "brew_method")
        ],
    )

    outcome = await run_audit(db, product.id)

    row = (await db.execute(select(Audit).where(Audit.id == outcome.audit_id))).scalar_one()
    assert row.structured_coverage == 1.0
    assert row.severity == "none"
    assert row.gaps_json == []


async def test_audit_equipment_missing_gtin_is_medium_with_null_coverage(db, shop):
    product = await _product(
        db, shop.id, title="Gooseneck Kettle", body="A kettle.",
        variants_json=[{"barcode": None}], gtin=None, product_type="Brewing Gear",
    )

    outcome = await run_audit(db, product.id)

    assert outcome.product_class == "equipment"
    assert outcome.severity == "medium"
    # Equipment has no grounded spec vocabulary — BOTH numbers are NULL, never a misleading 0.0.
    assert outcome.spec_coverage is None
    assert outcome.structured_coverage is None
    assert {g.code for g in outcome.gaps} == {"missing_gtin"}


async def test_audit_excludes_a_draft_product(db, shop):
    product = await _product(db, shop.id, visibility_state="draft")

    outcome = await run_audit(db, product.id)

    assert outcome.audited is False
    assert outcome.excluded_reason == "not_visible"
    row = (await db.execute(select(Audit).where(Audit.id == outcome.audit_id))).scalar_one()
    assert row.severity == "not_audited"
    assert row.gaps_json == []
    assert row.spec_coverage is None
    assert row.structured_coverage is None


async def test_audit_stamps_run_id_when_scoped(db, shop):
    product = await _product(db, shop.id)
    run = await _run(db, shop.id)

    outcome = await run_audit(db, product.id, run_id=run.id)

    row = (await db.execute(select(Audit).where(Audit.id == outcome.audit_id))).scalar_one()
    assert row.run_id == run.id


async def test_audit_appends_rather_than_overwrites(db, shop):
    product = await _product(db, shop.id)

    await run_audit(db, product.id)
    await run_audit(db, product.id)

    count = (
        await db.execute(
            select(func.count()).select_from(Audit).where(Audit.product_id == product.id)
        )
    ).scalar_one()
    assert count == 2


async def test_audit_raises_for_unknown_product(db):
    with pytest.raises(ValueError):
        await run_audit(db, 999999)
