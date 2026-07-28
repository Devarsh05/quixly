"""Verifier: the share-of-model delta between a baseline and a post-publish scan (Phase 4 step 1).

DB-backed against the transaction-scoped ``db`` fixture. ``share_of_model`` rows are seeded
directly with known values so every delta is hand-computable; the node makes no external calls of
any kind, so there is no live test.

Every assertion is scoped to its own run/shop, and the rows a query must EXCLUDE are seeded —
a query-over-everything assertion is unfalsifiable until the table holds rows it should skip.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.graph.interrogator import build_query_panel
from app.graph.verifier import VerificationAborted, run_verifier
from app.models import (
    AgentRun,
    AgentRunStatus,
    Fix,
    FixStatus,
    FixType,
    Product,
    ShareOfModel,
    Shop,
    ShopStatus,
    Verification,
)
from app.models import QueryPanel as QueryPanelRow
from app.services.baselines import MeasuredFix, resolve_measured_set
from app.services.panels import upsert_panel

PUBLISHED_AT = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
# Post scan starts well past the 168h settle window, so these tests exercise the settled path.
POST_START = PUBLISHED_AT + timedelta(days=10)

MEASURED = [
    MeasuredFix(
        fix_id=9702,
        product_id=114,
        type=FixType.description,
        target="body_html",
        published_at=PUBLISHED_AT,
    )
]


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain="verifier-test.myshopify.com", status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


@pytest.fixture
async def panel(db, shop):
    """The real deterministic coffee panel, so the fingerprint recheck runs against real data."""
    panel_id = await upsert_panel(db, build_query_panel(), shop.id)
    await db.commit()
    return await db.get(QueryPanelRow, panel_id)


async def _run(db, shop_id: int, panel_id: int, *, started_at: datetime) -> AgentRun:
    run = AgentRun(
        shop_id=shop_id,
        panel_id=panel_id,
        status=AgentRunStatus.completed,
        started_at=started_at,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _aggregate(
    db,
    run: AgentRun,
    *,
    engine: str = "perplexity",
    our_rate: float | None,
    our_mentions: int | None = 0,
    total_queries: int | None = 24,
    competitors: dict | None = None,
) -> ShareOfModel:
    row = ShareOfModel(
        run_id=run.id,
        shop_id=run.shop_id,
        engine=engine,
        period="2026-07-21",
        our_rate=our_rate,
        our_mentions=our_mentions,
        total_queries=total_queries,
        competitor_rates_json=competitors if competitors is not None else {},
    )
    db.add(row)
    await db.commit()
    return row


async def _pair(db, shop, panel) -> tuple[AgentRun, AgentRun]:
    """A baseline run and a post run on the same panel."""
    baseline = await _run(db, shop.id, panel.id, started_at=PUBLISHED_AT - timedelta(days=7))
    post = await _run(db, shop.id, panel.id, started_at=POST_START)
    return baseline, post


async def _rows(db, run_id: int) -> list[Verification]:
    return list(
        (
            await db.execute(
                select(Verification)
                .where(Verification.run_id == run_id)
                .order_by(Verification.engine)
            )
        ).scalars()
    )


# --- happy path --------------------------------------------------------------------------------


async def test_delta_is_post_minus_pre(db, shop, panel):
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.0, our_mentions=0)
    await _aggregate(db, post, our_rate=0.25, our_mentions=6)

    report = await run_verifier(
        db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED
    )

    (engine,) = report.engines
    assert engine.engine == "perplexity"
    assert engine.pre_rate == 0.0
    assert engine.post_rate == 0.25
    assert engine.delta == 0.25
    assert report.settle_satisfied is True
    assert report.published_at_max == PUBLISHED_AT
    assert report.panel_fingerprint == panel.fingerprint

    (row,) = await _rows(db, post.id)
    assert row.delta == 0.25
    assert row.baseline_run_id == baseline.id
    assert row.shop_id == shop.id
    assert row.panel_fingerprint == panel.fingerprint
    assert row.measured_fix_count == 1
    assert row.measured_fixes_json[0]["fix_id"] == 9702
    assert row.settle_satisfied is True
    assert row.settle_hours == pytest.approx(240.0)


async def test_negative_delta_is_recorded_as_is(db, shop, panel):
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.5, our_mentions=12)
    await _aggregate(db, post, our_rate=0.25, our_mentions=6)

    report = await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    assert report.engines[0].delta == -0.25


async def test_competitor_deltas_union_both_sides(db, shop, panel):
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(
        db,
        baseline,
        our_rate=0.0,
        competitors={
            "Blue Bottle": {"mention_rate": 0.25, "mentions": 6},
            "Gone Roasters": {"mention_rate": 0.5, "mentions": 12},
        },
    )
    await _aggregate(
        db,
        post,
        our_rate=0.25,
        competitors={
            "Blue Bottle": {"mention_rate": 0.125, "mentions": 3},
            "New Roasters": {"mention_rate": 0.75, "mentions": 18},
        },
    )

    report = await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)
    competitors = report.engines[0].competitors

    assert set(competitors) == {"Blue Bottle", "Gone Roasters", "New Roasters"}
    assert competitors["Blue Bottle"].delta == -0.125
    # Present on one side only: the missing side is no-data, so the delta is NULL, not the rate.
    assert competitors["Gone Roasters"].post_rate is None
    assert competitors["Gone Roasters"].delta is None
    assert competitors["New Roasters"].pre_rate is None
    assert competitors["New Roasters"].delta is None


# --- GATE 2: no-data is decided by total_queries, never by our_rate ----------------------------


async def test_degraded_post_scan_yields_null_delta_not_a_fabricated_regression(db, shop, panel):
    """The exact shape ``graph/share_of_model.py`` writes when an engine returns nothing usable:
    ``our_rate = NULL``, ``our_mentions = 0``, ``total_queries = 0``, and an empty
    ``competitor_rates_json``.

    A flaky post-scan must NOT read as a collapse to zero."""
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.5, our_mentions=12, total_queries=24)
    await _aggregate(db, post, our_rate=None, our_mentions=0, total_queries=0)

    report = await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    (engine,) = report.engines
    assert engine.pre_rate == 0.5
    assert engine.post_rate is None
    assert engine.delta is None  # NOT -0.5
    # The diagnostics survive so a reader can see WHY the side is NULL.
    assert engine.post_total_queries == 0
    assert engine.post_mentions == 0

    (row,) = await _rows(db, post.id)
    assert row.post_rate is None
    assert row.delta is None


async def test_zero_rate_with_zero_queries_is_still_no_data(db, shop, panel):
    """Defence in depth: ``our_rate`` NULL-on-degraded is one branch of one function, the columns
    are nullable and no CHECK ties them together. A literal 0.0 over 0 queries must NOT be read as
    a real 0% rate — that is the fabricated-regression bug."""
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.5, our_mentions=12, total_queries=24)
    await _aggregate(
        db,
        post,
        our_rate=0.0,  # literal zero, NOT NULL
        our_mentions=0,
        total_queries=0,
        competitors={"Blue Bottle": {"mention_rate": 0.0, "mentions": 0}},
    )

    report = await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    (engine,) = report.engines
    assert engine.post_rate is None
    assert engine.delta is None
    assert engine.competitors["Blue Bottle"].post_rate is None


async def test_null_total_queries_is_no_data(db, shop, panel):
    """``total_queries`` is a NULLABLE column, so a NULL is representable even though today's
    aggregator never writes one. It must not be treated as data."""
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.5, our_mentions=12, total_queries=24)
    await _aggregate(db, post, our_rate=0.9, our_mentions=None, total_queries=None)

    report = await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    assert report.engines[0].post_rate is None
    assert report.engines[0].delta is None


async def test_degraded_baseline_yields_null_delta(db, shop, panel):
    """NULL propagates from the PRE side too — a delta off a degraded baseline is a fabricated
    uplift, the mirror image of the fabricated regression."""
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=None, our_mentions=0, total_queries=0)
    await _aggregate(db, post, our_rate=0.25, our_mentions=6, total_queries=24)

    report = await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    assert report.engines[0].pre_rate is None
    assert report.engines[0].post_rate == 0.25
    assert report.engines[0].delta is None


# --- engine union ------------------------------------------------------------------------------


async def test_engine_present_in_baseline_only_still_gets_a_row(db, shop, panel):
    """A total regression must be VISIBLE, not silently absent from the report."""
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, engine="perplexity", our_rate=0.5)
    await _aggregate(db, baseline, engine="gemini", our_rate=0.3)
    await _aggregate(db, post, engine="perplexity", our_rate=0.6)

    report = await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    assert [e.engine for e in report.engines] == ["gemini", "perplexity"]
    gemini = report.engines[0]
    assert gemini.pre_rate == 0.3
    assert gemini.post_rate is None
    assert gemini.delta is None
    assert len(await _rows(db, post.id)) == 2


async def test_engine_present_in_post_only_gets_null_pre(db, shop, panel):
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, engine="perplexity", our_rate=0.5)
    await _aggregate(db, post, engine="perplexity", our_rate=0.6)
    await _aggregate(db, post, engine="gemini", our_rate=0.4)

    report = await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    gemini = next(e for e in report.engines if e.engine == "gemini")
    assert gemini.pre_rate is None
    assert gemini.delta is None


# --- run scoping + idempotency -----------------------------------------------------------------


async def test_aggregates_from_other_runs_are_not_read(db, shop, panel):
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.0)
    await _aggregate(db, post, our_rate=0.25)

    # A third run with a wildly different rate — seeded so the run-scoping is falsifiable.
    other = await _run(db, shop.id, panel.id, started_at=POST_START)
    await _aggregate(db, other, our_rate=0.99, our_mentions=23)

    report = await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    assert report.engines[0].delta == 0.25
    assert await _rows(db, other.id) == []


async def test_recompute_upserts_and_preserves_created_at(db, shop, panel):
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.0)
    await _aggregate(db, post, our_rate=0.25)

    await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)
    (first,) = await _rows(db, post.id)
    created_at = first.created_at

    await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)
    rows = await _rows(db, post.id)

    assert len(rows) == 1  # one row per (run, engine), never a second
    await db.refresh(rows[0])
    assert rows[0].created_at == created_at


# --- abort paths: nothing is written -----------------------------------------------------------


async def test_panel_mismatch_aborts_and_writes_nothing(db, shop, panel):
    other_panel = QueryPanelRow(
        shop_id=shop.id, category="coffee", queries_json=[{"text": "q0"}], fingerprint="fp-other"
    )
    db.add(other_panel)
    await db.commit()
    await db.refresh(other_panel)

    baseline = await _run(db, shop.id, panel.id, started_at=PUBLISHED_AT - timedelta(days=7))
    post = await _run(db, shop.id, other_panel.id, started_at=POST_START)
    await _aggregate(db, baseline, our_rate=0.0)
    await _aggregate(db, post, our_rate=0.25)

    with pytest.raises(VerificationAborted, match="Panel mismatch"):
        await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    assert await _rows(db, post.id) == []


async def test_fingerprint_mismatch_aborts(db, shop, panel):
    """The one way an immutable-by-construction panel row can still drift: an out-of-band edit."""
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.0)
    await _aggregate(db, post, our_rate=0.25)

    row = await db.get(QueryPanelRow, panel.id)
    queries = list(row.queries_json)
    queries[0] = {**queries[0], "text": "best DIFFERENT coffee beans"}
    row.queries_json = queries
    await db.commit()

    with pytest.raises(VerificationAborted, match="does not match its fingerprint"):
        await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    assert await _rows(db, post.id) == []


async def test_empty_measured_set_aborts(db, shop, panel):
    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.0)
    await _aggregate(db, post, our_rate=0.25)

    with pytest.raises(VerificationAborted, match="No published fixes"):
        await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=[])

    assert await _rows(db, post.id) == []


async def test_baseline_from_another_shop_aborts(db, shop, panel):
    other = Shop(shop_domain="verifier-other.myshopify.com", status=ShopStatus.active)
    db.add(other)
    await db.commit()
    await db.refresh(other)
    other_panel_id = await upsert_panel(db, build_query_panel(), other.id)
    await db.commit()

    baseline = await _run(db, other.id, other_panel_id, started_at=PUBLISHED_AT)
    post = await _run(db, shop.id, panel.id, started_at=POST_START)
    await _aggregate(db, baseline, our_rate=0.0)
    await _aggregate(db, post, our_rate=0.25)

    with pytest.raises(VerificationAborted, match="belongs to shop"):
        await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)


# --- settle gate -------------------------------------------------------------------------------


async def test_unsettled_window_aborts_without_force(db, shop, panel):
    baseline = await _run(db, shop.id, panel.id, started_at=PUBLISHED_AT - timedelta(days=7))
    post = await _run(db, shop.id, panel.id, started_at=PUBLISHED_AT + timedelta(hours=1))
    await _aggregate(db, baseline, our_rate=0.0)
    await _aggregate(db, post, our_rate=0.25)

    with pytest.raises(VerificationAborted, match="required"):
        await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED)

    assert await _rows(db, post.id) == []


async def test_force_records_the_row_labelled_unsettled(db, shop, panel):
    baseline = await _run(db, shop.id, panel.id, started_at=PUBLISHED_AT - timedelta(days=7))
    post = await _run(db, shop.id, panel.id, started_at=PUBLISHED_AT + timedelta(hours=1))
    await _aggregate(db, baseline, our_rate=0.0)
    await _aggregate(db, post, our_rate=0.25)

    report = await run_verifier(
        db, post.id, baseline_run_id=baseline.id, measured_fixes=MEASURED, force=True
    )

    assert report.settle_satisfied is False
    assert report.settle_hours == pytest.approx(1.0)
    (row,) = await _rows(db, post.id)
    # force is a LABEL, not a bypass: the delta is real but permanently marked unsettled.
    assert row.settle_satisfied is False
    assert row.delta == 0.25
    assert row.settle_hours == pytest.approx(1.0)


# --- the sacred column -------------------------------------------------------------------------


async def test_verification_never_writes_to_fixes(db, shop, panel):
    """``fixes.published_at`` is the anchor the whole measurement hangs off; the Publisher is its
    only writer. Snapshot every fixes row and assert byte-identity across a verification."""
    product = Product(
        shop_id=shop.id, shopify_product_id="gid://shopify/Product/114", title="Colombia Huila"
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)

    db.add(
        Fix(
            product_id=product.id,
            type=FixType.description,
            status=FixStatus.verified,
            target="body_html",
            published_at=PUBLISHED_AT,
        )
    )
    await db.commit()

    baseline, post = await _pair(db, shop, panel)
    await _aggregate(db, baseline, our_rate=0.0)
    await _aggregate(db, post, our_rate=0.25)

    measured = await resolve_measured_set(db, shop.id)
    snapshot = (
        await db.execute(
            select(
                Fix.id, Fix.status, Fix.published_at, Fix.publish_error, Fix.after_json, Fix.reason
            ).order_by(Fix.id)
        )
    ).all()

    await run_verifier(db, post.id, baseline_run_id=baseline.id, measured_fixes=measured)

    after = (
        await db.execute(
            select(
                Fix.id, Fix.status, Fix.published_at, Fix.publish_error, Fix.after_json, Fix.reason
            ).order_by(Fix.id)
        )
    ).all()
    assert after == snapshot
