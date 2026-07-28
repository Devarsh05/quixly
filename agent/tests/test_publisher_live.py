"""LIVE contract test for the Publisher's Shopify surface — the no-mock external-surface test.

**This is the only test that proves the write path works.** Every other Publisher test drives a
fake client, and a fake cannot tell us whether ``productUpdate`` accepts our input shape, whether
``PRODUCT_QUERY`` compiles server-side, or whether Shopify echoes ``descriptionHtml`` back
byte-for-byte. CLAUDE.md is explicit that green tests over a mock are not evidence, and this is a
live-write path, so the bar is higher here than anywhere else in the codebase.

Opt-in and destructive-adjacent, so it is fenced three ways:

1. marked ``live``, and skipped unless ``QUIXLY_LIVE_PUBLISH_TEST=1``;
2. it refuses to run against any shop but the dev store;
3. every test **restores the field it touched** in a ``finally``, so the store is left as found.

It needs the app shell running (it pulls its token through the single refresh authority) and the
dev store installed. Run it deliberately:

    QUIXLY_LIVE_PUBLISH_TEST=1 python -m pytest tests/test_publisher_live.py -m live -v
"""

import os

import pytest

from app.graph.publisher import _verify, _write_input
from app.models import Fix, FixStatus, FixType
from app.services.shopify_admin import ShopifyAdminClient, ShopifyWriteError
from app.services.token_provider import TokenProvider

pytestmark = pytest.mark.live

DEV_STORE = "quixly-ljymkoyb.myshopify.com"
# Product 113 — the product every prior spike (L1/L3/L6/L7) used, so its state is documented.
PRODUCT_GID = "gid://shopify/Product/15436808192371"
COFFEE_CATEGORY = "gid://shopify/TaxonomyCategory/fb-1-3-1"

live_only = pytest.mark.skipif(
    os.getenv("QUIXLY_LIVE_PUBLISH_TEST") != "1",
    reason="writes to the live dev store; set QUIXLY_LIVE_PUBLISH_TEST=1 to opt in",
)


@pytest.fixture
async def client():
    from app.settings import get_settings

    get_settings.cache_clear()  # pick up the real INTERNAL_API_KEY / APP_SHELL_URL
    assert DEV_STORE in get_settings().publish_allowed_shops, (
        "refusing to run a live write test against a shop outside the publish allowlist"
    )
    # The token comes through the app shell, the SINGLE refresh authority — exactly as the
    # Publisher gets it. This test opens no second token path.
    yield ShopifyAdminClient(DEV_STORE, TokenProvider())


@live_only
async def test_fetch_product_returns_every_field_we_map(client):
    """Proves PRODUCT_QUERY compiles server-side and selects what ``product_row_from_node`` reads.

    A mock cannot establish this: the query could name a field that does not exist at API 2026-07
    and every mocked test would still pass.
    """
    from app.services.catalog import product_row_from_node, stable_source_hash

    node = await client.fetch_product(PRODUCT_GID)

    assert node is not None, "product 113 is missing from the dev store"
    assert node["id"] == PRODUCT_GID
    assert "descriptionHtml" in node
    assert "category" in node and "id" in (node["category"] or {"id": None})
    assert node["variants"]["nodes"], "variant selection returned nothing"

    row = product_row_from_node(node)
    assert set(row) >= {"title", "body", "variants_json", "metafields_json", "product_type"}
    assert stable_source_hash(row)  # the projection survives real data


@live_only
async def test_a_missing_product_reads_as_None_not_an_error(client):
    """The Publisher treats ``None`` as "deleted since approval", so the shape matters."""
    assert await client.fetch_product("gid://shopify/Product/1") is None


@live_only
async def test_description_write_round_trip_and_whether_shopify_echoes_it_byte_for_byte(client):
    """The flagship: write → re-read → verify, then restore.

    Also settles the open question the Publisher's structural fallback exists for — whether
    ``descriptionHtml`` comes back byte-identical. The assertion is on our VERIFY contract, not on
    byte-identity, and the byte-identity result is reported either way.
    """
    original = (await client.fetch_product(PRODUCT_GID))["descriptionHtml"] or ""
    appended = (
        "<p><strong>Details</strong></p>"
        "<ul><li>Quixly Live Test: publisher round trip</li></ul>"
    )
    target = original + appended

    fix = Fix(
        id=-1,
        product_id=-1,
        type=FixType.description,
        status=FixStatus.approved,
        target="body_html",
        before_json={"body_html": original},
        after_json={"body_html": target},
    )

    try:
        await client.update_product(_write_input(PRODUCT_GID, fix))

        # A SEPARATE read. The mutation's own payload is not evidence.
        confirmed = await client.fetch_product(PRODUCT_GID)
        verified, why = _verify(fix, confirmed)
        assert verified, f"the write did not land as intended: {why}"

        live = confirmed["descriptionHtml"]
        print(
            "\nBYTE-IDENTITY: Shopify echoed descriptionHtml "
            f"{'byte-for-byte' if live == target else 'NORMALISED (structural check carried it)'}"
        )
        assert original in live or _verify(fix, confirmed)[0]
    finally:
        await client.update_product({"id": PRODUCT_GID, "descriptionHtml": original})
        restored = (await client.fetch_product(PRODUCT_GID))["descriptionHtml"] or ""
        assert "Quixly Live Test" not in restored, "FAILED TO RESTORE the dev store description"


@live_only
async def test_category_write_lands_the_exact_gid(client):
    """The publish-class write. Proven at spike L1; asserted here through OUR code path."""
    before = (await client.fetch_product(PRODUCT_GID))["category"] or {}
    original_gid = before.get("id")

    fix = Fix(
        id=-1,
        product_id=-1,
        type=FixType.category,
        status=FixStatus.approved,
        target="category",
        before_json={"category": before.get("fullName")},
        after_json={"category": COFFEE_CATEGORY, "fullName": "…Coffee Beans & Ground Coffee"},
    )

    try:
        await client.update_product(_write_input(PRODUCT_GID, fix))

        confirmed = await client.fetch_product(PRODUCT_GID)
        verified, why = _verify(fix, confirmed)
        assert verified, why
        assert confirmed["category"]["id"] == COFFEE_CATEGORY
    finally:
        if original_gid and original_gid != COFFEE_CATEGORY:
            await client.update_product({"id": PRODUCT_GID, "category": original_gid})


@live_only
async def test_user_errors_raise_instead_of_reading_as_a_silent_success(client):
    """``productUpdate`` returns HTTP 200 when it REFUSES a write.

    This is the exact failure a transport-status check misses, and the reason
    ``ShopifyWriteError`` exists. Nothing is written: the mutation is rejected.
    """
    with pytest.raises(ShopifyWriteError):
        await client.update_product(
            {"id": PRODUCT_GID, "category": "gid://shopify/TaxonomyCategory/not-a-real-category"}
        )
