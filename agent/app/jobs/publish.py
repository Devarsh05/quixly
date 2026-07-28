"""Publish orchestration — the job that writes approved fixes to a live merchant store.

Mirrors ``jobs.fix``: the caller creates and COMMITS the agent_run, then this job drives it to a
terminal status. Unlike that job, this one **does** talk to Shopify, so it takes the worker's
shared ``TokenProvider`` off ``ctx`` — the app shell stays the single refresh authority and the
agent stores no token.

The client is built at task START, never captured at enqueue time: a queued job can outlive the
60-minute token it would otherwise have been handed.

**Failure classification is the same as ingest's, and it is load-bearing.**
``TokenUnavailableError`` is PERMANENT (no session, or a dead refresh chain) — it flags the shop
``reauth_required`` and must not be retried blindly. Everything else, including
``TokenFetchError`` (app shell restarting), is transient: the shop is NOT flagged, the fixes
already committed keep their terminal states, and the raise lets Arq retry.
``PublishAborted`` is neither — it means our own state is broken (the approval gate was bypassed,
supersede failed, or the shop is not cleared for publishing) and nothing was written.
"""

import logging

from app.db import SessionLocal
from app.graph.publisher import PublishAborted, PublishReport, run_publisher
from app.models import AgentRun, AgentRunStatus, Shop, ShopStatus
from app.services.runs import complete_agent_run
from app.services.shopify_admin import ShopifyAdminClient
from app.services.token_provider import TokenProvider, TokenUnavailableError
from app.settings import get_settings

logger = logging.getLogger(__name__)


async def run_publish_task(ctx: dict, run_id: int) -> PublishReport:
    """Arq job: publish every approved fix for the run's shop, driving the run to terminal."""
    # The worker's single provider (app/worker.py). A job that built its own would be a second
    # token path; the refresh authority must stay singular.
    token_provider: TokenProvider = ctx["token_provider"]

    async with SessionLocal() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise ValueError(f"agent_run {run_id} not found")

        shop = await session.get(Shop, run.shop_id)
        if shop is None:
            raise ValueError(f"shop {run.shop_id} not found for agent_run {run_id}")

        # Held as a plain int: a rollback in the handlers below EXPIRES every ORM instance, and
        # reading an expired attribute there would emit synchronous IO outside the async context.
        shop_id = shop.id

        client = ShopifyAdminClient(shop.shop_domain, token_provider)

        try:
            report = await run_publisher(
                session,
                shop,
                client,
                run_id=run_id,
                allowed_shops=get_settings().publish_allowed_shops,
            )
            await complete_agent_run(session, run_id, AgentRunStatus.completed)
            await session.commit()  # load-bearing: persists status=completed
            return report

        except TokenUnavailableError:
            # PERMANENT. The shop must re-auth; retrying would never succeed. Terminal states
            # already committed for earlier fixes stay intact.
            await session.rollback()
            shop = await session.get(Shop, shop_id)
            if shop is not None:
                shop.status = ShopStatus.reauth_required
            await complete_agent_run(session, run_id, AgentRunStatus.failed)
            await session.commit()
            logger.warning("Publish run %s needs re-auth", run_id)
            raise

        except PublishAborted:
            # Our own state is broken and NOTHING was written. Not retryable — the run is marked
            # failed and the cause is surfaced, never swallowed.
            await session.rollback()
            await complete_agent_run(session, run_id, AgentRunStatus.failed)
            await session.commit()
            logger.exception("Publish run %s aborted before writing anything", run_id)
            raise

        except Exception:
            await session.rollback()
            await complete_agent_run(session, run_id, AgentRunStatus.failed)
            await session.commit()  # load-bearing: persists status=failed
            logger.exception("Publish run %s failed", run_id)
            raise
