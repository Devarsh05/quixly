/**
 * `authenticate.webhook()` under the shop's rotation lock.
 *
 * Webhook authentication loads the offline session via the library's
 * `ensureValidOfflineSession()`, which **refreshes the token** if it is within 5 minutes of
 * expiry. That makes every webhook route a token-rotation path, not just a read.
 *
 * Webhooks arrive asynchronously with no merchant present, at the same time as the agent's
 * headless jobs are calling /internal/shops/:shop/admin-token. Unserialized, those two can
 * rotate concurrently and invalidate each other's refresh token, permanently breaking the
 * chain. Holding the same per-shop lock makes them take turns instead.
 *
 * The lock key comes from the X-Shopify-Shop-Domain header, which is read BEFORE the HMAC
 * is verified. That is safe: the value is used only to pick a lock, never to authorize
 * anything. A forged header can at worst contend on the wrong lock; it cannot authenticate.
 */

import type { authenticate as authenticateType } from "../shopify.server";
import { authenticate } from "../shopify.server";
import { withShopRefreshLock } from "./shop-lock.server";

type WebhookContext = Awaited<ReturnType<typeof authenticateType.webhook>>;

export async function authenticateWebhookSerialized(request: Request): Promise<WebhookContext> {
  const shop = request.headers.get("x-shopify-shop-domain");

  if (!shop) {
    // No shop to key a lock on. Let the library reject it — an unsigned/malformed request
    // never reaches the refresh path anyway.
    return authenticate.webhook(request);
  }

  return withShopRefreshLock(shop, () => authenticate.webhook(request));
}
