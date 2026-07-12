"""ShopifyAdminClient: token refresh on 401, and failing rather than looping forever."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.services.shopify_admin import ShopifyAdminClient, ShopifyAdminError
from app.services.token_provider import TokenProvider

SHOP = "quixly-dev.myshopify.com"
TOKEN_URL = f"http://app-shell.test/internal/shops/{SHOP}/admin-token"
GRAPHQL_URL = f"https://{SHOP}/admin/api/2025-10/graphql.json"

OLD_TOKEN = "shpat_old"
NEW_TOKEN = "shpat_new"


def _token_response(token: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": token,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=60)).isoformat(),
        },
    )


@respx.mock
async def test_retries_once_with_a_fresh_token_after_401():
    """Shopify rotated the token mid-run; refetch once and carry on."""
    respx.post(TOKEN_URL).mock(
        side_effect=[_token_response(OLD_TOKEN), _token_response(NEW_TOKEN)]
    )
    graphql = respx.post(GRAPHQL_URL).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"data": {"ok": True}}),
        ]
    )

    data = await ShopifyAdminClient(SHOP, TokenProvider()).execute("query { ok }")

    assert data == {"ok": True}
    assert graphql.call_count == 2
    # The retry must use the *new* token, not replay the rejected one.
    assert graphql.calls[1].request.headers["X-Shopify-Access-Token"] == NEW_TOKEN


@respx.mock
async def test_gives_up_after_a_second_401():
    """A freshly-minted token that is still rejected is fatal, not an infinite loop."""
    respx.post(TOKEN_URL).mock(
        side_effect=[_token_response(OLD_TOKEN), _token_response(NEW_TOKEN)]
    )
    graphql = respx.post(GRAPHQL_URL).mock(return_value=httpx.Response(401))

    with pytest.raises(ShopifyAdminError):
        await ShopifyAdminClient(SHOP, TokenProvider()).execute("query { ok }")

    assert graphql.call_count == 2


@respx.mock
async def test_surfaces_graphql_errors():
    respx.post(TOKEN_URL).mock(return_value=_token_response(OLD_TOKEN))
    respx.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "bad field"}]})
    )

    with pytest.raises(ShopifyAdminError, match="bad field"):
        await ShopifyAdminClient(SHOP, TokenProvider()).execute("query { ok }")


@respx.mock
async def test_iter_products_follows_pagination():
    respx.post(TOKEN_URL).mock(return_value=_token_response(OLD_TOKEN))
    respx.post(GRAPHQL_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": {
                        "products": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                            "nodes": [{"id": "gid://shopify/Product/1"}],
                        }
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": {
                        "products": {
                            "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
                            "nodes": [{"id": "gid://shopify/Product/2"}],
                        }
                    }
                },
            ),
        ]
    )

    pages = [
        (nodes, cursor)
        async for nodes, cursor in ShopifyAdminClient(SHOP, TokenProvider()).iter_products()
    ]

    assert [cursor for _, cursor in pages] == ["cursor-1", "cursor-2"]
    assert [nodes[0]["id"] for nodes, _ in pages] == [
        "gid://shopify/Product/1",
        "gid://shopify/Product/2",
    ]
