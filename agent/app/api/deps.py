"""Shared FastAPI dependencies.

The internal API is the app shell's only way into the agent. It is authenticated with a
shared secret (``INTERNAL_API_KEY``) rather than OAuth, because both services are ours
and the call is service-to-service. It must never be reachable from the public internet.
"""

import hmac

from fastapi import Header, HTTPException, status

from app.settings import get_settings

INTERNAL_API_KEY_HEADER = "X-Internal-Api-Key"


async def require_internal_api_key(
    x_internal_api_key: str | None = Header(default=None, alias=INTERNAL_API_KEY_HEADER),
) -> None:
    """Reject any request that does not carry the shared secret.

    Uses a constant-time comparison so the key cannot be recovered by timing the
    response. A missing key is rejected the same way a wrong one is.
    """
    expected = get_settings().internal_api_key.get_secret_value()

    if not expected:
        # Fail closed: an unset key must not mean "allow everything".
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="INTERNAL_API_KEY is not configured on the agent service.",
        )

    if not x_internal_api_key or not hmac.compare_digest(x_internal_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal API key.",
        )
