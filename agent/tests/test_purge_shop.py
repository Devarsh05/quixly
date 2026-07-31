"""``shop/redact`` erasure — the cascade, the reinstall guard, and the job wrapper.

The point of the cascade test is that it asserts **each of the nine tables individually**. The
purge is a single ``DELETE FROM shops``, which is only complete because every shop-scoped table
hangs off ``shops`` by ``ON DELETE CASCADE`` — an argument about the schema, not a demonstration.
Counting rows per table is what turns it into evidence, and it is what fails if a future table is
added without a CASCADE path back to ``shops``.

A **second shop is seeded with a full set of its own rows** in every case. A purge that erased the
right shop's data by erasing everyone's would pass a single-tenant test; it cannot pass this one.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.jobs import purge_shop as purge_job
from app.jobs.purge_shop import purge_shop
from app.models import (
    AgentRun,
    AgentRunStatus,
    Audit,
    EngineRun,
    Fix,
    FixStatus,
    FixType,
    IngestRun,
    IngestStatus,
    Product,
    QueryPanel,
    ShareOfModel,
    Shop,
    ShopStatus,
    Verification,
)
from app.redis import ingest_lock_key
from app.services.purge import PurgeOutcome, purge_shop_data
from app.services.token_provider import TokenProvider

REDACTED = "redacted-shop.myshopify.com"
NEIGHBOUR = "neighbour-shop.myshopify.com"


class SeededShop:
    """Every id written for one shop, so each table can be asserted on separately."""

    def __init__(self, shop_id: int) -> None:
        self.shop_id = shop_id
        self.product_id: int
        self.ingest_run_id: int
        self.panel_id: int
        self.engine_run_id: int
        self.agent_run_id: int
        self.share_id: int
        self.audit_id: int
        self.fix_id: int
        self.verification_id: int


async def seed_shop(db, shop_domain: str, *, status: ShopStatus) -> SeededShop:
    """One row in all nine shop-scoped tables, covering every branch of the cascade tree."""
    shop = Shop(shop_domain=shop_domain, status=status)
    db.add(shop)
    await db.flush()
    seeded = SeededShop(shop.id)

    product = Product(
        shop_id=shop.id,
        shopify_product_id=f"gid://shopify/Product/{shop.id}",
        title="Ethiopia Yirgacheffe",
        body="<p>Washed, floral.</p>",
    )
    ingest_run = IngestRun(shop_id=shop.id, status=IngestStatus.complete)
    panel = QueryPanel(
        shop_id=shop.id,
        category="coffee",
        queries_json=["best light roast coffee"],
        fingerprint=f"purge-{shop.id}",
    )
    db.add_all([product, ingest_run, panel])
    await db.flush()
    seeded.product_id = product.id
    seeded.ingest_run_id = ingest_run.id
    seeded.panel_id = panel.id

    agent_run = AgentRun(shop_id=shop.id, panel_id=panel.id, status=AgentRunStatus.completed)
    engine_run = EngineRun(
        panel_id=panel.id,
        engine="perplexity",
        query="best light roast coffee",
        response_raw={"choices": []},
    )
    audit = Audit(product_id=product.id, gaps_json=[], severity="low")
    fix = Fix(
        product_id=product.id,
        type=FixType.description,
        status=FixStatus.proposed,
        target="body_html",
    )
    db.add_all([agent_run, engine_run, audit, fix])
    await db.flush()
    seeded.agent_run_id = agent_run.id
    seeded.engine_run_id = engine_run.id
    seeded.audit_id = audit.id
    seeded.fix_id = fix.id

    share = ShareOfModel(
        run_id=agent_run.id,
        shop_id=shop.id,
        engine="perplexity",
        period="2026-07-31",
        our_rate=0.25,
        our_mentions=1,
        total_queries=4,
        competitor_rates_json={},
    )
    verification = Verification(
        run_id=agent_run.id,
        shop_id=shop.id,
        engine="perplexity",
        panel_fingerprint=f"purge-{shop.id}",
        panel_id=panel.id,
        pre_rate=0.1,
        post_rate=0.25,
        delta=0.15,
        competitor_deltas_json={},
        measured_fixes_json=[],
        measured_fix_count=0,
        published_at_max=datetime(2026, 7, 1, tzinfo=UTC),
        settle_hours=200.0,
        settle_satisfied=True,
    )
    db.add_all([share, verification])
    await db.flush()
    seeded.share_id = share.id
    seeded.verification_id = verification.id

    await db.commit()
    return seeded


async def _count(db, model, column, value) -> int:
    return (
        await db.execute(select(func.count()).select_from(model).where(column == value))
    ).scalar_one()


async def assert_rows_present(db, seeded: SeededShop) -> None:
    """Every one of the nine tables still holds this shop's row."""
    assert await _count(db, Shop, Shop.id, seeded.shop_id) == 1
    assert await _count(db, Product, Product.id, seeded.product_id) == 1
    assert await _count(db, IngestRun, IngestRun.id, seeded.ingest_run_id) == 1
    assert await _count(db, QueryPanel, QueryPanel.id, seeded.panel_id) == 1
    assert await _count(db, EngineRun, EngineRun.id, seeded.engine_run_id) == 1
    assert await _count(db, AgentRun, AgentRun.id, seeded.agent_run_id) == 1
    assert await _count(db, ShareOfModel, ShareOfModel.id, seeded.share_id) == 1
    assert await _count(db, Audit, Audit.id, seeded.audit_id) == 1
    assert await _count(db, Fix, Fix.id, seeded.fix_id) == 1
    assert await _count(db, Verification, Verification.id, seeded.verification_id) == 1


