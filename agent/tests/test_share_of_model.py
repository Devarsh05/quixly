"""ShareOfModelAggregator: mention-rate aggregation over persisted engine_runs.

Uses the real Postgres-backed ``db`` fixture (transaction rolled back per test). No LLM and no
EngineRunner: ``engine_runs`` are seeded directly with known ``cited_brands_json`` /
``our_mentions_json`` so every rate is hand-computable. This node makes no external calls, so
there is no live test.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.graph.share_of_model import run_share_of_model
from app.models import EngineRun, ShareOfModel, Shop, ShopStatus
from app.models import QueryPanel as QueryPanelRow

SHOP = "share-of-model-test.myshopify.com"


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _panel(db, shop_id: int, *, query_count: int, fingerprint: str = "fp1") -> int:
    """Create a panel declaring ``query_count`` queries; return its id."""
    panel = QueryPanelRow(
        shop_id=shop_id,
        category="coffee",
        queries_json=[{"text": f"q{i}"} for i in range(query_count)],
        fingerprint=fingerprint,
    )
    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    return panel.id


def _brands(names: list[str]) -> list[dict]:
    return [{"rank": i + 1, "brand": n, "product": None} for i, n in enumerate(names)]


def _mentions(mentioned: bool) -> dict:
    return {"mentioned": mentioned, "ranks": [], "matched_alias": None, "products": []}


def _ts(minute: int) -> datetime:
    return datetime(2026, 7, 17, 12, minute, 0, tzinfo=UTC)


def _add_run(
    db,
    panel_id: int,
    query: str,
    *,
    engine: str = "perplexity",
    brands: list[str] | None = None,
    mentioned: bool = False,
    ts: datetime,
    unextracted: bool = False,
    error: bool = False,
) -> None:
    """Seed one engine_run. ``error``/``unextracted`` leave the brand columns NULL."""
    if error:
        db.add(EngineRun(panel_id=panel_id, engine=engine, query=query,
                         response_raw={"error": "Perplexity returned 500"}, ts=ts))
        return
    if unextracted:
        db.add(EngineRun(panel_id=panel_id, engine=engine, query=query,
                         response_raw={"choices": [{"message": {"content": "..."}}]}, ts=ts))
        return
    db.add(EngineRun(
        panel_id=panel_id,
        engine=engine,
        query=query,
        response_raw={"choices": [{"message": {"content": "..."}}]},
        cited_brands_json=_brands(brands or []),
        our_mentions_json=_mentions(mentioned),
        ts=ts,
    ))


async def _rows(db, panel_shop_id: int) -> list[ShareOfModel]:
    # populate_existing overwrites any identity-map instance with fresh DB values, so a re-read
    # after the node's Core upsert reflects the upserted row rather than the cached one.
    result = await db.execute(
        select(ShareOfModel)
        .where(ShareOfModel.shop_id == panel_shop_id)
        .order_by(ShareOfModel.engine)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# --- hand-computed rates, one engine ---------------------------------------------------------


async def test_hand_computed_rates_single_engine(db, shop):
    panel_id = await _panel(db, shop.id, query_count=4)
    # 4 usable queries. Store mentioned in 2 of 4 → 0.5. Blue Bottle in 3 of 4 → 0.75.
    _add_run(db, panel_id, "q0", brands=["Northwind Coffee", "Blue Bottle"], mentioned=True,
             ts=_ts(1))
    _add_run(db, panel_id, "q1", brands=["Blue Bottle", "Stumptown"], mentioned=False, ts=_ts(2))
    _add_run(db, panel_id, "q2", brands=["Northwind Coffee"], mentioned=True, ts=_ts(3))
    _add_run(db, panel_id, "q3", brands=["Blue Bottle"], mentioned=False, ts=_ts(4))
    await db.commit()

    report = await run_share_of_model(db, panel_id, period="2026-07-17")

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
    assert engine.competitor_rates["Stumptown"].mention_rate == 0.25
    assert engine.competitor_rates["Intelligentsia"].mentions == 0

    (row,) = await _rows(db, shop.id)
    assert row.engine == "perplexity"
    assert row.period == "2026-07-17"
    assert row.our_rate == 0.5
    assert row.our_mentions == 2
    assert row.total_queries == 4
    assert row.competitor_rates_json["Blue Bottle"] == {"mention_rate": 0.75, "mentions": 3}


# --- multiple engines, independent rows ------------------------------------------------------


async def test_multiple_engines_one_row_each(db, shop):
    panel_id = await _panel(db, shop.id, query_count=2)
    # perplexity: store mentioned 2/2 → 1.0. copilot: store mentioned 0/2 → 0.0.
    _add_run(db, panel_id, "q0", engine="perplexity", brands=["Northwind"], mentioned=True,
             ts=_ts(1))
    _add_run(db, panel_id, "q1", engine="perplexity", brands=["Northwind"], mentioned=True,
             ts=_ts(2))
    _add_run(db, panel_id, "q0", engine="copilot", brands=["Blue Bottle"], mentioned=False,
             ts=_ts(1))
    _add_run(db, panel_id, "q1", engine="copilot", brands=["Stumptown"], mentioned=False,
             ts=_ts(2))
    await db.commit()

    report = await run_share_of_model(db, panel_id, period="2026-07-17")

    by_engine = {e.engine: e for e in report.engines}
    assert set(by_engine) == {"copilot", "perplexity"}
    assert by_engine["perplexity"].our_rate == 1.0
    assert by_engine["copilot"].our_rate == 0.0

    rows = await _rows(db, shop.id)
    assert [r.engine for r in rows] == ["copilot", "perplexity"]
    assert {r.engine: r.our_rate for r in rows} == {"copilot": 0.0, "perplexity": 1.0}


# --- accumulation / dedup: only the latest run per query counts -------------------------------


async def test_dedup_latest_run_wins(db, shop):
    panel_id = await _panel(db, shop.id, query_count=1)
    # Two runs for the same (engine, query). Older says store mentioned; newer says not.
    _add_run(db, panel_id, "q0", brands=["Northwind"], mentioned=True, ts=_ts(1))
    _add_run(db, panel_id, "q0", brands=["Blue Bottle"], mentioned=False, ts=_ts(9))
    await db.commit()

    report = await run_share_of_model(db, panel_id, period="2026-07-17")

    (engine,) = report.engines
    # Only the latest run counts: 1 query, store not mentioned, Blue Bottle mentioned.
    assert engine.total_queries == 1
    assert engine.our_mentions == 0
    assert engine.our_rate == 0.0
    assert engine.competitor_rates["Blue Bottle"].mentions == 1


# --- latest-wins-then-usable: a newer error hides an older usable run -------------------------


async def test_latest_error_excludes_query_even_if_older_run_usable(db, shop):
    panel_id = await _panel(db, shop.id, query_count=2)
    # q0: older usable, newer error → q0's current standing is unusable, so it drops out.
    _add_run(db, panel_id, "q0", brands=["Northwind"], mentioned=True, ts=_ts(1))
    _add_run(db, panel_id, "q0", error=True, ts=_ts(9))
    # q1: a clean usable run.
    _add_run(db, panel_id, "q1", brands=["Northwind"], mentioned=True, ts=_ts(2))
    await db.commit()

    report = await run_share_of_model(db, panel_id, period="2026-07-17")

    (engine,) = report.engines
    # Only q1 is usable. q0's latest is an error, so it is excluded from the denominator.
    assert engine.total_queries == 1
    assert engine.our_mentions == 1
    assert engine.our_rate == 1.0
    # Coverage over the panel's 2 queries: 1 usable / 2 = 0.5.
    assert engine.coverage == 0.5


# --- denominator excludes error/unextracted AND never-run queries; coverage reflects all ------


async def test_coverage_counts_panel_scope_including_never_run(db, shop):
    panel_id = await _panel(db, shop.id, query_count=4)
    # q0 usable; q1 error; q2 unextracted; q3 has NO run at all (orchestration gap).
    _add_run(db, panel_id, "q0", brands=["Northwind"], mentioned=True, ts=_ts(1))
    _add_run(db, panel_id, "q1", error=True, ts=_ts(2))
    _add_run(db, panel_id, "q2", unextracted=True, ts=_ts(3))
    await db.commit()

    report = await run_share_of_model(db, panel_id, period="2026-07-17")

    (engine,) = report.engines
    assert engine.total_queries == 1  # only q0 is usable
    assert engine.our_rate == 1.0  # over usable queries only
    # Coverage is 1 usable / 4 panel queries = 0.25 — the never-run q3 also depresses it.
    assert engine.coverage == 0.25


# --- alias matching: an engine naming "Onyx" counts for "Onyx Coffee Lab" ---------------------


async def test_alias_matching_bridges_short_name(db, shop):
    panel_id = await _panel(db, shop.id, query_count=1)
    # The answer names "Onyx" — not the canonical "Onyx Coffee Lab". Suffix-stripping alone would
    # not bridge this; the alias set carries it.
    _add_run(db, panel_id, "q0", brands=["Onyx"], mentioned=False, ts=_ts(1))
    await db.commit()

    report = await run_share_of_model(db, panel_id, period="2026-07-17")

    (engine,) = report.engines
    assert engine.competitor_rates["Onyx Coffee Lab"].mentions == 1
    assert engine.competitor_rates["Onyx Coffee Lab"].mention_rate == 1.0


# --- upsert idempotency: same period updates in place -----------------------------------------


async def test_upsert_idempotent_same_period(db, shop):
    panel_id = await _panel(db, shop.id, query_count=1)
    _add_run(db, panel_id, "q0", brands=["Northwind"], mentioned=True, ts=_ts(1))
    await db.commit()

    await run_share_of_model(db, panel_id, period="2026-07-17")
    rows = await _rows(db, shop.id)
    assert len(rows) == 1
    assert rows[0].our_rate == 1.0

    # Re-run same period → one row, updated not duplicated. Point q0 at a not-mentioned run.
    run = (await db.execute(select(EngineRun).where(EngineRun.query == "q0"))).scalar_one()
    run.our_mentions_json = _mentions(False)
    await db.commit()

    await run_share_of_model(db, panel_id, period="2026-07-17")
    rows = await _rows(db, shop.id)
    assert len(rows) == 1
    assert rows[0].our_rate == 0.0


# --- fully-degraded engine: NULL rate, not 0.0 ------------------------------------------------


async def test_fully_degraded_engine_writes_null_rate(db, shop):
    panel_id = await _panel(db, shop.id, query_count=2)
    # Both queries' latest runs are unusable → no usable data at all.
    _add_run(db, panel_id, "q0", error=True, ts=_ts(1))
    _add_run(db, panel_id, "q1", unextracted=True, ts=_ts(2))
    await db.commit()

    report = await run_share_of_model(db, panel_id, period="2026-07-17")

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
