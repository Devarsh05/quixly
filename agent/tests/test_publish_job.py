"""The publish job (``jobs.publish``) — run lifecycle and failure classification.

The node's behaviour is covered in ``test_publisher``. What is tested HERE is the job wrapper,
whose whole job is to classify failures correctly and never leave a run stuck ``running``:

* ``TokenUnavailableError`` is PERMANENT → the shop is flagged ``reauth_required``. Conflating it
  with a transient error would retry a dead shop forever;
* ``TokenFetchError`` (the app shell restarting) is TRANSIENT → the shop is NOT flagged, so a blip
  never brands a healthy merchant;
* ``PublishAborted`` (invariant breach / rollout guard) fails the run having written nothing;
* every path commits a terminal ``agent_runs.status``.

``SessionLocal`` is patched to the transaction-scoped ``db`` fixture so the job runs its real code
path — including its rollback-then-commit error handling — without leaving rows behind.
"""

from contextlib import asynccontextmanager

import pytest

from app.jobs import publish as publish_job
from app.jobs.publish import run_publish_task
from app.models import (
    AgentRun,
    AgentRunStatus,
    Fix,
    FixStatus,
    FixType,
    Product,
    Shop,
    ShopStatus,
)
from app.models import QueryPanel as QueryPanelRow
from app.services.catalog import product_row_from_node, stable_source_hash
from app.services.token_provider import TokenFetchError, TokenUnavailableError
from tests.test_publisher import (
    BODY_AFTER,
    BODY_BEFORE,
    FakeShopify,
    make_node,
)

SHOP = "publish-job-test.myshopify.com"


@pytest.fixture
def session_local(db, monkeypatch):
    """Make the job use the test's transaction-scoped session instead of a new one."""

    @asynccontextmanager
    async def fake_session_local():
        yield db

    monkeypatch.setattr(publish_job, "SessionLocal", fake_session_local)
    return db


async def seed_run(db) -> tuple[Shop, AgentRun, Fix]:
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.flush()

    panel = QueryPanelRow(
        shop_id=shop.id, category="coffee", queries_json=["q"], fingerprint="publish-job-test"
    )
    db.add(panel)
    await db.flush()

    run = AgentRun(shop_id=shop.id, panel_id=panel.id, status=AgentRunStatus.running)
    db.add(run)

    node = make_node()
    product = Product(shop_id=shop.id, visibility_state="active", **product_row_from_node(node))
    db.add(product)
    await db.flush()

    fix = Fix(
        product_id=product.id,
        type=FixType.description,
        status=FixStatus.approved,
        target="body_html",
        before_json={"body_html": BODY_BEFORE},
        after_json={"body_html": BODY_AFTER},
        base_source_hash=stable_source_hash(product_row_from_node(node)),
    )
    db.add(fix)
    await db.commit()
    return shop, run, fix


def patch_client(monkeypatch, client):
    monkeypatch.setattr(publish_job, "ShopifyAdminClient", lambda *a, **k: client)


def allow(monkeypatch, domains: str = SHOP):
    monkeypatch.setattr(
        publish_job, "get_settings", lambda: type("S", (), {"publish_allowed_shops": domains})()
    )


async def test_a_successful_run_completes_and_reports(session_local, monkeypatch):
    db = session_local
    shop, run, fix = await seed_run(db)
    node = make_node()
    patch_client(monkeypatch, FakeShopify(node))
    allow(monkeypatch)

    report = await run_publish_task({"token_provider": object()}, run.id)

    assert report.verified == 1
    await db.refresh(run)
    assert run.status == AgentRunStatus.completed
    await db.refresh(fix)
    assert fix.status == FixStatus.verified


async def test_a_dead_refresh_chain_flags_the_shop_and_fails_the_run(session_local, monkeypatch):
    """PERMANENT. Retrying would never succeed, so it must surface as reauth_required."""
    db = session_local
    shop, run, _ = await seed_run(db)

    class Dead(FakeShopify):
        async def fetch_product(self, product_gid):
            raise TokenUnavailableError("no session")

    patch_client(monkeypatch, Dead(make_node()))
    allow(monkeypatch)

    with pytest.raises(TokenUnavailableError):
        await run_publish_task({"token_provider": object()}, run.id)

    await db.refresh(shop)
    await db.refresh(run)
    assert shop.status == ShopStatus.reauth_required
    assert run.status == AgentRunStatus.failed


async def test_an_unreachable_app_shell_does_NOT_flag_the_shop(session_local, monkeypatch):
    """TRANSIENT. A restarting app shell must never brand a healthy merchant for re-auth."""
    db = session_local
    shop, run, _ = await seed_run(db)

    class Blip(FakeShopify):
        async def fetch_product(self, product_gid):
            raise TokenFetchError("app shell unreachable")

    patch_client(monkeypatch, Blip(make_node()))
    allow(monkeypatch)

    with pytest.raises(TokenFetchError):
        await run_publish_task({"token_provider": object()}, run.id)

    await db.refresh(shop)
    await db.refresh(run)
    assert shop.status == ShopStatus.active  # NOT reauth_required
    assert run.status == AgentRunStatus.failed


async def test_the_rollout_guard_fails_the_run_without_writing(session_local, monkeypatch):
    db = session_local
    shop, run, fix = await seed_run(db)
    client = FakeShopify(make_node())
    patch_client(monkeypatch, client)
    allow(monkeypatch, "somewhere-else.myshopify.com")

    with pytest.raises(publish_job.PublishAborted):
        await run_publish_task({"token_provider": object()}, run.id)

    assert client.mutations == []
    await db.refresh(run)
    await db.refresh(fix)
    assert run.status == AgentRunStatus.failed
    assert fix.status == FixStatus.approved  # untouched, still publishable later


async def test_the_job_uses_the_workers_shared_token_provider(session_local, monkeypatch):
    """A job that built its own provider would be a second refresh authority."""
    db = session_local
    shop, run, _ = await seed_run(db)
    sentinel = object()
    captured = {}

    def capture(shop_domain, token_provider):
        captured["domain"] = shop_domain
        captured["provider"] = token_provider
        return FakeShopify(make_node())

    monkeypatch.setattr(publish_job, "ShopifyAdminClient", capture)
    allow(monkeypatch)

    await run_publish_task({"token_provider": sentinel}, run.id)

    assert captured["provider"] is sentinel
    assert captured["domain"] == SHOP
