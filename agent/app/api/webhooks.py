"""Shopify webhooks, forwarded from the app shell.

HMAC verification happens in the app shell (``authenticate.webhook``). By the time a
request reaches here it has already been proven to come from Shopify; this endpoint is
protected by the internal shared secret like every other internal route.
"""

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db import get_db
from app.models import Product, Shop, ShopStatus
from app.services.catalog import extract_gtin, normalize_visibility_state
from app.services.token_provider import TokenProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


class WebhookEnvelope(BaseModel):
    topic: str
    shop_domain: str
    payload: dict[str, Any] = {}


def _canonical_topic(topic: str) -> str:
    """Canonicalise a Shopify webhook topic to the library's stored form.

    ``authenticate.webhook()`` in the app shell returns the topic already run through
    Shopify's ``topicForStorage`` (``UPPER_SNAKE`` — e.g. ``PRODUCTS_UPDATE``), not the
    ``products/update`` REST-header form. Normalising here makes dispatch match what the
    shell actually forwards, while staying tolerant of the REST form (direct internal calls,
    tests, redeliveries).
    """
    return topic.upper().replace("/", "_").replace(".", "_")


@router.post(
    "/shopify",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_internal_api_key)],
)
async def handle_shopify_webhook(
    envelope: WebhookEnvelope,
    db: DbSession,
) -> None:
    """Dispatch a forwarded Shopify webhook."""
    topic = _canonical_topic(envelope.topic)
    if topic == "APP_UNINSTALLED":
        await _handle_uninstalled(db, envelope.shop_domain)
    elif topic == "PRODUCTS_UPDATE":
        await _handle_product_update(db, envelope.shop_domain, envelope.payload)
    else:
        logger.info("Ignoring unhandled webhook topic %s", envelope.topic)


async def _handle_uninstalled(db: AsyncSession, shop_domain: str) -> None:
    await db.execute(
        update(Shop).where(Shop.shop_domain == shop_domain).values(status=ShopStatus.uninstalled)
    )
    await db.commit()
    # The cached admin token is dead the moment the app is uninstalled — drop it rather
    # than letting a job pick it up and 401.
    await TokenProvider().invalidate(shop_domain)
    logger.info("Shop %s uninstalled", shop_domain)


async def _handle_product_update(
    db: AsyncSession, shop_domain: str, payload: dict[str, Any]
) -> None:
    """Refresh one product row from the webhook payload.

    The REST-shaped webhook payload uses a numeric id and `body_html`, unlike the GraphQL
    ingest path — normalise to the same GID form so the row stays consistent either way.
    """
    shop = (
        await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    ).scalar_one_or_none()
    if shop is None:
        logger.warning("products/update for unknown shop %s", shop_domain)
        return

    product_gid = f"gid://shopify/Product/{payload['id']}"
    variants = payload.get("variants") or []

    values: dict[str, Any] = {
        "title": payload.get("title"),
        "body": payload.get("body_html"),
        "variants_json": variants,
        "gtin": extract_gtin(variants),
        "updated_at": datetime.now(UTC),
    }
    # A raising webhook becomes a 500 back to the app shell -> Shopify retry storm and a lost
    # update. On an unmapped status, keep the previously-stored visibility_state (omit it from
    # the UPDATE) and apply the rest of the fields anyway. Ingest, by contrast, raises.
    try:
        values["visibility_state"] = normalize_visibility_state(payload.get("status"))
    except ValueError:
        logger.warning(
            "products/update for %s carried unmapped status %r; leaving visibility_state unchanged",
            product_gid,
            payload.get("status"),
        )

    result = await db.execute(
        update(Product)
        .where(Product.shop_id == shop.id, Product.shopify_product_id == product_gid)
        .values(**values)
    )
    await db.commit()

    if result.rowcount == 0:
        logger.info("products/update for unseen product %s — will land on next ingest", product_gid)
