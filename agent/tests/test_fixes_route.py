"""The approval gate routes (Phase 3, step 3).

Driven through ``httpx.ASGITransport`` against the transaction-scoped ``db`` fixture, mirroring
``test_scan_route``. The Arq enqueue is captured, not executed.

The load-bearing assertions here are the ones that keep a merchant safe:

* a ``metafield`` (taxonomy) or ``merchant_todo`` fix can NEVER be approved — enforced by the API,
  not merely hidden by the UI, because the UI is not a security boundary;
* a fix can only be acted on by the shop that owns it (cross-shop → 404, never 403);
* approving supersedes any other fix on the same ``(product, target)``, so Step 4 can never find
  two approved rows for one product body and append its Details block twice.
"""

import httpx
import pytest
from sqlalchemy import select

from app.api import fixes as fixes_api
from app.db import get_db
from app.main import app
from app.models import (
    AgentRun,
    AgentRunStatus,
    Audit,
    Fix,
    FixStatus,
    FixType,
    Product,
    Shop,
    ShopStatus,
)
from app.models import QueryPanel as QueryPanelRow
from tests.conftest import TEST_API_KEY

SHOP = "approval-test.myshopify.com"
OTHER_SHOP = "other-shop.myshopify.com"
HEADERS = {"X-Internal-Api-Key": TEST_API_KEY}

BODY_BEFORE = "<p>A bright everyday coffee.</p>"
BODY_AFTER = (
    "<p>A bright everyday coffee.</p><p><strong>Details</strong></p>"
    "<ul><li>Origin: Ethiopia</li><li>Process: Washed</li></ul>"
)


@pytest.fixture
def enqueued(monkeypatch) -> list[int]:
    calls: list[int] = []

    async def fake_enqueue(run_id: int) -> None:
        calls.append(run_id)

    monkeypatch.setattr(fixes_api, "_enqueue", fake_enqueue)
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


