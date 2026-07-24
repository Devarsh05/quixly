"""Fix orchestration — the job that audits a shop's catalog and proposes grounded fixes.

Mirrors ``jobs.scan``: the caller creates and COMMITS the agent_run, then this job runs the
pipeline end-to-end under that ``run_id``, driving the run to a terminal status. Per product:
``run_audit`` → ``run_optimizer``.

**No Shopify writes.** This job only reads the already-ingested ``products`` rows and persists
``audits`` + ``fixes`` (``status = proposed``). Publishing to a merchant's store is step 4 and
happens only through the approval gate (``fixes.status = approved``) — never from here. The job
therefore needs no Shopify token, so ``TokenProvider`` on ``ctx`` is deliberately unused (same as
the scan job).

**Load-bearing commit**, exactly as in ``jobs.scan``: ``complete_agent_run`` flushes but does not
commit, and the graph nodes commit their own writes — so without the final commit the run's
terminal status never lands and the run stays ``running`` forever. Both paths commit.

The LLM client is constructed at task START, not captured at enqueue time (the ingest/scan
convention): a queued job may not run for a while.
"""

import logging

from sqlalchemy import select

from app.db import SessionLocal
from app.graph.audit import run_audit
from app.graph.optimizer import run_optimizer
from app.models import AgentRun, AgentRunStatus, Product
from app.services.audit_rubric import SEVERITY_NOT_AUDITED
from app.services.optimizer_llm import OpenAIOptimizerClient, OptimizerClient
from app.services.runs import complete_agent_run

logger = logging.getLogger(__name__)


async def propose_fixes_for_shop(
    session,
    shop_id: int,
    client: OptimizerClient,
    *,
    run_id: int | None = None,
) -> list:
    """Audit every product of ``shop_id`` and propose fixes for the discoverable ones.

    The reusable core, injected with a session and a client so both the job and the tests drive
    the same code path. Not-discoverable products are still AUDITED (that is how they are recorded
    as ``not_audited``), but the Optimizer skips them — ``run_optimizer`` returns an empty report
    for an excluded product, so the population gate lives in one place, not two.
    """
    product_ids = (
        await session.execute(
            select(Product.id).where(Product.shop_id == shop_id).order_by(Product.id)
        )
    ).scalars().all()

    reports = []
    for product_id in product_ids:
        outcome = await run_audit(session, product_id, run_id=run_id)
        if outcome.severity == SEVERITY_NOT_AUDITED:
            continue
        reports.append(await run_optimizer(session, product_id, client, run_id=run_id))
    return reports


async def run_fix_task(ctx: dict, run_id: int) -> None:
    """Audit + optimize a shop's catalog under ``run_id``, driving the run to a terminal status."""
    client = OpenAIOptimizerClient()

    async with SessionLocal() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise ValueError(f"agent_run {run_id} not found")
        shop_id = run.shop_id

        try:
            await propose_fixes_for_shop(session, shop_id, client, run_id=run_id)
            await complete_agent_run(session, run_id, AgentRunStatus.completed)
            await session.commit()  # load-bearing: persists status=completed
        except Exception:
            # A node may have raised mid-transaction, leaving the session unusable. Roll back so
            # the failed-status write can land, then commit it — the run must never be left stuck
            # ``running``. Writes a prior node already committed stay intact.
            await session.rollback()
            await complete_agent_run(session, run_id, AgentRunStatus.failed)
            await session.commit()  # load-bearing: persists status=failed
            logger.exception("Fix run failed for run_id=%s", run_id)
            raise