async def assert_rows_gone(db, seeded: SeededShop) -> None:
    """Every one of the nine tables is empty of this shop, asserted table by table.

    Deliberately not a single "the shop row is gone" check: that would pass while orphaned
    products, engine_runs or verifications survived, which is exactly the failure the CASCADE
    chain is supposed to make impossible.
    """
    assert await _count(db, Shop, Shop.id, seeded.shop_id) == 0
    assert await _count(db, Product, Product.id, seeded.product_id) == 0
    assert await _count(db, IngestRun, IngestRun.id, seeded.ingest_run_id) == 0
    assert await _count(db, QueryPanel, QueryPanel.id, seeded.panel_id) == 0
    assert await _count(db, EngineRun, EngineRun.id, seeded.engine_run_id) == 0
    assert await _count(db, AgentRun, AgentRun.id, seeded.agent_run_id) == 0
    assert await _count(db, ShareOfModel, ShareOfModel.id, seeded.share_id) == 0
    assert await _count(db, Audit, Audit.id, seeded.audit_id) == 0
    assert await _count(db, Fix, Fix.id, seeded.fix_id) == 0
    assert await _count(db, Verification, Verification.id, seeded.verification_id) == 0


# --- the cascade ------------------------------------------------------------------------


async def test_purge_erases_every_shop_scoped_table(db):
    redacted = await seed_shop(db, REDACTED, status=ShopStatus.uninstalled)
    await seed_shop(db, NEIGHBOUR, status=ShopStatus.uninstalled)

    report = await purge_shop_data(db, REDACTED)

    assert report.outcome is PurgeOutcome.purged
    assert report.shop_id == redacted.shop_id
    await assert_rows_gone(db, redacted)


async def test_purge_leaves_other_shops_untouched(db):
    """Tenant isolation: erasing one shop must not reach a single row of another's."""
    await seed_shop(db, REDACTED, status=ShopStatus.uninstalled)
    neighbour = await seed_shop(db, NEIGHBOUR, status=ShopStatus.uninstalled)

    await purge_shop_data(db, REDACTED)

    await assert_rows_present(db, neighbour)


async def test_purge_is_idempotent(db):
    """Shopify redelivers shop/redact; the second delivery must be a clean no-op, not an error."""
    await seed_shop(db, REDACTED, status=ShopStatus.uninstalled)

    first = await purge_shop_data(db, REDACTED)
    second = await purge_shop_data(db, REDACTED)

    assert first.outcome is PurgeOutcome.purged
    assert second.outcome is PurgeOutcome.unknown_shop


async def test_purge_of_unknown_shop_is_not_an_error(db):
    report = await purge_shop_data(db, "never-installed.myshopify.com")

    assert report.outcome is PurgeOutcome.unknown_shop
    assert report.shop_id is None


# --- the reinstall guard ----------------------------------------------------------------


@pytest.mark.parametrize("status", [ShopStatus.uninstalled, ShopStatus.reauth_required])
async def test_purge_proceeds_for_a_shop_that_is_not_active(db, status):
    seeded = await seed_shop(db, REDACTED, status=status)

    report = await purge_shop_data(db, REDACTED)

    assert report.outcome is PurgeOutcome.purged
    await assert_rows_gone(db, seeded)


async def test_active_shop_is_skipped_and_keeps_all_its_data(db):
    """A reinstall inside the 48h window: the store is live, so nothing is deleted.

    The uplift history is the irreplaceable part — a baseline scan cannot be recreated after the
    fact, so a wrong purge here is unrecoverable, not merely inconvenient.
    """
    seeded = await seed_shop(db, REDACTED, status=ShopStatus.active)

    report = await purge_shop_data(db, REDACTED)

    assert report.outcome is PurgeOutcome.skipped_reinstalled
    assert report.shop_id == seeded.shop_id
    await assert_rows_present(db, seeded)


# --- the job wrapper: Redis-side shop state ---------------------------------------------


@pytest.fixture
def session_local(db, monkeypatch):
    """Make the job use the test's transaction-scoped session instead of a new one."""

    @asynccontextmanager
    async def fake_session_local():
        yield db

    monkeypatch.setattr(purge_job, "SessionLocal", fake_session_local)
    return db


def _ctx() -> dict:
    # The worker's shared provider (app/worker.py). Building one here instead would be a second
    # token path — the job must take the one it is handed.
    return {"token_provider": TokenProvider()}


async def _seed_redis(fake_redis, shop_domain: str) -> str:
    from app.services.token_provider import _cache_key

    key = _cache_key(shop_domain)
    await fake_redis.set(key, "shpua_cached_token")
    await fake_redis.set(ingest_lock_key(shop_domain), "42")
    return key


async def test_job_clears_the_token_cache_and_ingest_lock(session_local, fake_redis):
    db = session_local
    await seed_shop(db, REDACTED, status=ShopStatus.uninstalled)
    cache_key = await _seed_redis(fake_redis, REDACTED)

    report = await purge_shop(_ctx(), REDACTED)

    assert report.outcome is PurgeOutcome.purged
    # A cached admin token is shop data too, and it outlives the row it belonged to.
    assert await fake_redis.get(cache_key) is None
    assert await fake_redis.get(ingest_lock_key(REDACTED)) is None


async def test_job_leaves_redis_alone_when_the_shop_reinstalled(session_local, fake_redis):
    """A skipped shop is LIVE. Dropping its cached token or ingest lock would degrade it."""
    db = session_local
    await seed_shop(db, REDACTED, status=ShopStatus.active)
    cache_key = await _seed_redis(fake_redis, REDACTED)

    report = await purge_shop(_ctx(), REDACTED)

    assert report.outcome is PurgeOutcome.skipped_reinstalled
    assert await fake_redis.get(cache_key) == "shpua_cached_token"
    assert await fake_redis.get(ingest_lock_key(REDACTED)) == "42"