async def _make_shop(db, domain: str) -> Shop:
    shop = Shop(shop_domain=domain, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _make_run(db, shop_id: int) -> AgentRun:
    """A completed run for the shop, reusing its panel — (shop_id, fingerprint) is unique, and a
    shop legitimately has many runs over one panel."""
    panel = (
        await db.execute(select(QueryPanelRow).where(QueryPanelRow.shop_id == shop_id))
    ).scalars().first()
    if panel is None:
        panel = QueryPanelRow(
            shop_id=shop_id, category="coffee", queries_json=[], fingerprint=f"fp-{shop_id}"
        )
        db.add(panel)
        await db.commit()
        await db.refresh(panel)
    run = AgentRun(shop_id=shop_id, panel_id=panel.id, status=AgentRunStatus.completed)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _make_product(db, shop_id: int, *, title="Ethiopia Yirgacheffe 340 g") -> Product:
    product = Product(
        shop_id=shop_id, shopify_product_id=f"gid://shopify/Product/{shop_id}-1", title=title,
        body=BODY_BEFORE, variants_json=[], visibility_state="active", product_type="Coffee",
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    db.add(Audit(product_id=product.id, product_class="coffee", gaps_json=[], severity="high"))
    await db.commit()
    return product


def _fix(product_id: int, run_id: int, fix_type: FixType, target: str, **kwargs) -> Fix:
    return Fix(
        product_id=product_id, run_id=run_id, type=fix_type,
        status=kwargs.pop("status", FixStatus.proposed), target=target, **kwargs,
    )


@pytest.fixture
async def seeded(db):
    """A shop with one run and one product carrying all four fix types."""
    shop = await _make_shop(db, SHOP)
    run = await _make_run(db, shop.id)
    product = await _make_product(db, shop.id)
    db.add_all([
        _fix(
            product.id, run.id, FixType.description, "body_html",
            before_json={"body_html": BODY_BEFORE}, after_json={"body_html": BODY_AFTER},
            source_json=[{"attribute": "origin", "source_field": "title", "snippet": "Ethiopia"}],
            diff="appended 2 labeled spec line(s)",
        ),
        _fix(
            product.id, run.id, FixType.category, "category",
            before_json={"category": None},
            after_json={"category": "gid://shopify/TaxonomyCategory/fb-1-3-1",
                        "fullName": "Food, Beverages & Tobacco > Beverages > Coffee"},
            source_json=[{"attribute": "category", "source_field": "product_type",
                          "snippet": "Coffee"}],
        ),
        _fix(
            product.id, run.id, FixType.metafield, "metafield:shopify.country",
            after_json={"namespace": "shopify", "key": "country", "value": "Ethiopia",
                        "taxonomy_value_gid": "gid://shopify/TaxonomyValue/8882"},
            source_json=[{"attribute": "origin", "source_field": "title", "snippet": "Ethiopia"}],
        ),
        _fix(
            product.id, run.id, FixType.merchant_todo, "spec:brew_method",
            reason="No brew method stated in any source field; a merchant must add it.",
        ),
    ])
    await db.commit()
    return {"shop": shop, "run": run, "product": product}


async def _fixes_of(db, product_id: int, fix_type: FixType | None = None):
    query = select(Fix).where(Fix.product_id == product_id).order_by(Fix.id)
    rows = (await db.execute(query)).scalars().all()
    return [f for f in rows if fix_type is None or f.type == fix_type]


# --- auth ------------------------------------------------------------------------------------
async def test_every_route_requires_the_internal_key(client, seeded):
    assert (await client.get(f"/shops/by-domain/{SHOP}/fixes")).status_code == 401
    assert (await client.post(f"/shops/by-domain/{SHOP}/fixes/run")).status_code == 401
    assert (await client.post(f"/shops/by-domain/{SHOP}/fixes/1/approve")).status_code == 401


# --- list ------------------------------------------------------------------------------------
async def test_list_groups_by_product_and_splits_by_approvability(client, seeded):
    response = await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()

    assert body["run_id"] == seeded["run"].id
    assert len(body["products"]) == 1
    product = body["products"][0]
    assert product["product_id"] == seeded["product"].id
    assert product["title"] == "Ethiopia Yirgacheffe 340 g"
    assert product["severity"] == "high"

    assert {f["type"] for f in product["approvable"]} == {"description", "category"}
    assert {f["type"] for f in product["not_publishable"]} == {"metafield"}
    assert {f["type"] for f in product["needs_input"]} == {"merchant_todo"}


async def test_description_fix_renders_added_lines_not_raw_html(client, seeded):
    response = await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)
    description = next(
        f for f in response.json()["products"][0]["approvable"] if f["type"] == "description"
    )
    assert description["added_lines"] == [
        {"label": "Origin", "value": "Ethiopia"},
        {"label": "Process", "value": "Washed"},
    ]
    # Grounding is the selling point — the citation rides along.
    assert description["citations"][0]["source_field"] == "title"
    assert description["citations"][0]["snippet"] == "Ethiopia"


async def test_added_lines_falls_back_when_after_does_not_extend_before(client):
    # The composer appends, so ``after`` starts with ``before``. If that ever stops holding we must
    # show the whole block rather than a silently wrong diff.
    lines = fixes_api._added_lines(
        {"body_html": "<p>totally different</p>"},
        {"body_html": "<ul><li>Origin: Ethiopia</li></ul>"},
    )
    assert lines == [fixes_api.AddedLine(label="Origin", value="Ethiopia")]


async def test_category_and_metafield_previews(client, seeded):
    body = (await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)).json()
    product = body["products"][0]

    category = next(f for f in product["approvable"] if f["type"] == "category")
    assert category["category_from"] == "Uncategorized"
    assert category["category_to"].startswith("Food, Beverages & Tobacco")

    metafield = product["not_publishable"][0]
    assert metafield["metafield_key"] == "shopify.country"
    assert metafield["metafield_value"] == "Ethiopia"
    assert metafield["approvable"] is False
    assert "permission" in metafield["block_reason"]


async def test_todo_carries_its_truthful_reason_and_is_not_approvable(client, seeded):
    body = (await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)).json()
    todo = body["products"][0]["needs_input"][0]
    assert todo["approvable"] is False
    assert "No brew method stated in any source field" in todo["reason"]


async def test_list_is_run_scoped_and_excludes_older_runs(client, db, seeded):
    older = await _make_run(db, seeded["shop"].id)  # a NEWER row, used as a second run
    db.add(_fix(seeded["product"].id, older.id, FixType.description, "body_html",
                after_json={"body_html": BODY_AFTER}))
    await db.commit()

    # Default resolution = the latest run that produced fixes.
    body = (await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)).json()
    assert body["run_id"] == older.id
    assert len(body["products"][0]["approvable"]) == 1  # only the new run's row

    # Explicit run_id still reaches the earlier run.
    scoped = (
        await client.get(
            f"/shops/by-domain/{SHOP}/fixes?run_id={seeded['run'].id}", headers=HEADERS
        )
    ).json()
    assert scoped["run_id"] == seeded["run"].id
    assert len(scoped["products"][0]["approvable"]) == 2


