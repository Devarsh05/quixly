"""POST /products/{product_id}/audit (PRD §9): run the deterministic audit for one product.

Internal-key guarded like every other agent route. Persists one ``audits`` row and returns the
gaps + severity + spec_coverage. Unknown product → 404.
"""

import httpx
import pytest
from sqlalchemy import select

from app.db import get_db
from app.main import app
from app.models import AgentRun, AgentRunStatus, Audit, Product, Shop, ShopStatus
from app.models import QueryPanel as QueryPanelRow
from tests.conftest import TEST_API_KEY

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
    shop = Shop(shop_domain="audit-route-test.myshopify.com", status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _product(db, shop_id: int, **overrides) -> Product:
    defaults = dict(
        shopify_product_id="gid://shopify/Product/1",
        title="Kenya AA",
        body=None,
        variants_json=[],
        gtin=None,
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


async def test_requires_the_internal_key(client, db, shop):
    product = await _product(db, shop.id)
    response = await client.post(f"/products/{product.id}/audit")
    assert response.status_code == 401


async def test_audit_persists_a_row_and_returns_gaps(client, db, shop):
    product = await _product(db, shop.id)

    response = await client.post(f"/products/{product.id}/audit", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["product_id"] == product.id
    assert body["run_id"] is None
    assert body["product_class"] == "coffee"
    assert body["severity"] == "high"  # thin coffee: no description + almost no spec attributes
    codes = {gap["code"] for gap in body["gaps"]}
    assert "missing_description" in codes
    assert "spec_missing" in codes

    row = (
        await db.execute(select(Audit).where(Audit.id == body["audit_id"]))
    ).scalar_one()
    assert row.product_id == product.id


async def test_run_id_in_body_scopes_the_audit(client, db, shop):
    product = await _product(db, shop.id)
    panel = QueryPanelRow(
        shop_id=shop.id, category="coffee", queries_json=[{"text": "q"}], fingerprint="fp"
    )
    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    run = AgentRun(shop_id=shop.id, panel_id=panel.id, status=AgentRunStatus.running)
    db.add(run)
    await db.commit()
    await db.refresh(run)

    response = await client.post(
        f"/products/{product.id}/audit", headers=HEADERS, json={"run_id": run.id}
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == run.id
    row = (
        await db.execute(select(Audit).where(Audit.id == response.json()["audit_id"]))
    ).scalar_one()
    assert row.run_id == run.id


async def test_unknown_product_404(client, db, shop):
    response = await client.post("/products/999999/audit", headers=HEADERS)
    assert response.status_code == 404
