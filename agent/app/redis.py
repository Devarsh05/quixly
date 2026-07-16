"""Shared Redis client.

Used for the TokenProvider's short-lived access-token cache and for the per-shop
ingest lock that keeps concurrent /shops/connect calls from double-enqueueing.
"""

from redis.asyncio import Redis

from app.settings import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    """Return the process-wide Redis client (lazily created)."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    """Close the client on shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


# --- Per-shop ingest lock -------------------------------------------------------------
# /shops/connect takes this lock and the ingest job releases it. Defined here so the two
# sides can never disagree about the key. The TTL is a safety net: if a worker dies
# mid-ingest the lock expires rather than wedging the shop forever.

INGEST_LOCK_TTL_SECONDS = 60 * 60


def ingest_lock_key(shop_domain: str) -> str:
    return f"ingest_lock:{shop_domain}"


async def acquire_ingest_lock(shop_domain: str, run_id: int) -> int | None:
    """Claim the ingest lock for ``shop_domain``.

    Returns ``None`` if the lock was acquired, or the *existing* run_id if an ingest is
    already in flight — the caller should hand that back rather than enqueueing a second.
    """
    redis = get_redis()
    key = ingest_lock_key(shop_domain)
    acquired = await redis.set(key, str(run_id), nx=True, ex=INGEST_LOCK_TTL_SECONDS)
    if acquired:
        return None
    existing = await redis.get(key)
    return int(existing) if existing else None


async def release_ingest_lock(shop_domain: str) -> None:
    await get_redis().delete(ingest_lock_key(shop_domain))
