"""ShareOfModelAggregator: mention-rate aggregation scoped to one run (step 5).

Uses the real Postgres-backed ``db`` fixture (transaction rolled back per test). No LLM and no
EngineRunner: an ``agent_run`` is created and ``engine_runs`` are seeded directly with known
``cited_brands_json`` / ``our_mentions_json`` and stamped with the run's id, so every rate is
hand-computable. Aggregation is invoked as ``run_share_of_model(db, run_id)``. This node makes no
external calls, so there is no live test.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.graph.share_of_model import run_share_of_model
from app.models import AgentRun, AgentRunStatus, EngineRun, ShareOfModel, Shop, ShopStatus
from app.models import QueryPanel as QueryPanelRow

SHOP = "share-of-model-test.myshopify.com"


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _make_panel(
    db, shop_id: int, *, query_count: int, fingerprint: str = "fp1"
) -> QueryPanelRow:
    """Create a panel declaring ``query_count`` queries."""
    panel = QueryPanelRow(
        shop_id=shop_id,
        category="coffee",
        queries_json=[{"text": f"q{i}"} for i in range(query_count)],
        fingerprint=fingerprint,
    )
    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    return panel


async def _make_run(db, shop_id: int, panel_id: int) -> AgentRun:
    """Create a running agent_run for the panel."""
    run = AgentRun(shop_id=shop_id, panel_id=panel_id, status=AgentRunStatus.running)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _brands(names: list[str]) -> list[dict]:
    return [{"rank": i + 1, "brand": n, "product": None} for i, n in enumerate(names)]


def _mentions(mentioned: bool) -> dict:
    return {"mentioned": mentioned, "ranks": [], "matched_alias": None, "products": []}


def _ts(minute: int) -> datetime:
    return datetime(2026, 7, 17, 12, minute, 0, tzinfo=UTC)


def _add_run(
    db,
    panel_id: int,
    run_id: int,
    query: str,
    *,
    engine: str = "perplexity",
    brands: list[str] | None = None,
    mentioned: bool = False,
    ts: datetime | None = None,
    unextracted: bool = False,
    error: bool = False,
) -> None:
    """Seed one engine_run stamped with ``run_id``. ``error``/``unextracted`` leave brands NULL."""
    ts = ts if ts is not None else _ts(1)
    if error:
        db.add(EngineRun(panel_id=panel_id, run_id=run_id, engine=engine, query=query,
                         response_raw={"error": "Perplexity returned 500"}, ts=ts))
        return
    if unextracted:
        db.add(EngineRun(panel_id=panel_id, run_id=run_id, engine=engine, query=query,
                         response_raw={"choices": [{"message": {"content": "..."}}]}, ts=ts))
        return
    db.add(EngineRun(
        panel_id=panel_id,
        run_id=run_id,
        engine=engine,
        query=query,
        response_raw={"choices": [{"message": {"content": "..."}}]},
        cited_brands_json=_brands(brands or []),
        our_mentions_json=_mentions(mentioned),
        ts=ts,
    ))


async def _rows(db, shop_id: int) -> list[ShareOfModel]:
    # populate_existing overwrites any identity-map instance with fresh DB values, so a re-read
    # after the node's Core upsert reflects the upserted row rather than the cached one.
    result = await db.execute(
        select(ShareOfModel)
        .where(ShareOfModel.shop_id == shop_id)
        .order_by(ShareOfModel.engine)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# --- hand-computed rates, one engine; period defaults to the run's start date ----------------


async def test_hand_computed_rates_single_engine(db, shop):
    panel = await _make_panel(db, shop.id, query_count=4)
    run = await _make_run(db, shop.id, panel.id)
    # 4 usable queries. Store mentioned in 2 of 4 → 0.5. Blue Bottle in 3 of 4 → 0.75.
    _add_run(db, panel.id, run.id, "q0", brands=["Northwind Coffee", "Blue Bottle"], mentioned=True)
    _add_run(db, panel.id, run.id, "q1", brands=["Blue Bottle", "Stumptown"], mentioned=False)
    _add_run(db, panel.id, run.id, "q2", brands=["Northwind Coffee"], mentioned=True)
    _add_run(db, panel.id, run.id, "q3", brands=["Blue Bottle"], mentioned=False)
    await db.commit()

    report = await run_share_of_model(db, run.id)  # no explicit period

    assert report.panel_id == panel.id
    assert len(report.engines) == 1
    engine = report.engines[0]
    assert engine.engine == "perplexity"
    assert engine.total_queries == 4
    assert engine.coverage == 1.0
    assert engine.our_mentions == 2
    assert engine.our_rate == 0.5
    assert engine.competitor_rates["Blue Bottle"].mentions == 3
    assert engine.competitor_rates["Blue Bottle"].mention_rate == 0.75
    assert engine.competitor_rates["Stumptown"].mentions == 1
    assert engine.competitor_rates["Intelligentsia"].mentions == 0

    # period defaults to the run's start date (a run is a point-in-time scan).
    expected_period = run.started_at.date().isoformat()
    assert report.period == expected_period

    (row,) = await _rows(db, shop.id)
    assert row.engine == "perplexity"
    assert row.period == expected_period
    assert row.our_rate == 0.5
    assert row.our_mentions == 2
    assert row.total_queries == 4
    assert row.competitor_rates_json["Blue Bottle"] == {"mention_rate": 0.75, "mentions": 3}


# --- multiple engines, independent rows ------------------------------------------------------


async def test_multiple_engines_one_row_each(db, shop):
    panel = await _make_panel(db, shop.id, query_count=2)
    run = await _make_run(db, shop.id, panel.id)
    # perplexity: store mentioned 2/2 → 1.0. copilot: store mentioned 0/2 → 0.0.
    _add_run(db, panel.id, run.id, "q0", engine="perplexity", brands=["Northwind"], mentioned=True)
    _add_run(db, panel.id, run.id, "q1", engine="perplexity", brands=["Northwind"], mentioned=True)
    _add_run(db, panel.id, run.id, "q0", engine="copilot", brands=["Blue Bottle"], mentioned=False)
    _add_run(db, panel.id, run.id, "q1", engine="copilot", brands=["Stumptown"], mentioned=False)
    await db.commit()

    report = await run_share_of_model(db, run.id, period="2026-07-17")

    by_engine = {e.engine: e for e in report.engines}
    assert set(by_engine) == {"copilot", "perplexity"}
    assert by_engine["perplexity"].our_rate == 1.0
    assert by_engine["copilot"].our_rate == 0.0

    rows = await _rows(db, shop.id)
    assert [r.engine for r in rows] == ["copilot", "perplexity"]
    assert {r.engine: r.our_rate for r in rows} == {"copilot": 0.0, "perplexity": 1.0}


# --- coverage: BOTH exclusion branches in one run --------------------------------------------


async def test_coverage_excludes_unusable_row_and_never_run_query(db, shop):
    # Panel scope is 4 queries. In THIS run: q0/q1 usable, q2 ran but is unusable (error →
    # cited_brands_json NULL), q3 never ran (no engine_runs row). The two exclusions are distinct
    # branches: q2 tests denominator exclusion of a present-but-unusable row; q3 tests that a
    # never-run panel query still depresses coverage.
    panel = await _make_panel(db, shop.id, query_count=4)
    run = await _make_run(db, shop.id, panel.id)
    _add_run(db, panel.id, run.id, "q0", brands=["Northwind"], mentioned=True)
    _add_run(db, panel.id, run.id, "q1", brands=["Northwind"], mentioned=True)
    _add_run(db, panel.id, run.id, "q2", error=True)  # ran this run, unusable
    # q3: no row at all
    await db.commit()

    report = await run_share_of_model(db, run.id, period="2026-07-17")

    (engine,) = report.engines
    # total_queries counts only usable queries — the present-but-unusable q2 is excluded (2, not 3).
    assert engine.total_queries == 2
    assert engine.our_rate == 1.0  # both usable queries mention the store
    # coverage is over the panel's full scope of 4 — the never-run q3 lowers it (2/4, not 2/3).
    assert engine.coverage == 0.5


# --- alias matching: an engine naming "Onyx" counts for "Onyx Coffee Lab" ---------------------


async def test_alias_matching_bridges_short_name(db, shop):
    panel = await _make_panel(db, shop.id, query_count=1)
    run = await _make_run(db, shop.id, panel.id)
    _add_run(db, panel.id, run.id, "q0", brands=["Onyx"], mentioned=False)
    await db.commit()

    report = await run_share_of_model(db, run.id, period="2026-07-17")

    (engine,) = report.engines
    assert engine.competitor_rates["Onyx Coffee Lab"].mentions == 1
    assert engine.competitor_rates["Onyx Coffee Lab"].mention_rate == 1.0


# --- upsert idempotency: same run + period updates in place -----------------------------------


async def test_upsert_idempotent_same_period(db, shop):
    panel = await _make_panel(db, shop.id, query_count=1)
    run = await _make_run(db, shop.id, panel.id)
    _add_run(db, panel.id, run.id, "q0", brands=["Northwind"], mentioned=True)
    await db.commit()

    await run_share_of_model(db, run.id, period="2026-07-17")
    rows = await _rows(db, shop.id)
    assert len(rows) == 1
    assert rows[0].our_rate == 1.0

    # Repoint q0 at a not-mentioned run and re-aggregate the same run/period → one row, updated.
    er = (await db.execute(select(EngineRun).where(EngineRun.query == "q0"))).scalar_one()
    er.our_mentions_json = _mentions(False)
    await db.commit()

    await run_share_of_model(db, run.id, period="2026-07-17")
    rows = await _rows(db, shop.id)
    assert len(rows) == 1
    assert rows[0].our_rate == 0.0


# --- fully-degraded engine: NULL rate, not 0.0 ------------------------------------------------


async def test_fully_degraded_engine_writes_null_rate(db, shop):
    panel = await _make_panel(db, shop.id, query_count=2)
    run = await _make_run(db, shop.id, panel.id)
    _add_run(db, panel.id, run.id, "q0", error=True)
    _add_run(db, panel.id, run.id, "q1", unextracted=True)
    await db.commit()

    report = await run_share_of_model(db, run.id, period="2026-07-17")

    (engine,) = report.engines
    assert engine.our_rate is None  # NULL, never 0.0 — "no data", not "0% recommendation rate"
    assert engine.our_mentions == 0
    assert engine.total_queries == 0
    assert engine.coverage == 0.0
    assert engine.competitor_rates == {}

    (row,) = await _rows(db, shop.id)
    assert row.our_rate is None
    assert row.total_queries == 0
    assert row.competitor_rates_json == {}


# --- run isolation: aggregating one run does not see another run's rows -----------------------


async def test_run_isolation_same_panel(db, shop):
    # The core guarantee run identity buys: two scans of the SAME panel, different brand data.
    # Aggregating run A must reflect only A's rows, never B's.
    panel = await _make_panel(db, shop.id, query_count=1)
    run_a = await _make_run(db, shop.id, panel.id)
    run_b = await _make_run(db, shop.id, panel.id)

    # Run A: store mentioned. Run B: store not mentioned, a competitor instead.
    _add_run(db, panel.id, run_a.id, "q0", brands=["Northwind"], mentioned=True)
    _add_run(db, panel.id, run_b.id, "q0", brands=["Blue Bottle"], mentioned=False)
    await db.commit()

    report_a = await run_share_of_model(db, run_a.id, period="A")
    (engine_a,) = report_a.engines
    assert engine_a.total_queries == 1
    assert engine_a.our_rate == 1.0  # only A's mention — B's row is invisible
    assert engine_a.competitor_rates["Blue Bottle"].mentions == 0

    report_b = await run_share_of_model(db, run_b.id, period="B")
    (engine_b,) = report_b.engines
    assert engine_b.total_queries == 1
    assert engine_b.our_rate == 0.0
    assert engine_b.competitor_rates["Blue Bottle"].mentions == 1


# --- defensive within-run dedup: latest row per (engine, query) wins --------------------------


async def test_within_run_dedup_latest_wins(db, shop):
    # One run should hold one row per (engine, query), but the node dedups defensively. Two rows
    # for the same (run, engine, query) with different ts → only the latest counts.
    panel = await _make_panel(db, shop.id, query_count=1)
    run = await _make_run(db, shop.id, panel.id)
    _add_run(db, panel.id, run.id, "q0", brands=["Northwind"], mentioned=True, ts=_ts(1))
    _add_run(db, panel.id, run.id, "q0", brands=["Blue Bottle"], mentioned=False, ts=_ts(9))
    await db.commit()

    report = await run_share_of_model(db, run.id, period="2026-07-17")

    (engine,) = report.engines
    assert engine.total_queries == 1  # deduped to one, not double-counted
    assert engine.our_mentions == 0  # latest row does not mention the store
    assert engine.competitor_rates["Blue Bottle"].mentions == 1
