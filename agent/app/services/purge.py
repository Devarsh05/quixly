"""Shop data erasure for Shopify's mandatory ``shop/redact`` compliance webhook.

This is the ONLY code in the agent that deletes a merchant's data. It is deliberately not
reachable from any merchant-facing route: the single caller is ``jobs.purge_shop``, which is
enqueued by the ``SHOP_REDACT`` branch of the forwarded-webhook dispatcher.

**One statement erases the shop, and that is a property of the schema, not of this module.**
Every shop-scoped table hangs off ``shops`` by ``ON DELETE CASCADE`` in the real DDL::

    shops -> products -> audits
          |           -> fixes
          -> ingest_runs
          -> query_panels -> engine_runs      (engine_runs.panel_id is NOT NULL)
          -> agent_runs   -> share_of_model
          |               -> verifications
          -> share_of_model
          -> verifications

so ``DELETE FROM shops`` reaches all nine and Postgres resolves the order itself — there is no
manual delete sequence to get wrong. The ``SET NULL`` FKs (``engine_runs.run_id``,
``audits.run_id``, ``fixes.run_id``, ``verifications.baseline_run_id`` / ``panel_id``,
``fixes.reverts_fix_id``) leave no orphans, because every row they point at is also reached by a
CASCADE path of its own. **That is an argument, not evidence** — ``tests/test_purge_shop.py``
asserts each of the nine tables is empty afterwards, and that a second shop's rows all survive.
If a future table is added without a CASCADE path to ``shops``, that test is what fails.

The ``shops`` row itself goes too. For a *shop* redact the store domain is precisely the
identifier being erased, and a later reinstall re-creates the row through ``connect_shop``'s
upsert.

**The reinstall guard.** ``shop/redact`` arrives 48 hours after uninstall, and Shopify does not
document it as cancelled if the merchant reinstalls in the meantime. A shop found ``active`` is a
live store: honouring a stale request would wipe a paying merchant's catalog, audits and — with no
way to recover them — their verification baselines and uplift history. So an ``active`` shop is
skipped and the skip is logged as a warning, which is the signal that a genuine erasure request
went unhonoured and needs a human.

Deliberately Shopify-free: no Admin API call, no token. The shop is being erased, so there is
nothing to read from it and no credential worth minting.
"""

import enum
import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Shop, ShopStatus

logger = logging.getLogger(__name__)


class PurgeOutcome(enum.StrEnum):
    """What the purge actually did. Never inferred from a row count by the caller."""

    purged = "purged"
    # The shop reinstalled inside the 48h window — deliberately untouched. See module docstring.
    skipped_reinstalled = "skipped_reinstalled"
    # Nothing stored for this domain. A normal outcome, not an error: Shopify redelivers
    # shop/redact, and the second delivery finds the work already done.
    unknown_shop = "unknown_shop"


@dataclass(frozen=True)
class PurgeReport:
    shop_domain: str
    outcome: PurgeOutcome
    shop_id: int | None = None


async def purge_shop_data(session: AsyncSession, shop_domain: str) -> PurgeReport:
    """Erase every ``public``-schema row belonging to ``shop_domain``.

    Idempotent: a redelivered ``shop/redact`` finds no shop and returns ``unknown_shop``.
    Does NOT touch the ``shopify`` schema — Prisma owns ``Session``, and the app shell deletes
    those rows in its own handler.
    """
    shop = (
        await session.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    ).scalar_one_or_none()

    if shop is None:
        logger.info("shop/redact for %s: nothing stored, already erased", shop_domain)
        return PurgeReport(shop_domain=shop_domain, outcome=PurgeOutcome.unknown_shop)

    if shop.status == ShopStatus.active:
        # Loud on purpose: a real erasure request was not honoured, and only a human can tell
        # a genuine reinstall from a status that never got flipped.
        logger.warning(
            "shop/redact for %s SKIPPED: the shop is active again (reinstalled inside the 48h "
            "window). No data was deleted; review manually if the erasure was genuine.",
            shop_domain,
        )
        return PurgeReport(
            shop_domain=shop_domain,
            outcome=PurgeOutcome.skipped_reinstalled,
            shop_id=shop.id,
        )

    shop_id = shop.id
    # Core DELETE, not ``session.delete(shop)``: the ORM cascade would load every child row into
    # memory to delete it one statement at a time. The FK cascade is the database's job.
    await session.execute(delete(Shop).where(Shop.id == shop_id))
    await session.commit()

    logger.info("shop/redact for %s: purged shop %s and all cascaded rows", shop_domain, shop_id)
    return PurgeReport(shop_domain=shop_domain, outcome=PurgeOutcome.purged, shop_id=shop_id)
