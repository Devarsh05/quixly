"""ShareOfModelAggregator — turns persisted extractions into per-engine mention rates (PRD §6).

The measurement node. EngineRunner writes one ``engine_runs`` row per (query, engine) — INSERT
only, ``ts``-stamped, so runs accumulate period over period — and the Extractor fills each row's
``cited_brands_json`` / ``our_mentions_json``. This node reads those rows for a panel and, per
engine, computes the store's **mention rate** (the metric) and each tracked competitor's mention
rate, then UPSERTs one ``share_of_model`` row per engine.

It makes **no external API calls** — pure aggregation over persisted rows — so there is no live
contract to test; every test is DB-backed. The session is injected so tests drive it against the
transaction-scoped ``db`` fixture.

Two load-bearing rules (decided with the product owner):

1. **Latest-wins, then usable.** Within an engine, dedup to the LATEST run per query
   (``max(ts)``, tie-broken by ``max(id)``) over *all* runs, then classify that latest run as
   usable (``cited_brands_json`` present) or not. A query whose current standing is an
   error/unextracted run is excluded from the denominator even if an older run was extracted —
   we count current standings only.
2. **``our_rate`` is NULL on a fully-degraded scan**, never ``0.0``. ``0/0`` is *no data*, not
   *0%*; writing ``0.0`` would be a false 0%-recommendation finding a merchant would act on.

Coverage is measured against the panel's full query count (``len(queries_json)``), not the
queries that happened to run — so an orchestration gap (a query that never ran at all) also
depresses coverage, not just engine errors.

Rank-weighting is deliberately not built here (ranks live in ``engine_runs``); the metric is a
flat mention rate.
"""

from collections import defaultdict
from datetime import date

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EngineRun, ShareOfModel
from app.models import QueryPanel as QueryPanelRow
from app.services.matching import normalize_and_match

# Placeholder competitor set for the coffee vertical, until the later org-memory step persists the
# competitor set per shop (mirrors ``extractor.STORE_ALIASES``). Aliases matter: an engine may say
# "Onyx" while the canonical name is "Onyx Coffee Lab", and the normalizer's suffix-stripping alone
# won't bridge that. name -> alias tuple.
COMPETITOR_ALIASES: dict[str, tuple[str, ...]] = {
    "Blue Bottle": ("Blue Bottle", "Blue Bottle Coffee"),
    "Counter Culture": ("Counter Culture", "Counter Culture Coffee"),
    "Stumptown": ("Stumptown", "Stumptown Coffee", "Stumptown Coffee Roasters"),
    "Onyx Coffee Lab": ("Onyx", "Onyx Coffee Lab", "Onyx Coffee"),
    "Intelligentsia": ("Intelligentsia", "Intelligentsia Coffee"),
}


class CompetitorRate(BaseModel):
    """One competitor's mention rate for an engine over the usable queries."""

    mention_rate: float
    mentions: int


class EngineShare(BaseModel):
    """Per-engine share-of-model result held in graph state."""

    engine: str
    our_rate: float | None  # NULL on a fully-degraded scan — never 0.0
    our_mentions: int
    total_queries: int  # usable latest runs (the rate denominator)
    coverage: float  # usable latest runs / panel query count (scan completeness)
    competitor_rates: dict[str, CompetitorRate]


class ShareOfModelReport(BaseModel):
    """ShareOfModelAggregator's typed return, held in graph state."""

    panel_id: int
    period: str
    engines: list[EngineShare]


def _latest_per_query(runs: list[EngineRun]) -> dict[str, dict[str, EngineRun]]:
    """Group runs by engine, keeping the latest run per query (max ts, tie-broken by id)."""
    latest: dict[str, dict[str, EngineRun]] = defaultdict(dict)
    for run in runs:
        current = latest[run.engine].get(run.query)
        if current is None or (run.ts, run.id) > (current.ts, current.id):
            latest[run.engine][run.query] = run
    return latest


async def run_share_of_model(
    session: AsyncSession,
    panel_id: int,
    *,
    period: str | None = None,
    competitor_aliases: dict[str, tuple[str, ...]] = COMPETITOR_ALIASES,
) -> ShareOfModelReport:
    """Aggregate a panel's engine_runs into per-engine mention rates; UPSERT one row per engine."""
    panel = (
        await session.execute(select(QueryPanelRow).where(QueryPanelRow.id == panel_id))
    ).scalar_one()
    shop_id = panel.shop_id
    # Coverage is measured against the panel's full scope so a query that never ran also counts
    # against it, not only engine errors.
    panel_query_count = len(panel.queries_json)

    runs = (
        await session.execute(select(EngineRun).where(EngineRun.panel_id == panel_id))
    ).scalars().all()
    latest = _latest_per_query(runs)

    # period defaults to the ISO date of the latest usable run across all engines.
    usable_runs = [
        run
        for by_query in latest.values()
        for run in by_query.values()
        if run.cited_brands_json is not None
    ]
    if period is None:
        period = (
            max(run.ts for run in usable_runs).date().isoformat()
            if usable_runs
            else date.today().isoformat()
        )

    engines_out: list[EngineShare] = []
    for engine in sorted(latest):
        usable = [run for run in latest[engine].values() if run.cited_brands_json is not None]
        total_queries = len(usable)
        coverage = total_queries / panel_query_count if panel_query_count else 0.0

        if total_queries == 0:
            # Fully degraded: NULL rate (not 0.0) and an empty competitor map. See module docstring.
            our_rate: float | None = None
            our_mentions = 0
            competitor_rates: dict[str, CompetitorRate] = {}
        else:
            # our_mentions READS the Extractor's self-match; do NOT re-match store identity here.
            our_mentions = sum(
                1 for run in usable if (run.our_mentions_json or {}).get("mentioned") is True
            )
            our_rate = our_mentions / total_queries

            competitor_rates = {}
            for name, aliases in competitor_aliases.items():
                hits = sum(
                    1
                    for run in usable
                    if normalize_and_match(
                        [b["brand"] for b in (run.cited_brands_json or [])], aliases
                    )
                )
                competitor_rates[name] = CompetitorRate(
                    mention_rate=hits / total_queries, mentions=hits
                )

        statement = insert(ShareOfModel).values(
            shop_id=shop_id,
            engine=engine,
            period=period,
            our_rate=our_rate,
            our_mentions=our_mentions,
            total_queries=total_queries,
            competitor_rates_json={n: cr.model_dump() for n, cr in competitor_rates.items()},
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_share_of_model_shop_engine_period",
            set_={
                "our_rate": statement.excluded.our_rate,
                "our_mentions": statement.excluded.our_mentions,
                "total_queries": statement.excluded.total_queries,
                "competitor_rates_json": statement.excluded.competitor_rates_json,
                # created_at intentionally NOT touched — first-seen timestamp preserved on re-run.
            },
        )
        await session.execute(statement)

        engines_out.append(
            EngineShare(
                engine=engine,
                our_rate=our_rate,
                our_mentions=our_mentions,
                total_queries=total_queries,
                coverage=coverage,
                competitor_rates=competitor_rates,
            )
        )

    await session.commit()
    return ShareOfModelReport(panel_id=panel_id, period=period, engines=engines_out)
