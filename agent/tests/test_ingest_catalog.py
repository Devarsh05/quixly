"""The ingest job: idempotent, batched, and resumable.

A failed ingest must leave the products it already wrote plus a cursor to resume from —
never an empty table, and never a duplicated catalog on retry.
"""

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.jobs import ingest_catalog as job_module
from app.jobs.ingest_catalog import ingest_catalog
from app.models import IngestRun, IngestStatus, Product, Shop, ShopStatus
from app.services.token_provider import TokenFetchError, TokenProvider, TokenUnavailableError

SHOP = "ingest-test.myshopify.com"
TOKEN_URL = f"http://app-shell.test/internal/shops/{SHOP}/admin-token"


def _product(
    pid: int,
    title: str,
    barcode: str | None = None,
    status: str = "ACTIVE",
    product_type: str | None = "Coffee",
    category: str | None = None,
) -> dict:
    # `status` mirrors Shopify's GraphQL ProductStatus enum, which is uppercase.
    return {
        "id": f"gid://shopify/Product/{pid}",
        "title": title,
        "descriptionHtml": f"<p>{title}</p>",
        "status": status,
        "productType": product_type,
        "category": {"fullName": category} if category else None,
        "variants": {"nodes": [{"id": f"gid://shopify/Variant/{pid}", "barcode": barcode}]},
        "metafields": {"nodes": []},
    }


class FakeAdminClient:
    """Stands in for ShopifyAdminClient, yielding canned pages."""

    def __init__(self, pages, fail_after: int | None = None, error: Exception | None = None):
        self._pages = pages
        self._fail_after = fail_after
        self._error = error or RuntimeError("shopify exploded")

    async def iter_products(self, cursor=None):
        start = 0
        if cursor is not None:
            # Resume: skip the pages already committed.
            start = next(i for i, (_, c) in enumerate(self._pages) if c == cursor) + 1

        for index in range(start, len(self._pages)):
            if self._fail_after is not None and index >= self._fail_after:
                raise self._error
            yield self._pages[index]


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


@pytest.fixture
async def run(db, shop):
    run = IngestRun(shop_id=shop.id, status=IngestStatus.queued)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


@pytest.fixture
def patched_job(db, monkeypatch):
    """Point the job at the test's transaction-scoped session and a fake Shopify."""

    class SessionCtx:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(job_module, "SessionLocal", lambda: SessionCtx())

    async def noop_release(_shop_domain):
        return None

    monkeypatch.setattr(job_module, "release_ingest_lock", noop_release)

    def install(client):
        monkeypatch.setattr(job_module, "ShopifyAdminClient", lambda *a, **kw: client)

    return install


async def test_writes_products_and_completes(db, shop, run, patched_job):
    patched_job(
        FakeAdminClient(
            [
                ([_product(1, "Coffee", "0123456789012")], "cursor-1"),
                ([_product(2, "Grinder")], "cursor-2"),
            ]
        )
    )

    await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    await db.refresh(run)
    assert run.status == IngestStatus.complete
    assert run.products_written == 2
    assert run.completed_at is not None

    count = await db.scalar(
        select(func.count()).select_from(Product).where(Product.shop_id == shop.id)
    )
    assert count == 2

    # gtin is lifted from the primary variant's barcode.
    coffee = (
        await db.execute(
            select(Product).where(Product.shopify_product_id == "gid://shopify/Product/1")
        )
    ).scalar_one()
    assert coffee.gtin == "0123456789012"
    assert coffee.title == "Coffee"
    # Shopify's uppercase ACTIVE is normalized to the lowercase canonical.
    assert coffee.visibility_state == "active"


