"""Arq job for Shopify's mandatory ``shop/redact`` compliance webhook.

Why a job at all: Shopify expects a 2xx from the webhook endpoint within seconds, and the erasure
spans two services and two schemas. The app shell verifies the HMAC, deletes its own
``shopify.Session`` rows, forwards the topic and answers 200; the dispatcher enqueues this and
returns 204. Nothing waits on a cross-service delete inside the request.

The deletion itself lives in ``services.purge`` so it is testable without arq. This wrapper owns
the two pieces of shop-scoped state that are NOT in Postgres: the Redis admin-token cache and the
per-shop ingest lock. Both are cleared through the existing helpers — the token cache via the
worker's shared ``TokenProvider``, never a locally built one, so the app shell stays the single
refresh authority.

**Redis is cleared only when rows actually went.** A skipped reinstall must keep its cached token
and its ingest lock: the shop is live, and dropping either would degrade a store we deliberately
chose not to touch.
"""

import logging

from app.db import SessionLocal
from app.redis import release_ingest_lock
from app.services.purge import PurgeOutcome, PurgeReport, purge_shop_data
from app.services.token_provider import TokenProvider

logger = logging.getLogger(__name__)


async def purge_shop(ctx: dict, shop_domain: str) -> PurgeReport:
    """Arq job: erase everything the agent stores for ``shop_domain``.

    Raises on failure so arq retries. The purge is idempotent, so a retry after a partial
    failure is safe — either the shop row is still there and the DELETE runs again, or it is
    gone and the run reports ``unknown_shop``.
    """
    # The worker's single provider (app/worker.py). A job that built its own would be a second
    # token path; the refresh authority must stay singular.
    token_provider: TokenProvider = ctx["token_provider"]

    async with SessionLocal() as session:
        try:
            report = await purge_shop_data(session, shop_domain)
        except Exception:
            await session.rollback()
            logger.exception("shop/redact purge failed for %s", shop_domain)
            raise

    if report.outcome is not PurgeOutcome.skipped_reinstalled:
        await token_provider.invalidate(shop_domain)
        await release_ingest_lock(shop_domain)

    return report
