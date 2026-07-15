"""Catalog ingestion — the first real Arq job.

Design constraints, all of which have bitten someone before:

* The token is fetched inside the job (via ``ShopifyAdminClient`` → ``TokenProvider``),
  never captured at enqueue time. A queued job may not start for minutes and a large
  catalog can outlive a 60-minute token.
* Products are committed **per page**, not once at the end. A run that dies at SKU 1,900
  leaves 1,900 rows, ``status = failed``, and a resumable cursor — not an empty table.
* Writes upsert on ``(shop_id, shopify_product_id)``, so re-running (or retrying after a
  401) never duplicates the catalog.
* A shop whose refresh chain has lapsed is marked ``reauth_required`` and does not take
  the rest of a batch down with it.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db import SessionLocal
from app.models import IngestRun, IngestStatus, Product, Shop, ShopStatus
from app.redis import release_ingest_lock
from app.services.catalog import extract_gtin
from app.services.shopify_admin import ShopifyAdminClient
from app.services.token_provider import TokenProvider, TokenUnavailableError

logger = logging.getLogger(__name__)

# Shopify's GraphQL ProductStatus enum is uppercase (ACTIVE/DRAFT/ARCHIVED); the rest of
# the system stores a lowercase canonical (matching the webhook write path and the
# shop_status enum). Map explicitly so an unrecognized value fails loudly rather than
# leaking an un-normalized value through or silently nulling it.
_STATUS = {"ACTIVE": "active", "DRAFT": "draft", "ARCHIVED": "archived"}


def _visibility_state(node: dict[str, Any]) -> str:
    raw = node.get("status")
    try:
        return _STATUS[raw]
    except KeyError:
        raise ValueError(
            f"Unmapped Shopify product status {raw!r} for {node.get('id')}"
        ) from None


async def _write_page(session, shop_id: int, nodes: list[dict[str, Any]]) -> int:
    """Upsert one page of products. Returns the number of rows written."""
    if not nodes:
        return 0

    rows = []
    for node in nodes:
        variants = (node.get("variants") or {}).get("nodes") or []
        metafields = (node.get("metafields") or {}).get("nodes") or []
        rows.append(
            {
                "shop_id": shop_id,
                "shopify_product_id": node["id"],
                "title": node.get("title"),
                "body": node.get("descriptionHtml"),
                "variants_json": variants,
                "gtin": extract_gtin(variants),
                "metafields_json": metafields,
                "visibility_state": _visibility_state(node),
                "updated_at": datetime.now(UTC),
            }
        )

    statement = insert(Product).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[Product.shop_id, Product.shopify_product_id],
        set_={
            "title": statement.excluded.title,
            "body": statement.excluded.body,
            "variants_json": statement.excluded.variants_json,
            "gtin": statement.excluded.gtin,
            "metafields_json": statement.excluded.metafields_json,
            "visibility_state": statement.excluded.visibility_state,
            "updated_at": statement.excluded.updated_at,
        },
    )
    await session.execute(statement)
    return len(rows)


async def ingest_catalog(ctx: dict, shop_domain: str, run_id: int) -> None:
    """Arq job: page the shop's catalog into ``products``."""
    token_provider: TokenProvider = ctx["token_provider"]

    async with SessionLocal() as session:
        run = await session.get(IngestRun, run_id)
        shop = (
            await session.execute(select(Shop).where(Shop.shop_domain == shop_domain))
        ).scalar_one()

        run.status = IngestStatus.running
        run.started_at = datetime.now(UTC)
        await session.commit()

        client = ShopifyAdminClient(shop_domain, token_provider)
        cursor = run.cursor  # resume where a previous attempt died, if it did

        try:
            async for nodes, end_cursor in client.iter_products(cursor):
                written = await _write_page(session, shop.id, nodes)

                run.products_seen += len(nodes)
                run.products_written += written
                run.cursor = end_cursor
                # Commit the page and its cursor together, so progress is never lost.
                await session.commit()

            run.status = IngestStatus.complete
            run.completed_at = datetime.now(UTC)
            await session.commit()
            logger.info(
                "Ingest complete for %s: %d products", shop_domain, run.products_written
            )

        except TokenUnavailableError as exc:
            # The refresh chain is dead (uninstalled, or 90+ days idle). This shop needs
            # re-auth; it is not a transient failure and must not be retried blindly.
            await session.rollback()
            run.status = IngestStatus.failed
            run.error = str(exc)
            run.completed_at = datetime.now(UTC)
            shop.status = ShopStatus.reauth_required
            await session.commit()
            logger.warning("Ingest for %s needs re-auth: %s", shop_domain, exc)

        except Exception as exc:
            # Everything else, including TokenFetchError (app shell unreachable), is
            # treated as transient: the shop is NOT flagged for re-auth, the rows and
            # cursor already committed stay intact, and the raise lets Arq retry from
            # where this attempt stopped.
            await session.rollback()
            run.status = IngestStatus.failed
            run.error = str(exc)
            run.completed_at = datetime.now(UTC)
            await session.commit()
            logger.exception("Ingest failed for %s", shop_domain)
            raise

        finally:
            # Always free the shop for a subsequent ingest, including on failure — the
            # TTL is only a backstop for a hard worker crash.
            await release_ingest_lock(shop_domain)