async def test_list_omits_non_proposed_fixes(client, db, seeded):
    approved = (await _fixes_of(db, seeded["product"].id, FixType.description))[0]
    approved.status = FixStatus.approved
    await db.commit()

    body = (await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)).json()
    assert {f["type"] for f in body["products"][0]["approvable"]} == {"category"}


async def test_list_unknown_shop_404_but_a_shop_with_no_fixes_is_empty(client, db):
    assert (
        await client.get("/shops/by-domain/nope.myshopify.com/fixes", headers=HEADERS)
    ).status_code == 404

    await _make_shop(db, OTHER_SHOP)
    response = await client.get(f"/shops/by-domain/{OTHER_SHOP}/fixes", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "run_id": None,
        "status": None,
        "products": [],
        "publish_run_id": None,
        "publish_status": None,
    }


# --- run trigger -----------------------------------------------------------------------------
async def test_fix_run_returns_202_and_commits_the_run_before_enqueue(client, db, seeded, enqueued):
    response = await client.post(f"/shops/by-domain/{SHOP}/fixes/run", headers=HEADERS)

    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert response.json()["status"] == "running"
    assert enqueued == [run_id]

    run = await db.get(AgentRun, run_id)
    assert run is not None and run.status == AgentRunStatus.running


async def test_fix_run_unknown_shop_404_and_enqueues_nothing(client, enqueued):
    response = await client.post("/shops/by-domain/nope.myshopify.com/fixes/run", headers=HEADERS)
    assert response.status_code == 404
    assert enqueued == []


# --- approve / reject ------------------------------------------------------------------------
async def test_approve_flips_proposed_to_approved_and_is_idempotent(client, db, seeded):
    fix = (await _fixes_of(db, seeded["product"].id, FixType.description))[0]

    first = await client.post(f"/shops/by-domain/{SHOP}/fixes/{fix.id}/approve", headers=HEADERS)
    assert first.status_code == 200
    assert first.json() == {"fix_id": fix.id, "status": "approved"}

    # Idempotent: re-approving the same fix is a 200 no-op, not a 409.
    again = await client.post(f"/shops/by-domain/{SHOP}/fixes/{fix.id}/approve", headers=HEADERS)
    assert again.status_code == 200

    await db.refresh(fix)
    assert fix.status == FixStatus.approved


async def test_reject_flips_proposed_to_rejected(client, db, seeded):
    fix = (await _fixes_of(db, seeded["product"].id, FixType.category))[0]
    response = await client.post(f"/shops/by-domain/{SHOP}/fixes/{fix.id}/reject", headers=HEADERS)

    assert response.status_code == 200
    await db.refresh(fix)
    assert fix.status == FixStatus.rejected


async def test_approving_a_rejected_fix_is_409_not_a_silent_noop(client, db, seeded):
    fix = (await _fixes_of(db, seeded["product"].id, FixType.description))[0]
    fix.status = FixStatus.rejected
    await db.commit()

    response = await client.post(f"/shops/by-domain/{SHOP}/fixes/{fix.id}/approve", headers=HEADERS)
    assert response.status_code == 409
    await db.refresh(fix)
    assert fix.status == FixStatus.rejected


@pytest.mark.parametrize("terminal", [FixStatus.published, FixStatus.stale, FixStatus.verified])
async def test_a_terminal_fix_cannot_be_decided(client, db, seeded, terminal):
    fix = (await _fixes_of(db, seeded["product"].id, FixType.description))[0]
    fix.status = terminal
    await db.commit()

    assert (
        await client.post(f"/shops/by-domain/{SHOP}/fixes/{fix.id}/approve", headers=HEADERS)
    ).status_code == 409


@pytest.mark.parametrize("blocked", [FixType.metafield, FixType.merchant_todo])
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_non_approvable_types_are_refused_by_the_API(client, db, seeded, blocked, decision):
    # The invariant: no approvable path to a write that cannot execute. Enforced server-side,
    # because the UI is not a security boundary.
    fix = (await _fixes_of(db, seeded["product"].id, blocked))[0]

    response = await client.post(
        f"/shops/by-domain/{SHOP}/fixes/{fix.id}/{decision}", headers=HEADERS
    )
    assert response.status_code == 409
    await db.refresh(fix)
    assert fix.status == FixStatus.proposed


