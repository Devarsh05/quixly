"""TokenProvider: the agent's only source of Shopify admin tokens.

These tests pin the behaviours that keep the app shell as the SINGLE refresh authority
and keep credentials out of the logs.
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.services.token_provider import (
    TokenFetchError,
    TokenProvider,
    TokenUnavailableError,
)

SHOP = "quixly-dev.myshopify.com"
TOKEN_URL = f"http://app-shell.test/internal/shops/{SHOP}/admin-token"
SECRET_TOKEN = "shpat_super_secret_value"


def _token_response(minutes: int = 60) -> httpx.Response:
    expires_at = datetime.now(UTC) + timedelta(minutes=minutes)
    return httpx.Response(
        200,
        json={"access_token": SECRET_TOKEN, "expires_at": expires_at.isoformat()},
    )


@respx.mock
async def test_fetches_token_from_app_shell():
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())

    token = await TokenProvider().get_token(SHOP)

    assert token == SECRET_TOKEN
    assert route.call_count == 1


@respx.mock
async def test_second_call_is_served_from_cache():
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())
    provider = TokenProvider()

    first = await provider.get_token(SHOP)
    second = await provider.get_token(SHOP)

    assert first == second == SECRET_TOKEN
    # The app shell is the single refresh authority — hammering it on every Admin call
    # would be both wasteful and a rotation hazard.
    assert route.call_count == 1


@respx.mock
async def test_force_refresh_bypasses_the_cache():
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())
    provider = TokenProvider()

    await provider.get_token(SHOP)
    await provider.get_token(SHOP, force_refresh=True)

    # This is what the 401 path uses: Shopify rejected the cached token, so the cache
    # must be bypassed rather than handing back the same dead value.
    assert route.call_count == 2


@respx.mock
async def test_near_expiry_token_is_not_cached():
    """A token inside the safety margin must never be served from cache."""
    route = respx.post(TOKEN_URL).mock(return_value=_token_response(minutes=2))
    provider = TokenProvider()

    await provider.get_token(SHOP)
    await provider.get_token(SHOP)

    assert route.call_count == 2


@respx.mock
async def test_404_is_permanent():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(404, json={"error": "no session"}))

    with pytest.raises(TokenUnavailableError):
        await TokenProvider().get_token(SHOP)


@respx.mock
async def test_502_is_transient_not_permanent():
    """A flaky app shell must not be mistaken for an uninstalled shop.

    TokenUnavailableError flags the shop for re-auth; a transient error must not, or a
    healthy store would be permanently marked broken by one blip.
    """
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(502))

    with pytest.raises(TokenFetchError):
        await TokenProvider().get_token(SHOP)


@respx.mock
async def test_network_error_is_transient():
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(TokenFetchError):
        await TokenProvider().get_token(SHOP)


@respx.mock
async def test_invalidate_drops_the_cached_token():
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())
    provider = TokenProvider()

    await provider.get_token(SHOP)
    await provider.invalidate(SHOP)
    await provider.get_token(SHOP)

    assert route.call_count == 2


@respx.mock
async def test_token_never_appears_in_logs(caplog):
    """The access token must not be recoverable from log output."""
    respx.post(TOKEN_URL).mock(return_value=_token_response())

    with caplog.at_level(logging.DEBUG):
        token = await TokenProvider().get_token(SHOP)

    assert token == SECRET_TOKEN
    assert SECRET_TOKEN not in caplog.text


@respx.mock
async def test_token_never_appears_in_error_messages(caplog):
    """Nor from an exception raised on a failed fetch."""
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(500, text=SECRET_TOKEN))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TokenFetchError) as exc_info:
            await TokenProvider().get_token(SHOP)

    assert SECRET_TOKEN not in str(exc_info.value)
    assert SECRET_TOKEN not in caplog.text


async def test_settings_never_expose_the_internal_key_in_a_repr(settings):
    """SecretStr keeps the shared secret out of tracebacks and debug dumps."""
    assert "test-internal-key" not in repr(settings)