async def test_captures_product_type_and_category(db, shop, run, patched_job):
    """productType and the taxonomy category fullName are ingested onto the product row."""
    patched_job(
        FakeAdminClient(
            [
                (
                    [
                        _product(1, "Ethiopia", product_type="Coffee"),
                        _product(
                            2, "Grinder", product_type="Brewing Gear",
                            category="Home & Garden > Kitchen > Coffee Grinders",
                        ),
                        _product(3, "Untyped", product_type=None, category=None),
                    ],
                    "cursor-1",
                )
            ]
        )
    )

    await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    rows = dict(
        (
            await db.execute(
                select(Product.shopify_product_id, Product.product_type).where(
                    Product.shop_id == shop.id
                )
            )
        ).all()
    )
    assert rows["gid://shopify/Product/1"] == "Coffee"
    assert rows["gid://shopify/Product/2"] == "Brewing Gear"
    assert rows["gid://shopify/Product/3"] is None

    grinder = (
        await db.execute(
            select(Product).where(Product.shopify_product_id == "gid://shopify/Product/2")
        )
    ).scalar_one()
    assert grinder.category == "Home & Garden > Kitchen > Coffee Grinders"


async def test_product_status_is_normalized_to_lowercase(db, shop, run, patched_job):
    """Shopify's uppercase ProductStatus enum is stored as the lowercase canonical."""
    patched_job(
        FakeAdminClient(
            [
                (
                    [
                        _product(1, "Live", status="ACTIVE"),
                        _product(2, "Unpublished", status="DRAFT"),
                        _product(3, "Retired", status="ARCHIVED"),
                        _product(4, "Direct link only", status="UNLISTED"),
                    ],
                    "cursor-1",
                )
            ]
        )
    )

    await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    states = dict(
        (
            await db.execute(
                select(Product.shopify_product_id, Product.visibility_state).where(
                    Product.shop_id == shop.id
                )
            )
        ).all()
    )
    assert states["gid://shopify/Product/1"] == "active"
    assert states["gid://shopify/Product/2"] == "draft"
    assert states["gid://shopify/Product/3"] == "archived"
    # `unlisted` (reachable on Admin API 2026-07) is kept as its own state, not collapsed.
    assert states["gid://shopify/Product/4"] == "unlisted"


async def test_unmapped_status_fails_the_run(db, shop, run, patched_job):
    """An unrecognized ProductStatus raises, failing the run rather than leaking a raw value."""
    patched_job(
        FakeAdminClient([([_product(1, "Mystery", status="SOMETHING_NEW")], "cursor-1")])
    )

    with pytest.raises(ValueError):
        await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    await db.refresh(run)
    assert run.status == IngestStatus.failed
    assert run.error


async def test_rerunning_upserts_rather_than_duplicating(db, shop, run, patched_job):
    pages = [([_product(1, "Coffee")], "cursor-1")]
    patched_job(FakeAdminClient(pages))
    await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    # Second run over a renamed catalog: same product id, new title.
    second = IngestRun(shop_id=shop.id, status=IngestStatus.queued)
    db.add(second)
    await db.commit()
    await db.refresh(second)

    patched_job(FakeAdminClient([([_product(1, "Coffee, Dark Roast")], "cursor-1")]))
    await ingest_catalog({"token_provider": object()}, SHOP, second.id)

    count = await db.scalar(
        select(func.count()).select_from(Product).where(Product.shop_id == shop.id)
    )
    assert count == 1, "re-ingest must upsert, not duplicate"

    product = (
        await db.execute(select(Product).where(Product.shop_id == shop.id))
    ).scalar_one()
    assert product.title == "Coffee, Dark Roast"


async def test_failure_midway_keeps_written_rows_and_a_resumable_cursor(
    db, shop, run, patched_job
):
    patched_job(
        FakeAdminClient(
            [
                ([_product(1, "Coffee")], "cursor-1"),
                ([_product(2, "Grinder")], "cursor-2"),
                ([_product(3, "Kettle")], "cursor-3"),
            ],
            fail_after=2,  # two pages commit, then Shopify dies
        )
    )

    with pytest.raises(RuntimeError):
        await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    await db.refresh(run)
    assert run.status == IngestStatus.failed
    assert run.error
    # The whole point: partial progress survives.
    assert run.products_written == 2
    assert run.cursor == "cursor-2"

    count = await db.scalar(
        select(func.count()).select_from(Product).where(Product.shop_id == shop.id)
    )
    assert count == 2, "rows committed before the failure must survive"


