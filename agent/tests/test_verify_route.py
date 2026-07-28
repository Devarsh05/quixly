"""POST /verify + GET /verification (Phase 4 step 1).

Driven through httpx.ASGITransport (see test_shops_connect for why, not TestClient). The Arq
enqueue is captured, not executed — no real queue and no task run here.

The route is where a measurement is REFUSED, before a full panel fan-out is spent, and where the
measured set is snapshotted (Gate 1). Both are asserted here: every 409 path, and the exact
payload handed to the queue.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.api import verify as verify_api
from app.db import get_db
from app.graph.interrogator import build_query_panel
from app.main import app
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
from app.services.panels import upsert_panel
from tests.conftest import TEST_API_KEY

SHOP = "verify-route-test.myshopify.com"
HEADERS = {"X-Internal-Api-Key": TEST_API_KEY}

SETTLED_PUBLISH = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)  # weeks old: settle window met


@pytest.fixture
def enqueued(monkeypatch) -> list[tuple]:
    """Capture the enqueue payload instead of hitting a real Arq queue."""
    calls: list[tuple] = []

    async def fake_enqueue(run_id, baseline_run_id, measured_fixes, force):
        calls.append((run_id, baseline_run_id, measured_fixes, force))

    monkeypatch.setattr(verify_api, "_enqueue", fake_enqueue)
    return calls


@pytest.fixture
async def client(db):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
    app.dependency_overrides.clear()


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


@pytest.fixture
async def panel_id(db, shop):
    panel_id = await upsert_panel(db, build_query_panel(), shop.id)
    await db.commit()
    return panel_id


async def _baseline(db, shop_id: int, panel_id: int, *, completed_at: datetime) -> AgentRun:
    run = AgentRun(
        shop_id=shop_id,
        panel_id=panel_id,
        status=AgentRunStatus.completed,
        started_at=completed_at,
        completed_at=completed_at,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    db.add(
        ShareOfModel(
            run_id=run.id, shop_id=shop_id, engine="perplexity", period="2026-06-01",
            our_rate=0.0, our_mentions=0, total_queries=24, competitor_rates_json={},
        )
    )
    await db.commit()
    return run


async def _published_fix(db, shop_id: int, *, published_at: datetime, status=FixStatus.verified):
    product = Product(
        shop_id=shop_id,
        shopify_product_id=f"gid://shopify/Product/{int(published_at.timestamp())}",
        title="Colombia Huila",
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)

    fix = Fix(
        product_id=product.id,
        type=FixType.description,
        status=status,
        target="body_html",
        published_at=published_at,
    )
    db.add(fix)
    await db.commit()
    await db.refresh(fix)
    return fix


# --- auth + 404 --------------------------------------------------------------------------------


async def test_requires_the_internal_key(client, shop):
    assert (await client.post(f"/shops/by-domain/{SHOP}/verify")).status_code == 401


async def test_unknown_shop_is_404(client):
    response = await client.post("/shops/by-domain/nope.myshopify.com/verify", headers=HEADERS)
    assert response.status_code == 404


# --- the refusal paths, each before any engine spend -------------------------------------------


async def test_nothing_published_is_409(client, db, shop, panel_id, enqueued):
    await _baseline(db, shop.id, panel_id, completed_at=SETTLED_PUBLISH - timedelta(days=7))
    # Seeded so the "verified only" filter is falsifiable: a published-but-unconfirmed row.
    await _published_fix(
        db, shop.id, published_at=SETTLED_PUBLISH, status=FixStatus.published
    )

    response = await client.post(f"/shops/by-domain/{SHOP}/verify", headers=HEADERS)

    assert response.status_code == 409
    assert "nothing to measure" in response.json()["detail"]
    assert enqueued == []


async def test_no_usable_baseline_is_409(client, db, shop, panel_id, enqueued):
    await _published_fix(db, shop.id, published_at=SETTLED_PUBLISH)
    # A completed run that produced NO share_of_model rows — a publish/fix run, not a scan.
    run = AgentRun(
        shop_id=shop.id,
        panel_id=panel_id,
        status=AgentRunStatus.completed,
        completed_at=SETTLED_PUBLISH - timedelta(days=1),
    )
    db.add(run)
    await db.commit()

    response = await client.post(f"/shops/by-domain/{SHOP}/verify", headers=HEADERS)

    assert response.status_code == 409
    assert "baseline" in response.json()["detail"]
    assert enqueued == []


async def test_unsettled_window_is_409_without_force(client, db, shop, panel_id, enqueued):
    recent = datetime.now(UTC) - timedelta(hours=2)
    await _baseline(db, shop.id, panel_id, completed_at=recent - timedelta(days=7))
    await _published_fix(db, shop.id, published_at=recent)

    response = await client.post(f"/shops/by-domain/{SHOP}/verify", headers=HEADERS)

    assert response.status_code == 409
    assert "force=true" in response.json()["detail"]
    assert enqueued == []


# --- the happy path + the snapshot -------------------------------------------------------------


async def test_verify_returns_202_and_binds_to_the_baseline_panel(
    client, db, shop, panel_id, enqueued
):
    baseline = await _baseline(
        db, shop.id, panel_id, completed_at=SETTLED_PUBLISH - timedelta(days=7)
    )
    fix = await _published_fix(db, shop.id, published_at=SETTLED_PUBLISH)

    response = await client.post(f"/shops/by-domain/{SHOP}/verify", headers=HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["baseline_run_id"] == baseline.id
    assert body["measured_fix_count"] == 1
    assert body["settle_satisfied"] is True
    assert body["status"] == "running"

    # BIND, don't rebuild: the post run must carry the BASELINE's panel_id.
    run = await db.get(AgentRun, body["run_id"])
    assert run.panel_id == baseline.panel_id

    # Gate 1: the snapshot is taken HERE and handed to the queue verbatim.
    (run_id, baseline_run_id, measured, force) = enqueued[0]
    assert run_id == body["run_id"]
    assert baseline_run_id == baseline.id
    assert force is False
    assert measured == [
        {
            "fix_id": fix.id,
            "product_id": fix.product_id,
            "type": FixType.description.value,
            "target": "body_html",
            # pydantic's JSON mode renders UTC as "Z", not "+00:00".
            "published_at": "2026-07-01T12:00:00Z",
        }
    ]


async def test_force_enqueues_an_unsettled_run(client, db, shop, panel_id, enqueued):
    recent = datetime.now(UTC) - timedelta(hours=2)
    await _baseline(db, shop.id, panel_id, completed_at=recent - timedelta(days=7))
    await _published_fix(db, shop.id, published_at=recent)

    response = await client.post(f"/shops/by-domain/{SHOP}/verify?force=true", headers=HEADERS)

    assert response.status_code == 202
    assert response.json()["settle_satisfied"] is False
    assert enqueued[0][3] is True


async def test_fixes_published_before_the_baseline_are_excluded(
    client, db, shop, panel_id, enqueued
):
    """A fix already baked into the baseline is not a change the window can measure."""
    old_publish = SETTLED_PUBLISH - timedelta(days=30)
    await _published_fix(db, shop.id, published_at=old_publish)
    # Baseline completes AFTER that publish but before the second one.
    await _baseline(db, shop.id, panel_id, completed_at=SETTLED_PUBLISH - timedelta(days=7))
    recent_fix = await _published_fix(db, shop.id, published_at=SETTLED_PUBLISH)

    response = await client.post(f"/shops/by-domain/{SHOP}/verify", headers=HEADERS)

    assert response.status_code == 202
    assert response.json()["measured_fix_count"] == 1
    assert [fix["fix_id"] for fix in enqueued[0][2]] == [recent_fix.id]


async def test_another_shops_published_fix_is_not_measured(client, db, shop, panel_id, enqueued):
    other = Shop(shop_domain="verify-other.myshopify.com", status=ShopStatus.active)
    db.add(other)
    await db.commit()
    await db.refresh(other)
    await _published_fix(db, other.id, published_at=SETTLED_PUBLISH)

    await _baseline(db, shop.id, panel_id, completed_at=SETTLED_PUBLISH - timedelta(days=7))

    response = await client.post(f"/shops/by-domain/{SHOP}/verify", headers=HEADERS)

    assert response.status_code == 409  # ours has nothing published
    assert enqueued == []


async def test_route_writes_nothing_to_fixes(client, db, shop, panel_id, enqueued):
    await _baseline(db, shop.id, panel_id, completed_at=SETTLED_PUBLISH - timedelta(days=7))
    await _published_fix(db, shop.id, published_at=SETTLED_PUBLISH)

    columns = (Fix.id, Fix.status, Fix.published_at, Fix.publish_error)
    before = (await db.execute(select(*columns).order_by(Fix.id))).all()

    response = await client.post(f"/shops/by-domain/{SHOP}/verify", headers=HEADERS)
    assert response.status_code == 202

    assert (await db.execute(select(*columns).order_by(Fix.id))).all() == before


# --- the read route ----------------------------------------------------------------------------


async def _seed_verification(db, shop_id: int, panel_id: int, **overrides) -> Verification:
    run = AgentRun(
        shop_id=shop_id, panel_id=panel_id, status=AgentRunStatus.completed,
        completed_at=SETTLED_PUBLISH,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    values = {
        "run_id": run.id,
        "baseline_run_id": None,
        "shop_id": shop_id,
        "engine": "perplexity",
        "panel_id": panel_id,
        "panel_fingerprint": "fp-read",
        "pre_rate": 0.0,
        "post_rate": 0.25,
        "delta": 0.25,
        "pre_mentions": 0,
        "post_mentions": 6,
        "pre_total_queries": 24,
        "post_total_queries": 24,
        "competitor_deltas_json": {
            "Blue Bottle": {"pre_rate": 0.25, "post_rate": 0.125, "delta": -0.125}
        },
        "measured_fixes_json": [
            {
                "fix_id": 9702,
                "product_id": 114,
                "type": "description",
                "target": "body_html",
                "published_at": SETTLED_PUBLISH.isoformat(),
            }
        ],
        "measured_fix_count": 1,
        "published_at_max": SETTLED_PUBLISH,
        "settle_hours": 240.0,
        "settle_satisfied": True,
    }
    row = Verification(**{**values, **overrides})
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def test_get_verification_returns_the_persisted_delta(client, db, shop, panel_id):
    row = await _seed_verification(db, shop.id, panel_id)

    response = await client.get(f"/shops/by-domain/{SHOP}/verification", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == row.run_id
    assert body["settle_satisfied"] is True
    assert body["measured_fixes"][0]["fix_id"] == 9702
    (engine,) = body["engines"]
    assert engine["delta"] == 0.25
    assert engine["competitors"]["Blue Bottle"]["delta"] == -0.125


async def test_get_verification_serializes_no_data_as_null_never_zero(client, db, shop, panel_id):
    await _seed_verification(db, shop.id, panel_id, post_rate=None, delta=None,
                             post_total_queries=0)

    body = (
        await client.get(f"/shops/by-domain/{SHOP}/verification", headers=HEADERS)
    ).json()

    assert body["engines"][0]["post_rate"] is None
    assert body["engines"][0]["delta"] is None


async def test_get_verification_is_run_scoped(client, db, shop, panel_id):
    older = await _seed_verification(db, shop.id, panel_id)
    newer = await _seed_verification(db, shop.id, panel_id, delta=0.5)

    latest = (await client.get(f"/shops/by-domain/{SHOP}/verification", headers=HEADERS)).json()
    assert latest["run_id"] == newer.run_id
    assert latest["engines"][0]["delta"] == 0.5

    pinned = (
        await client.get(
            f"/shops/by-domain/{SHOP}/verification?run_id={older.run_id}", headers=HEADERS
        )
    ).json()
    assert pinned["run_id"] == older.run_id
    assert pinned["engines"][0]["delta"] == 0.25


async def test_get_verification_404s_when_none_exists(client, shop):
    response = await client.get(f"/shops/by-domain/{SHOP}/verification", headers=HEADERS)
    assert response.status_code == 404