async def test_approve_supersedes_any_other_fix_on_the_same_target(client, db, seeded):
    # Step 4's description composer APPENDS, so two approved rows for one body would append the
    # Details block twice. At most one approved fix per (product, target).
    product_id = seeded["product"].id
    older = (await _fixes_of(db, product_id, FixType.description))[0]
    older.status = FixStatus.approved
    newer = _fix(product_id, seeded["run"].id, FixType.description, "body_html",
                 after_json={"body_html": BODY_AFTER})
    db.add(newer)
    await db.commit()
    await db.refresh(newer)

    response = await client.post(
        f"/shops/by-domain/{SHOP}/fixes/{newer.id}/approve", headers=HEADERS
    )
    assert response.status_code == 200

    await db.refresh(older)
    await db.refresh(newer)
    assert newer.status == FixStatus.approved
    assert older.status == FixStatus.stale

    approved = [
        f for f in await _fixes_of(db, product_id, FixType.description)
        if f.status == FixStatus.approved
    ]
    assert len(approved) == 1


async def test_supersede_does_not_touch_a_different_target(client, db, seeded):
    product_id = seeded["product"].id
    category = (await _fixes_of(db, product_id, FixType.category))[0]
    category.status = FixStatus.approved
    await db.commit()

    description = (await _fixes_of(db, product_id, FixType.description))[0]
    await client.post(f"/shops/by-domain/{SHOP}/fixes/{description.id}/approve", headers=HEADERS)

    await db.refresh(category)
    assert category.status == FixStatus.approved  # different target — untouched


# --- authorization ---------------------------------------------------------------------------
async def test_a_shop_cannot_decide_another_shops_fix(client, db, seeded):
    """Cross-shop access is 404, never 403 — ids must not be probeable for existence."""
    other = await _make_shop(db, OTHER_SHOP)
    other_run = await _make_run(db, other.id)
    other_product = await _make_product(db, other.id, title="Someone else's coffee")
    victim = _fix(other_product.id, other_run.id, FixType.description, "body_html",
                  after_json={"body_html": BODY_AFTER})
    db.add(victim)
    await db.commit()
    await db.refresh(victim)

    response = await client.post(
        f"/shops/by-domain/{SHOP}/fixes/{victim.id}/approve", headers=HEADERS
    )
    assert response.status_code == 404

    await db.refresh(victim)
    assert victim.status == FixStatus.proposed  # untouched


async def test_a_shops_list_never_leaks_another_shops_fixes(client, db, seeded):
    other = await _make_shop(db, OTHER_SHOP)
    other_run = await _make_run(db, other.id)
    other_product = await _make_product(db, other.id, title="Someone else's coffee")
    db.add(_fix(other_product.id, other_run.id, FixType.description, "body_html",
                after_json={"body_html": BODY_AFTER}))
    await db.commit()

    body = (await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)).json()
    assert [p["product_id"] for p in body["products"]] == [seeded["product"].id]


# --- publish (step 4) ------------------------------------------------------------------------
@pytest.fixture
def publish_enqueued(monkeypatch) -> list[int]:
    calls: list[int] = []

    async def fake_enqueue(run_id: int) -> None:
        calls.append(run_id)

    monkeypatch.setattr(fixes_api, "_enqueue_publish", fake_enqueue)
    return calls


async def test_publish_requires_the_internal_key(client, seeded):
    assert (await client.post(f"/shops/by-domain/{SHOP}/fixes/publish")).status_code == 401


async def test_publish_with_nothing_approved_409s_rather_than_running_an_empty_job(
    client, seeded, publish_enqueued
):
    """"Publish" must never silently do nothing — an empty run reads as success."""
    response = await client.post(f"/shops/by-domain/{SHOP}/fixes/publish", headers=HEADERS)

    assert response.status_code == 409
    assert publish_enqueued == []


