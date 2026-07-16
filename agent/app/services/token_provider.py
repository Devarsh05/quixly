"""The agent's single source of Shopify admin access tokens.

Why this exists
---------------
Shopify offline access tokens now expire after ~60 minutes. Obtaining a new one
**retires the previous token and invalidates its refresh token immediately**, so there
can be exactly ONE refresh authority for a shop. The app shell already is one: its
``unauthenticated.admin(shop)`` refreshes within 5 minutes of expiry and persists the
rotation to the Prisma session store.

Therefore the agent holds no refresh token and persists no access token. It asks the app
shell for a short-lived token and caches it in Redis. **Every** Admin API call goes
through this class — a second code path that fetches tokens would be a second refresh
authority, which would silently break the first.

The token is never logged, never placed in an exception message, and never included in a
repr.
"""

import logging
from datetime import UTC, datetime

import httpx

from app.redis import get_redis
from app.settings import get_settings

logger = logging.getLogger(__name__)

# Mirrors the app shell's own refresh window: never serve a token from cache that is
# within 5 minutes of expiring.
SAFETY_MARGIN_SECONDS = 5 * 60

# Floor for the cache TTL, so a token that is already near expiry isn't cached at all.
MIN_TTL_SECONDS = 30


class TokenUnavailableError(RuntimeError):
    """PERMANENT: this shop has no usable session and must re-authenticate.

    Raised only when the app shell reports 404 — the shop is uninstalled, was never
    installed, or its refresh chain has lapsed (refresh tokens die after 90 days of
    disuse). Callers should surface this as "re-auth required" rather than retrying, and
    never let it degrade into a silent 401.
    """


class TokenFetchError(RuntimeError):
    """TRANSIENT: the app shell was unreachable or errored.

    Distinct from TokenUnavailableError on purpose. A network blip or a restarting app
    shell must NOT flag a perfectly healthy shop as needing re-auth; these are retried.
    """


def _cache_key(shop_domain: str) -> str:
    return f"admin_token:{shop_domain}"


class TokenProvider:
    """Fetches and caches short-lived Shopify admin tokens from the app shell."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._settings = get_settings()
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def get_token(self, shop_domain: str, *, force_refresh: bool = False) -> str:
        """Return a valid admin access token, from cache when possible.

        ``force_refresh`` bypasses the cache — used after a 401 to discard a token that
        Shopify rejected before the cache TTL expected it to.
        """
        redis = get_redis()

        if not force_refresh:
            cached = await redis.get(_cache_key(shop_domain))
            if cached:
                return cached

        token, expires_at = await self._fetch_from_app_shell(shop_domain)

        ttl = self._ttl_for(expires_at)
        if ttl >= MIN_TTL_SECONDS:
            await redis.set(_cache_key(shop_domain), token, ex=ttl)

        return token

    async def invalidate(self, shop_domain: str) -> None:
        """Drop the cached token (e.g. on uninstall)."""
        await get_redis().delete(_cache_key(shop_domain))

    def _ttl_for(self, expires_at: datetime | None) -> int:
        if expires_at is None:
            # No expiry reported — cache conservatively rather than indefinitely.
            return SAFETY_MARGIN_SECONDS
        lifetime = (expires_at - datetime.now(UTC)).total_seconds()
        return int(lifetime - SAFETY_MARGIN_SECONDS)

    async def _fetch_from_app_shell(self, shop_domain: str) -> tuple[str, datetime | None]:
        url = f"{self._settings.app_shell_url}/internal/shops/{shop_domain}/admin-token"
        try:
            response = await self._client.post(
                url,
                headers={"X-Internal-Api-Key": self._settings.internal_api_key.get_secret_value()},
            )
        except httpx.HTTPError as exc:
            # str(exc) carries the URL but never the token — the token only ever exists
            # in the response body, which we never interpolate into an error.
            raise TokenFetchError(
                f"Could not reach the app shell for {shop_domain}: {exc}"
            ) from exc

        if response.status_code == 404:
            # The only permanent answer. Everything else is worth retrying.
            raise TokenUnavailableError(
                f"No Shopify session for {shop_domain} — the shop is uninstalled or must re-auth."
            )
        if response.status_code != 200:
            raise TokenFetchError(
                f"App shell returned {response.status_code} fetching a token for {shop_domain}."
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise TokenFetchError(f"App shell returned no token for {shop_domain}.")

        expires_at = None
        if raw_expiry := payload.get("expires_at"):
            expires_at = datetime.fromisoformat(raw_expiry)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)

        logger.info("Fetched admin token for %s (expires %s)", shop_domain, expires_at)
        return token, expires_at
