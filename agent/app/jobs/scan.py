"""Scan orchestration — the Arq job that drives one full measurement run.

The scan route (``app/api/scan.py``) creates and COMMITS the agent_run + panel, then enqueues
this job with the ``run_id``. The job runs the pipeline end-to-end under that run_id:
EngineRunner → Extractor → ShareOfModelAggregator → complete the run.

**Load-bearing commit.** ``create_agent_run`` / ``complete_agent_run`` flush but do NOT commit,
and the graph nodes commit their own writes — so without the final commit here the run's terminal
status is never persisted and the run stays ``running`` forever. BOTH the success and the failed
paths commit.

Clients are constructed at task start (not captured at enqueue time), mirroring the ingest job:
a queued job may not start for a while, and a 60-minute-scoped Shopify token could expire in the
gap — irrelevant here (the scan makes no Admin API calls) but the pattern is kept uniform. The
scan needs no Shopify token, so ``TokenProvider`` (on ``ctx``) is deliberately unused.
"""

import logging

from app.db import SessionLocal
from app.graph.engine_runner import run_engine
from app.graph.extractor import run_extractor
from app.graph.share_of_model import run_share_of_model
from app.models import AgentRun, AgentRunStatus
from app.services.extractor_llm import OpenAIExtractorClient
from app.services.panels import load_query_panel
from app.services.perplexity import PerplexitySonarClient
from app.services.runs import complete_agent_run

logger = logging.getLogger(__name__)


async def run_scan_task(ctx: dict, run_id: int) -> None:
    """Run one scan end-to-end under ``run_id``, driving the agent_run to a terminal status."""
    engine_client = PerplexitySonarClient()
    extractor_client = OpenAIExtractorClient()

    async with SessionLocal() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise ValueError(f"agent_run {run_id} not found")
        shop_id = run.shop_id
        panel_id = run.panel_id
        panel = await load_query_panel(session, panel_id)

        try:
            await run_engine(session, panel, shop_id, engine_client, run_id=run_id)
            await run_extractor(session, panel_id, extractor_client, run_id=run_id)
            await run_share_of_model(session, run_id)
            await complete_agent_run(session, run_id, AgentRunStatus.completed)
            await session.commit()  # load-bearing: persists status=completed
        except Exception:
            # A node may have raised mid-transaction, leaving the session in a failed state.
            # Roll back so the failed-status write can land, then commit it — the run must never
            # be left stuck ``running``. Any writes a prior node already committed stay intact.
            await session.rollback()
            await complete_agent_run(session, run_id, AgentRunStatus.failed)
            await session.commit()  # load-bearing: persists status=failed
            logger.exception("Scan failed for run_id=%s", run_id)
            raise