async def test_publish_enqueues_a_run_for_the_shops_approved_rows(
    client, db, seeded, publish_enqueued
):
    fix = (await _fixes_of(db, seeded["product"].id, FixType.description))[0]
    await client.post(f"/shops/by-domain/{SHOP}/fixes/{fix.id}/approve", headers=HEADERS)

    response = await client.post(f"/shops/by-domain/{SHOP}/fixes/publish", headers=HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == AgentRunStatus.running
    assert publish_enqueued == [body["run_id"]]

    # The route itself writes NO fix row — it only enqueues. The gate stays the only status writer
    # from a merchant decision.
    await db.refresh(fix)
    assert fix.status == FixStatus.approved


async def test_publish_ignores_another_shops_approved_rows(
    client, db, seeded, publish_enqueued
):
    other = await _make_shop(db, OTHER_SHOP)
    other_run = await _make_run(db, other.id)
    other_product = await _make_product(db, other.id, title="Someone else's coffee")
    db.add(_fix(other_product.id, other_run.id, FixType.description, "body_html",
                status=FixStatus.approved, after_json={"body_html": BODY_AFTER}))
    await db.commit()

    # SHOP has nothing approved of its own, so it must still 409 despite the other shop's row.
    response = await client.post(f"/shops/by-domain/{SHOP}/fixes/publish", headers=HEADERS)
    assert response.status_code == 409
    assert publish_enqueued == []


# --- the widened list ------------------------------------------------------------------------
async def test_an_approved_fix_moves_to_ready_so_it_is_never_invisible(client, db, seeded):
    """Approving must not make a fix disappear: ``ready`` is exactly the Publisher's work set."""
    fix = (await _fixes_of(db, seeded["product"].id, FixType.description))[0]
    await client.post(f"/shops/by-domain/{SHOP}/fixes/{fix.id}/approve", headers=HEADERS)

    product = (await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)).json()[
        "products"
    ][0]

    assert [f["id"] for f in product["ready"]] == [fix.id]
    assert fix.id not in [f["id"] for f in product["approvable"]]


async def test_approved_rows_from_an_EARLIER_run_are_still_listed(client, db, seeded):
    """The Publisher publishes every approved row for the shop, so the list must show them all.

    Run-scoping this half would show the merchant fewer rows than we would actually write.
    """
    fix = (await _fixes_of(db, seeded["product"].id, FixType.description))[0]
    fix.status = FixStatus.approved
    later_run = await _make_run(db, seeded["shop"].id)
    db.add(_fix(seeded["product"].id, later_run.id, FixType.category, "category",
                after_json={"category": "gid://shopify/TaxonomyCategory/fb-1-3-1"}))
    await db.commit()

    body = (await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)).json()

    assert body["run_id"] == later_run.id  # the newest run scopes the PROPOSED rows
    assert [f["id"] for f in body["products"][0]["ready"]] == [fix.id]  # ...but not the approved


async def test_publish_outcomes_are_surfaced_with_their_cause(client, db, seeded):
    fix = (await _fixes_of(db, seeded["product"].id, FixType.description))[0]
    fix.status = FixStatus.publish_failed
    fix.publish_error = "Shopify reports the appended line is missing"
    await db.commit()

    product = (await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)).json()[
        "products"
    ][0]

    settled = product["settled"][0]
    assert settled["id"] == fix.id
    assert settled["publish_error"] == "Shopify reports the appended line is missing"
    assert settled["published_at"] is None


async def test_a_supersede_stale_row_stays_hidden_but_a_publisher_refusal_shows(client, db, seeded):
    """``stale`` is overloaded. Only the Publisher leaves a cause, and only those are shown."""
    description, category = (
        (await _fixes_of(db, seeded["product"].id, FixType.description))[0],
        (await _fixes_of(db, seeded["product"].id, FixType.category))[0],
    )
    description.status = FixStatus.stale  # Step 3 supersede — no cause recorded
    category.status = FixStatus.stale
    category.publish_error = "the product category changed after this fix was approved"
    await db.commit()

    product = (await client.get(f"/shops/by-domain/{SHOP}/fixes", headers=HEADERS)).json()[
        "products"
    ][0]

    assert [f["id"] for f in product["settled"]] == [category.id]


async def test_publish_run_status_is_echoed_back_for_polling(client, db, seeded):
    publish_run = await _make_run(db, seeded["shop"].id)
    publish_run.status = AgentRunStatus.running
    await db.commit()

    body = (
        await client.get(
            f"/shops/by-domain/{SHOP}/fixes?publish_run_id={publish_run.id}", headers=HEADERS
        )
    ).json()

    assert body["publish_run_id"] == publish_run.id
    assert body["publish_status"] == AgentRunStatus.running


async def test_another_shops_publish_run_is_not_reported(client, db, seeded):
    other = await _make_shop(db, OTHER_SHOP)
    other_run = await _make_run(db, other.id)

    body = (
        await client.get(
            f"/shops/by-domain/{SHOP}/fixes?publish_run_id={other_run.id}", headers=HEADERS
        )
    ).json()

    assert body["publish_status"] is None
