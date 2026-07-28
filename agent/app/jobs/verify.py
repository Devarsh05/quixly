"""Verify orchestration — the post-publish scan plus the delta against its baseline.

**This is ``jobs.scan`` plus one node, not a second scan.** It calls the same
``run_scan_pipeline`` (EngineRunner → Extractor → ShareOfModelAggregator), so a verification run
IS a scan run: it writes ``engine_runs`` and ``share_of_model`` under its own ``run_id`` exactly
like any other, and the report route reads it with no special-casing. Then ``run_verifier``
computes the delta against the baseline's already-persisted aggregates.

**Gate 1 — the measured set is NOT re-resolved here.** ``measured_fixes`` arrives from the route
(``services/baselines.resolve_measured_set``) as a snapshot and is passed straight through. A
publish landing between route validation and this job must not change what gets measured: the
settle gate, ``published_at_max`` and the persisted manifest all read the one snapshot, so the
409 decision the merchant already got and the row that lands can never disagree.

**No Shopify token.** Like ``jobs.scan`` and ``jobs.fix``, this job makes no Admin API call, so
``TokenProvider`` on ``ctx`` is deliberately unused. It reads ``fixes`` nowhere at all —
``fixes.published_at`` is measurement-sacred and the Publisher is its only writer.

**Load-bearing commit**, same as every sibling job: ``complete_agent_run`` flushes but does not
commit, so without the explicit commit the run stays ``running`` forever. Both paths commit.
"""

import logging

from app.db import SessionLocal
from app.graph.verifier import VerificationAborted, VerificationReport, run_verifier
from app.jobs.scan import run_scan_pipeline
from app.models import AgentRun, AgentRunStatus
from app.services.baselines import MeasuredFix
from app.services.extractor_llm import OpenAIExtractorClient
from app.services.panels import load_query_panel
from app.services.perplexity import PerplexitySonarClient
from app.services.runs import complete_agent_run

logger = logging.getLogger(__name__)


async def run_verify_task(
    ctx: dict,
    run_id: int,
    baseline_run_id: int,
    measured_fixes: list[dict],
    force: bool = False,
) -> VerificationReport:
    """Run the post-publish scan under ``run_id``, then record the delta vs ``baseline_run_id``.

    ``measured_fixes`` is the route's snapshot, serialized for the queue. Clients are built at task
    START, never captured at enqueue time (the standing convention — a queued job may not run for
    a while).
    """
    engine_client = PerplexitySonarClient()
    extractor_client = OpenAIExtractorClient()
    # Rehydrated, not re-queried: this is the route's snapshot, verbatim.
    measured = [MeasuredFix.model_validate(fix) for fix in measured_fixes]

    async with SessionLocal() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            raise ValueError(f"agent_run {run_id} not found")
        # The route created this run with the baseline's panel_id — bind, never rebuild. The
        # verifier re-asserts the binding and the panel's fingerprint before any delta is computed.
        panel = await load_query_panel(session, run.panel_id)

        try:
            await run_scan_pipeline(session, run, panel, engine_client, extractor_client)
            report = await run_verifier(
                session,
                run_id,
                baseline_run_id=baseline_run_id,
                measured_fixes=measured,
                force=force,
            )
            await complete_agent_run(session, run_id, AgentRunStatus.completed)
            await session.commit()  # load-bearing: persists status=completed
            return report

        except VerificationAborted:
            # The scan itself may well have succeeded — its engine_runs and share_of_model rows are
            # already committed and stay. What could not be trusted is the DELTA, and no
            # verifications row was written. Not retryable: re-running would re-spend a full panel
            # against the same broken precondition.
            await session.rollback()
            await complete_agent_run(session, run_id, AgentRunStatus.failed)
            await session.commit()
            logger.exception("Verification run %s aborted; no delta recorded", run_id)
            raise

        except Exception:
            # A node may have raised mid-transaction, leaving the session unusable. Roll back so
            # the failed-status write can land, then commit it — the run must never be left stuck
            # ``running``. Writes a prior node already committed stay intact.
            await session.rollback()
            await complete_agent_run(session, run_id, AgentRunStatus.failed)
            await session.commit()  # load-bearing: persists status=failed
            logger.exception("Verification run %s failed", run_id)
            raise
