"""Forwarded Shopify webhooks.

HMAC is verified in the app shell; by the time a request reaches the agent it is trusted
and guarded only by the internal shared secret. The app/uninstalled branch must flip the
shop to ``uninstalled`` and stay safe to replay — Shopify redelivers webhooks, and can
deliver app/uninstalled more than once.

The topic is parametrized over BOTH the form the app shell actually forwards
(``PRODUCTS_UPDATE`` / ``APP_UNINSTALLED`` — Shopify's ``authenticate.webhook`` returns the
``topicForStorage`` UPPER_SNAKE form) and the REST-header form (``products/update`` /
``app/uninstalled``). A test that only exercised the REST form passed green while the real
UPPER_SNAKE deliveries fell through to a 204 no-op — so every case asserts the DB row was
actually written, never just a 2xx.

Driven through httpx.ASGITransport rather than TestClient, for the same event-loop reason
as test_shops_connect.
"""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app.db import get_db
from app.main import app
from app.models import Product, Shop, ShopStatus
from tests.conftest import TEST_API_KEY

SHOP = "uninstall-test.myshopify.com"
HEADERS = {"X-Internal-Api-Key": TEST_API_KEY}

# Seeded rows start far in the past so "updated_at advanced" is a deterministic write check.
SEEDED_AT = datetime(2000, 1, 1, tzinfo=UTC)


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


def _envelope(topic: str = "APP_UNINSTALLED") -> dict:
    return {"topic": topic, "shop_domain": SHOP, "payload": {}}


async def _status(db) -> ShopStatus:
    return (
        await db.execute(select(Shop.status).where(Shop.shop_domain == SHOP))
    ).scalar_one()


async def test_requires_the_internal_key(client, shop):
    response = await client.post("/webhooks/shopify", json=_envelope())
    assert response.status_code == 401


# The shell forwards "APP_UNINSTALLED"; "app/uninstalled" is the REST-header form. Both must work.
@pytest.mark.parametrize("topic", ["APP_UNINSTALLED", "app/uninstalled"])
async def test_app_uninstalled_marks_the_shop_uninstalled(client, db, shop, topic):
    response = await client.post("/webhooks/shopify", json=_envelope(topic), headers=HEADERS)

    assert response.status_code == 204
    assert await _status(db) == ShopStatus.uninstalled


@pytest.mark.parametrize("topic", ["APP_UNINSTALLED", "app/uninstalled"])
async def test_app_uninstalled_is_idempotent(client, db, shop, topic):
    """Shopify can redeliver app/uninstalled; replaying it must stay safe."""
    first = await client.post("/webhooks/shopify", json=_envelope(topic), headers=HEADERS)
    second = await client.post("/webhooks/shopify", json=_envelope(topic), headers=HEADERS)

    assert first.status_code == 204
    assert second.status_code == 204
    assert await _status(db) == ShopStatus.uninstalled


# --- products/update: the shared visibility_state normalizer on the webhook path -------------

PRODUCT_ID = 555
PRODUCT_GID = f"gid://shopify/Product/{PRODUCT_ID}"


async def _seed_product(db, shop, *, visibility_state: str) -> None:
    db.add(
        Product(
            shop_id=shop.id,
            shopify_product_id=PRODUCT_GID,
            title="Old title",
            visibility_state=visibility_state,
            updated_at=SEEDED_AT,
        )
    )
    await db.commit()


async def _product(db):
    return (
        await db.execute(select(Product).where(Product.shopify_product_id == PRODUCT_GID))
    ).scalar_one()


def _update_envelope(status: str, topic: str = "PRODUCTS_UPDATE") -> dict:
    # The REST/webhook payload uses a numeric id, `body_html`, and lowercase status.
    return {
        "topic": topic,
        "shop_domain": SHOP,
        "payload": {
            "id": PRODUCT_ID,
            "title": "New title",
            "body_html": "<p>New</p>",
            "variants": [{"barcode": "0123456789012"}],
            "status": status,
        },
    }


# "PRODUCTS_UPDATE" is what the shell forwards; "products/update" is the REST-header form.
@pytest.mark.parametrize("topic", ["PRODUCTS_UPDATE", "products/update"])
async def test_products_update_normalizes_lowercase_status(client, db, shop, topic):
    """Webhook status (lowercase) is normalized to the canonical, incl. the new `unlisted`."""
    await _seed_product(db, shop, visibility_state="active")

    response = await client.post(
        "/webhooks/shopify", json=_update_envelope("unlisted", topic), headers=HEADERS
    )
    assert response.status_code == 204

    product = await _product(db)
    # The row was actually written — not a 204 no-op: title changed and updated_at advanced.
    assert product.title == "New title"
    assert product.updated_at > SEEDED_AT
    assert product.visibility_state == "unlisted"


@pytest.mark.parametrize("topic", ["PRODUCTS_UPDATE", "products/update"])
async def test_products_update_unknown_status_keeps_prior_value(client, db, shop, topic):
    """An unmapped status must NOT 500; keep the prior visibility_state but apply other fields."""
    await _seed_product(db, shop, visibility_state="active")

    response = await client.post(
        "/webhooks/shopify", json=_update_envelope("bogus", topic), headers=HEADERS
    )
    assert response.status_code == 204

    product = await _product(db)
    # Unmapped value ignored — previously-stored state survives.
    assert product.visibility_state == "active"
    # ...but the rest of the update still landed (row written, not a no-op).
    assert product.title == "New title"
    assert product.gtin == "0123456789012"
    assert product.updated_at > SEEDED_AT
