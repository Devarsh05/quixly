"""Shopify Admin GraphQL client.

Tokens come exclusively from ``TokenProvider``. On a 401 the client refetches the token
once (the app shell may have rotated it mid-run) and retries; a second 401 is fatal.

Shopify's Admin API is rate-limited by a leaky-bucket *cost* model, not a request count,
so the client backs off on ``THROTTLED`` using the ``throttleStatus`` the API returns.
"""

import asyncio
import logging
from typing import Any

import httpx

from app.services.token_provider import TokenProvider
from app.settings import get_settings

logger = logging.getLogger(__name__)

MAX_THROTTLE_RETRIES = 5

PRODUCTS_QUERY = """
query CatalogPage($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      descriptionHtml
      status
      variants(first: 100) {
        nodes { id title sku barcode price inventoryQuantity }
      }
      metafields(first: 50) {
        nodes { namespace key value type }
      }
    }
  }
}
"""


class ShopifyAdminError(RuntimeError):
    """A non-recoverable error talking to the Shopify Admin API."""


class ShopifyAdminClient:
    """Async Admin GraphQL client for one shop."""

    def __init__(
        self,
        shop_domain: str,
        token_provider: TokenProvider,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._shop_domain = shop_domain
        self._tokens = token_provider
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._api_version = get_settings().shopify_api_version

    @property
    def _endpoint(self) -> str:
        return f"https://{self._shop_domain}/admin/api/{self._api_version}/graphql.json"

    async def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL document, refreshing the token once on 401 and backing off on throttle."""
        for attempt in range(MAX_THROTTLE_RETRIES):
            # Re-read the token every call rather than capturing it once: a long catalog
            # ingest can outlive a 60-minute token, and the cache hides the cost.
            token = await self._tokens.get_token(self._shop_domain)
            response = await self._post(token, query, variables)

            if response.status_code == 401:
                logger.info("401 from Shopify for %s — refetching token once", self._shop_domain)
                token = await self._tokens.get_token(self._shop_domain, force_refresh=True)
                response = await self._post(token, query, variables)
                if response.status_code == 401:
                    raise ShopifyAdminError(
                        f"Shopify rejected a freshly-refreshed token for {self._shop_domain}."
                    )

            if response.status_code == 429:
                await self._backoff(attempt)
                continue

            if response.status_code != 200:
                raise ShopifyAdminError(
                    f"Shopify Admin API returned {response.status_code} for {self._shop_domain}."
                )

            payload = response.json()

            if self._is_throttled(payload):
                await self._backoff(attempt, payload)
                continue

            if errors := payload.get("errors"):
                raise ShopifyAdminError(f"GraphQL errors for {self._shop_domain}: {errors}")

            return payload["data"]

        raise ShopifyAdminError(
            f"Still throttled by Shopify after {MAX_THROTTLE_RETRIES} attempts "
            f"for {self._shop_domain}."
        )

    async def iter_products(self, cursor: str | None = None):
        """Yield ``(products, end_cursor)`` one page at a time, resuming from ``cursor``."""
        while True:
            data = await self.execute(PRODUCTS_QUERY, {"cursor": cursor})
            connection = data["products"]
            page_info = connection["pageInfo"]
            cursor = page_info["endCursor"]

            yield connection["nodes"], cursor

            if not page_info["hasNextPage"]:
                return

    async def _post(
        self, token: str, query: str, variables: dict[str, Any] | None
    ) -> httpx.Response:
        return await self._client.post(
            self._endpoint,
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables or {}},
        )

    @staticmethod
    def _is_throttled(payload: dict[str, Any]) -> bool:
        return any(
            error.get("extensions", {}).get("code") == "THROTTLED"
            for error in payload.get("errors") or []
        )

    @staticmethod
    async def _backoff(attempt: int, payload: dict[str, Any] | None = None) -> None:
        """Wait long enough for the leaky bucket to refill, else exponential backoff."""
        delay = 2**attempt
        if payload:
            throttle = (payload.get("extensions") or {}).get("cost", {}).get("throttleStatus", {})
            restore_rate = throttle.get("restoreRate")
            requested = (payload.get("extensions") or {}).get("cost", {}).get("requestedQueryCost")
            available = throttle.get("currentlyAvailable")
            if restore_rate and requested is not None and available is not None:
                needed = max(requested - available, 0)
                delay = max(needed / restore_rate, 1)
        logger.info("Throttled by Shopify; backing off %.1fs", delay)
        await asyncio.sleep(delay)
