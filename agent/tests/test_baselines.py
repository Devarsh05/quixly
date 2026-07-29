"""Baseline selection + measured-set snapshot (Phase 4 step 1).

DB-backed against the transaction-scoped ``db`` fixture; no LLM, no engine, no Shopify.

**Every exclusion is SEEDED, not assumed.** A selection query with nothing to exclude is
unfalsifiable — it passes identically with and without its WHERE clause. That trap has been hit
twice in this repo, so each test here plants the row it must skip:

* a ``completed`` run with ZERO ``share_of_model`` rows (the live dev store has exactly one —
  run 623 — and "latest completed run" would pick a publish run without this);
* a run on a different panel, and one that completed too late;
* every non-``verified`` fix status, seeded individually;
* a second shop's verified+published fix;
* a fix published BEFORE the baseline completed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

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
)
from app.models import QueryPanel as QueryPanelRow
from app.services.baselines import MeasuredFix, resolve_measured_set, select_baseline_run

PUBLISHED_AT = datetime(2026, 7, 28, 14, 51, tzinfo=UTC)


def _published(*timestamps: datetime) -> list[MeasuredFix]:
    """A published-set snapshot for baseline selection; only ``published_at`` is read."""
    return [
        MeasuredFix(
            fix_id=i + 1,
            product_id=1,
            type=FixType.description,
            target="body_html",
            published_at=ts,
        )
        for i, ts in enumerate(timestamps)
    ]


async def _shop(db, domain: str) -> Shop:
    shop = Shop(shop_domain=domain, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


@pytest.fixture
async def shop(db):
    return await _shop(db, "baselines-test.myshopify.com")


async def _panel(db, shop_id: int, fingerprint: str) -> QueryPanelRow:
    panel = QueryPanelRow(
        shop_id=shop_id,
        category="coffee",
        queries_json=[{"text": "q0"}],
        fingerprint=fingerprint,
    )
    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    return panel


async def _run(
    db,
    shop_id: int,
    panel_id: int,
    *,
    completed_at: datetime | None,
    status: str = AgentRunStatus.completed,
    with_aggregate: bool = True,
) -> AgentRun:
    """A run, optionally with the share_of_model row that makes it look like a real SCAN."""
    run = AgentRun(
        shop_id=shop_id,
        panel_id=panel_id,
        status=status,
        completed_at=completed_at,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    if with_aggregate:
        db.add(
            ShareOfModel(
                run_id=run.id,
                shop_id=shop_id,
                engine="perplexity",
                period="2026-07-21",
                our_rate=0.0,
                our_mentions=0,
                total_queries=24,
                competitor_rates_json={},
            )
        )
        await db.commit()
    return run


async def _product(db, shop_id: int, shopify_id: str) -> Product:
    product = Product(shop_id=shop_id, shopify_product_id=shopify_id, title="Colombia Huila")
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


async def _fix(
    db,
    product_id: int,
    *,
    status: str,
    published_at: datetime | None,
    fix_type: str = FixType.description,
    target: str = "body_html",
) -> Fix:
    fix = Fix(
        product_id=product_id,
        type=fix_type,
        status=status,
        target=target,
        published_at=published_at,
    )
    db.add(fix)
    await db.commit()
    await db.refresh(fix)
    return fix


# --- measured set: exclusions, each seeded -----------------------------------------------------


async def test_measured_set_takes_only_verified_published_fixes(db, shop):
    product = await _product(db, shop.id, "gid://shopify/Product/1")

    wanted = await _fix(db, product.id, status=FixStatus.verified, published_at=PUBLISHED_AT)

    # Every excluded status, seeded individually — the point of the test.
    for status in (
        FixStatus.proposed,
        FixStatus.approved,
        FixStatus.published,  # intermediate: written but NOT re-read-confirmed
        FixStatus.publish_failed,
        FixStatus.stale,
        FixStatus.rejected,
        FixStatus.reverted,
    ):
        await _fix(db, product.id, status=status, published_at=PUBLISHED_AT)

    # ...and a ``verified`` row that never got a published_at stamp.
    await _fix(db, product.id, status=FixStatus.verified, published_at=None)

    measured = await resolve_measured_set(db, shop.id)

    assert [fix.fix_id for fix in measured] == [wanted.id]
    assert measured[0].type == FixType.description
    assert measured[0].target == "body_html"
    assert measured[0].published_at == PUBLISHED_AT


async def test_measured_set_is_scoped_to_one_shop(db, shop):
    other = await _shop(db, "other-shop.myshopify.com")
    mine = await _product(db, shop.id, "gid://shopify/Product/1")
    theirs = await _product(db, other.id, "gid://shopify/Product/2")

    wanted = await _fix(db, mine.id, status=FixStatus.verified, published_at=PUBLISHED_AT)
    await _fix(db, theirs.id, status=FixStatus.verified, published_at=PUBLISHED_AT)

    assert [fix.fix_id for fix in await resolve_measured_set(db, shop.id)] == [wanted.id]


async def test_measured_set_excludes_fixes_published_before_the_baseline(db, shop):
    product = await _product(db, shop.id, "gid://shopify/Product/1")
    # Already baked INTO the baseline — counting it would credit the window with a change it
    # never saw.
    await _fix(
        db, product.id, status=FixStatus.verified, published_at=PUBLISHED_AT - timedelta(days=3)
    )
    wanted = await _fix(db, product.id, status=FixStatus.verified, published_at=PUBLISHED_AT)

    measured = await resolve_measured_set(db, shop.id, after=PUBLISHED_AT - timedelta(days=1))

    assert [fix.fix_id for fix in measured] == [wanted.id]


# --- staggered publishes: the baseline must predate the ENTIRE measured set --------------------


async def test_baseline_predates_every_published_fix_not_just_the_last(db, shop):
    """With staggered publishes, a baseline sitting BETWEEN two publishes understates uplift.

    It bakes the earlier fix into the pre-rate AND drops it from the measured set, so the very
    change being measured is counted as part of the "before". The baseline must be the most recent
    scan that predates the whole set, not the most recent scan that predates the LAST publish.

    Every previous test seeds a single publish timestamp, where that distinction is invisible —
    which is exactly why this branch was never exercised.
    """
    panel = await _panel(db, shop.id, "fp-staggered")
    product = await _product(db, shop.id, "gid://shopify/Product/1")

    t0 = PUBLISHED_AT
    scan = await _run(db, shop.id, panel.id, completed_at=t0)
    # The run current logic wrongly prefers: a real scan, but it sits AFTER F1.
    between = await _run(db, shop.id, panel.id, completed_at=t0 + timedelta(days=1, hours=12))

    first = await _fix(
        db, product.id, status=FixStatus.verified, published_at=t0 + timedelta(days=1)
    )
    second = await _fix(
        db, product.id, status=FixStatus.verified, published_at=t0 + timedelta(days=2)
    )

    published = await resolve_measured_set(db, shop.id)
    baseline = await select_baseline_run(db, shop.id, published=published)

    assert baseline is not None
    assert baseline.id == scan.id, (
        f"picked run {baseline.id}; the between-publishes run is {between.id}"
    )

    # ...and with that baseline, BOTH publishes are inside the measured window.
    measured = [fix for fix in published if fix.published_at > baseline.completed_at]
    assert {fix.fix_id for fix in measured} == {first.id, second.id}


async def test_baseline_falls_back_when_no_scan_predates_the_first_publish(db, shop):
    """Tier two: install → publish → first scan later.

    Nothing predates the ancient publish, so the primary anchor finds no baseline. Rather than
    leaving the shop permanently unmeasurable, selection falls back to the latest scan predating
    the LAST publish. The ancient fix is then correctly outside the window — nothing exists to
    baseline it against — and ``resolve_measured_set``'s ``after`` filter drops it from M.
    """
    panel = await _panel(db, shop.id, "fp-fallback")
    product = await _product(db, shop.id, "gid://shopify/Product/1")

    t0 = PUBLISHED_AT
    ancient = await _fix(db, product.id, status=FixStatus.verified, published_at=t0)
    scan = await _run(db, shop.id, panel.id, completed_at=t0 + timedelta(days=1))
    recent = await _fix(
        db, product.id, status=FixStatus.verified, published_at=t0 + timedelta(days=2)
    )
    # Seeded so the fallback's own upper bound is falsifiable: a scan AFTER the last publish is
    # still not a pre-state and must never be chosen.
    await _run(db, shop.id, panel.id, completed_at=t0 + timedelta(days=3))

    published = await resolve_measured_set(db, shop.id)
    assert {fix.fix_id for fix in published} == {ancient.id, recent.id}

    baseline = await select_baseline_run(db, shop.id, published=published)

    assert baseline is not None
    assert baseline.id == scan.id

    # M is a SUBSET here, by design — and the ancient fix is the one dropped.
    measured = await resolve_measured_set(db, shop.id, after=baseline.completed_at)
    assert [fix.fix_id for fix in measured] == [recent.id]


# --- baseline selection: "completed" and panel_id do NOT identify a scan ------------------------


async def test_baseline_skips_completed_run_with_no_share_of_model(db, shop):
    """The live-DB shape: run 623 is completed with zero aggregates, and the fix/publish runs
    borrow the same panel. Only what a run PRODUCED identifies it as a scan."""
    panel = await _panel(db, shop.id, "fp-baseline")

    real_scan = await _run(db, shop.id, panel.id, completed_at=PUBLISHED_AT - timedelta(days=7))
    # Later, completed, same panel — but produced nothing. A publish/fix run.
    await _run(
        db,
        shop.id,
        panel.id,
        completed_at=PUBLISHED_AT - timedelta(hours=1),
        with_aggregate=False,
    )

    chosen = await select_baseline_run(db, shop.id, published=_published(PUBLISHED_AT))

    assert chosen is not None
    assert chosen.id == real_scan.id


async def test_baseline_skips_runs_completing_after_the_first_publish(db, shop):
    panel = await _panel(db, shop.id, "fp-baseline")
    early = await _run(db, shop.id, panel.id, completed_at=PUBLISHED_AT - timedelta(days=7))
    # A scan that ran AFTER the publish cannot be a "before" measurement.
    await _run(db, shop.id, panel.id, completed_at=PUBLISHED_AT + timedelta(hours=1))

    chosen = await select_baseline_run(db, shop.id, published=_published(PUBLISHED_AT))

    assert chosen is not None
    assert chosen.id == early.id


async def test_baseline_skips_still_running_and_failed_runs(db, shop):
    panel = await _panel(db, shop.id, "fp-baseline")
    good = await _run(db, shop.id, panel.id, completed_at=PUBLISHED_AT - timedelta(days=7))
    await _run(
        db,
        shop.id,
        panel.id,
        completed_at=PUBLISHED_AT - timedelta(days=1),
        status=AgentRunStatus.failed,
    )
    await _run(db, shop.id, panel.id, completed_at=None, status=AgentRunStatus.running)

    chosen = await select_baseline_run(db, shop.id, published=_published(PUBLISHED_AT))

    assert chosen is not None
    assert chosen.id == good.id


async def test_baseline_picks_the_latest_eligible_run(db, shop):
    panel = await _panel(db, shop.id, "fp-baseline")
    await _run(db, shop.id, panel.id, completed_at=PUBLISHED_AT - timedelta(days=8))
    latest = await _run(db, shop.id, panel.id, completed_at=PUBLISHED_AT - timedelta(days=7))

    chosen = await select_baseline_run(db, shop.id, published=_published(PUBLISHED_AT))

    assert chosen is not None
    assert chosen.id == latest.id


async def test_baseline_is_scoped_to_one_shop(db, shop):
    other = await _shop(db, "other-baseline.myshopify.com")
    other_panel = await _panel(db, other.id, "fp-other")
    await _run(db, other.id, other_panel.id, completed_at=PUBLISHED_AT - timedelta(days=1))

    assert await select_baseline_run(db, shop.id, published=_published(PUBLISHED_AT)) is None


async def test_explicit_baseline_id_is_validated_not_trusted(db, shop):
    """An override must go through the same predicates — it is not a way around them."""
    panel = await _panel(db, shop.id, "fp-baseline")
    good = await _run(db, shop.id, panel.id, completed_at=PUBLISHED_AT - timedelta(days=7))
    no_aggregate = await _run(
        db,
        shop.id,
        panel.id,
        completed_at=PUBLISHED_AT - timedelta(days=2),
        with_aggregate=False,
    )

    assert (
        await select_baseline_run(
            db, shop.id, published=_published(PUBLISHED_AT), baseline_run_id=good.id
        )
    ).id == good.id
    assert (
        await select_baseline_run(
            db, shop.id, published=_published(PUBLISHED_AT), baseline_run_id=no_aggregate.id
        )
    ) is None


# --- the sacred column -------------------------------------------------------------------------


async def test_resolving_the_measured_set_writes_nothing_to_fixes(db, shop):
    product = await _product(db, shop.id, "gid://shopify/Product/1")
    await _fix(db, product.id, status=FixStatus.verified, published_at=PUBLISHED_AT)

    before = (await db.execute(select(Fix.id, Fix.status, Fix.published_at))).all()
    await resolve_measured_set(db, shop.id)
    after = (await db.execute(select(Fix.id, Fix.status, Fix.published_at))).all()

    assert before == after