async def test_resumes_from_the_stored_cursor(db, shop, run, patched_job):
    pages = [
        ([_product(1, "Coffee")], "cursor-1"),
        ([_product(2, "Grinder")], "cursor-2"),
        ([_product(3, "Kettle")], "cursor-3"),
    ]

    patched_job(FakeAdminClient(pages, fail_after=2))
    with pytest.raises(RuntimeError):
        await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    # Retry the same run: it should pick up at page 3 only.
    patched_job(FakeAdminClient(pages))
    await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    await db.refresh(run)
    assert run.status == IngestStatus.complete

    count = await db.scalar(
        select(func.count()).select_from(Product).where(Product.shop_id == shop.id)
    )
    assert count == 3


async def test_dead_refresh_chain_flags_the_shop_for_reauth(db, shop, run, patched_job):
    patched_job(
        FakeAdminClient(
            [([_product(1, "Coffee")], "cursor-1")],
            fail_after=0,
            error=TokenUnavailableError("no session"),
        )
    )

    # Permanent failure: swallowed, not re-raised — retrying cannot help.
    await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    await db.refresh(run)
    await db.refresh(shop)
    assert run.status == IngestStatus.failed
    assert shop.status == ShopStatus.reauth_required


async def test_transient_token_error_does_not_flag_reauth(db, shop, run, patched_job):
    """An unreachable app shell must not permanently brand a healthy shop."""
    patched_job(
        FakeAdminClient(
            [([_product(1, "Coffee")], "cursor-1")],
            fail_after=0,
            error=TokenFetchError("app shell down"),
        )
    )

    with pytest.raises(TokenFetchError):
        await ingest_catalog({"token_provider": object()}, SHOP, run.id)

    await db.refresh(run)
    await db.refresh(shop)
    assert run.status == IngestStatus.failed
    assert shop.status == ShopStatus.active, "a transient blip must not force re-auth"


# --- The dead-refresh-chain path, end to end -------------------------------------------
# These two run the REAL TokenProvider and REAL ShopifyAdminClient (patched_job's `install`
# is deliberately not called), with only the app shell's HTTP response faked. They pin the
# whole chain, because every link in it has to agree on what "permanent" means:
#
#   Shopify rejects the refresh token (invalid_grant)
#     -> app shell returns 404
#     -> TokenProvider raises TokenUnavailableError (not TokenFetchError)
#     -> ingest job sets shops.status = reauth_required
#
# If any link mapped invalid_grant to "transient", a 90-day-idle shop would be retried
# forever and never surfaced to the merchant.


@respx.mock
async def test_dead_refresh_chain_lands_the_shop_in_reauth_required(db, shop, run, patched_job):
    """404 from the app shell (its response to invalid_grant) => reauth_required."""
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            404, json={"error": "Re-auth required", "reauth_required": True}
        )
    )

    # Permanent: swallowed rather than re-raised, because no retry can fix it.
    await ingest_catalog({"token_provider": TokenProvider()}, SHOP, run.id)

    assert token_route.called

    await db.refresh(run)
    await db.refresh(shop)

    assert shop.status == ShopStatus.reauth_required
    assert run.status == IngestStatus.failed
    assert run.products_written == 0
    assert run.error


@respx.mock
async def test_app_shell_502_leaves_the_shop_active(db, shop, run, patched_job):
    """The mirror image: a transient 502 must NOT reach reauth_required."""
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(502))

    with pytest.raises(TokenFetchError):
        await ingest_catalog({"token_provider": TokenProvider()}, SHOP, run.id)

    await db.refresh(run)
    await db.refresh(shop)

    assert run.status == IngestStatus.failed
    assert shop.status == ShopStatus.active, "a transient 502 must never force re-auth"
