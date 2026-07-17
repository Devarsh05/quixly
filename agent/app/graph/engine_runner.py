"""EngineRunner — fans a query panel across one engine and persists the raw results.

PRD §6: "fans out each query across engines; returns raw answers + citations." This node
consumes the deterministic ``QueryPanel`` from ``app.graph.interrogator`` and, for one engine
(Perplexity Sonar today):

1. Upserts the panel on ``(shop_id, fingerprint)`` so an identical re-run reuses the row.
2. Fans each query across the engine with bounded concurrency; one failing query records its
   error and does NOT abort the batch — partial results persist.
3. Writes one ``engine_runs`` row per query (always INSERT, so runs accumulate), storing the raw
   response and the normalized cited sources only. ``cited_brands_json`` / ``our_mentions_json``
   stay NULL — the Extractor (step 3) fills them.

It writes no brand parsing here and makes no bare/unstructured LLM calls: every engine answer is a
typed ``EngineAnswer``. The session and engine client are injected (this is a graph node, not a
self-contained job), which also lets tests drive it against the transaction-scoped ``db`` fixture.
"""

import asyncio
import logging

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.interrogator import QueryPanel
from app.models import EngineRun
from app.models import QueryPanel as QueryPanelRow
from app.services.perplexity import EngineAnswer, EngineClient
from app.settings import get_settings

logger = logging.getLogger(__name__)


class QueryOutcome(BaseModel):
    """The per-query result surfaced into graph state."""

    query: str
    engine_run_id: int
    ok: bool
    error: str | None = None
    answer: EngineAnswer | None = None


class EngineRunReport(BaseModel):
    """EngineRunner's typed return, held in graph state."""

    panel_id: int
    engine: str
    outcomes: list[QueryOutcome]


def _map_sources(answer: EngineAnswer) -> list[dict]:
    """Normalize engine sources to a stable list of ``{url, title?, snippet?, ...}`` objects.

    ``search_results`` is authoritative when present and mapped through in order (order carries a
    ranking signal — no merge, no dedup). Only when it is empty do we fall back to the bare
    ``citations`` URL list, synthesizing ``{"url": u}``. ``response_raw`` keeps both arrays intact.
    """
    if answer.search_results:
        return [
            sr.model_dump(exclude_none=True, exclude_defaults=False) for sr in answer.search_results
        ]
    return [{"url": url} for url in answer.citations]


async def _upsert_panel(session: AsyncSession, panel: QueryPanel, shop_id: int) -> int:
    """Insert the panel, or reuse the row for ``(shop_id, fingerprint)``. Returns its id."""
    statement = insert(QueryPanelRow).values(
        shop_id=shop_id,
        category=panel.category,
        queries_json=[q.model_dump() for q in panel.queries],
        fingerprint=panel.fingerprint,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[QueryPanelRow.shop_id, QueryPanelRow.fingerprint],
        # Touch category so RETURNING yields the id on conflict too. queries_json is a pure
        # function of the fingerprint, so this is a no-op refresh, not a semantic change.
        set_={"category": statement.excluded.category},
    ).returning(QueryPanelRow.id)

    return (await session.execute(statement)).scalar_one()


async def run_engine(
    session: AsyncSession,
    panel: QueryPanel,
    shop_id: int,
    client: EngineClient,
    *,
    max_concurrency: int | None = None,
) -> EngineRunReport:
    """Fan ``panel`` across ``client``'s engine, persist every result, return a typed report."""
    concurrency = max_concurrency or get_settings().engine_max_concurrency
    panel_id = await _upsert_panel(session, panel, shop_id)

    semaphore = asyncio.Semaphore(concurrency)

    async def _run(query_text: str) -> tuple[str, EngineAnswer | None, str | None]:
        async with semaphore:
            try:
                return query_text, await client.run_query(query_text), None
            except Exception as exc:  # noqa: BLE001 — one query's failure must not sink the batch
                logger.warning("Engine %s failed on query %r: %s", client.engine, query_text, exc)
                return query_text, None, str(exc)

    results = await asyncio.gather(*(_run(q.text) for q in panel.queries))

    outcomes: list[QueryOutcome] = []
    for query_text, answer, error in results:
        if answer is not None:
            response_raw = answer.raw
            cited_sources = _map_sources(answer)
        else:
            response_raw = {"error": error}
            cited_sources = None

        row = EngineRun(
            panel_id=panel_id,
            engine=client.engine,
            query=query_text,
            response_raw=response_raw,
            cited_sources_json=cited_sources,
            # cited_brands_json / our_mentions_json intentionally left NULL — Extractor (step 3).
        )
        session.add(row)
        await session.flush()  # assign row.id without ending the transaction

        outcomes.append(
            QueryOutcome(
                query=query_text,
                engine_run_id=row.id,
                ok=answer is not None,
                error=error,
                answer=answer,
            )
        )

    await session.commit()
    return EngineRunReport(panel_id=panel_id, engine=client.engine, outcomes=outcomes)
