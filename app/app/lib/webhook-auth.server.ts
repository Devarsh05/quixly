/**
 * `authenticate.webhook()` with the shop's token already refreshed under the rotation lock.
 *
 * Webhook authentication loads the offline session via the library's
 * `ensureValidOfflineSession()`, which **refreshes the token** if it is within 5 minutes of
 * expiry. That makes every webhook route a token-rotation path, not just a read.
 *
 * Webhooks arrive asynchronously with no merchant present, at the same time as the agent's
 * headless jobs are calling /internal/shops/:shop/admin-token. Unserialized, those two can
 * rotate concurrently and invalidate each other's refresh token, permanently breaking the
 * chain. So the refresh has to be serialized — but it cannot be serialized by wrapping
 * `authenticate.webhook()` in the lock, which is what this module used to do.
 *
 * WHY NOT WRAP IT. `withShopRefreshLock` holds `pg_advisory_xact_lock` inside a
 * `prisma.$transaction`, which pins one pooled connection for the whole critical section. The
 * library reaches session storage through the client bound at `shopifyApp()` construction —
 * the GLOBAL one — and there is no seam to inject a transaction-bound storage into
 * `authenticate.webhook(request)`. So the critical section needed a SECOND connection, and
 * `createOrLoadOfflineSession` takes it unconditionally, before any expiry check: *every*
 * webhook, not only the refreshing ones. With a pool no larger than the number of concurrent
 * same-shop webhooks, every connection is held by a transaction waiting on the lock and the
 * winner cannot get its second one. On a 1-vCPU container Prisma's default pool is 3, and
 * `products/update` fans out on bulk merchant edits — and on our own publishes.
 *
 * WHAT THIS DOES INSTEAD. Refresh first, through `ensureFreshOfflineSession` — the same
 * authority and the same tx-pinned critical section `getAdminToken` uses, so the section
 * borrows no second connection — then run `authenticate.webhook()` with no transaction open
 * at all. The library finds a fresh token and its own refresh branch never fires.
 *
 * "Never fires" is by construction, not by luck: WEBHOOK_REFRESH_WINDOW_MS is deliberately
 * wider than the library's 5-minute threshold. Either we refreshed (≈60 minutes of headroom)
 * or the token had at least WEBHOOK_REFRESH_WINDOW_MS left a moment ago — and the lock's own
 * `maxWait` + `timeout` cap that moment at ~35 seconds. `webhook-refresh-single-connection`
 * pins the invariant by probing the library's real threshold, because it is an internal
 * constant that a dependency bump could move.
 *
 * HMAC IS UNTOUCHED. `authenticate.webhook()` still runs, exactly once, on the unmodified
 * request: it verifies the HMAC, rejects with 401/400/405, normalizes the topic to
 * `topicForStorage()` form (`PRODUCTS_UPDATE`), and builds the context. This module never
 * validates a webhook itself and never re-reads the body.
 *
 * THE PRE-HMAC TRADE-OFF, accepted deliberately. The lock key and now the refresh are keyed on
 * X-Shopify-Shop-Domain, which is read BEFORE the HMAC is verified — it has to be, because
 * verification happens inside the library call. The value picks a shop to refresh; it never
 * authorizes anything and nothing is returned to the caller. A forged request cannot raise a
 * shop's refresh RATE: a refresh only happens inside the window, and one rotation pushes the
 * token ~60 minutes out, so every subsequent forgery is a no-op read. The most it can do is
 * advance a refresh that was already due within WEBHOOK_REFRESH_WINDOW_MS, which rules out
 * exhausting Shopify's OAuth rate limit to starve legitimate refreshes.
 */

import type { authenticate as authenticateType } from "../shopify.server";
import { authenticate } from "../shopify.server";
import { ensureFreshOfflineSession, ReauthRequiredError } from "./admin-token.server";

type WebhookContext = Awaited<ReturnType<typeof authenticateType.webhook>>;

/**
 * How much validity the token must have left before we hand the request to the library.
 *
 * Strictly wider than the library's own 5-minute threshold, by enough that no plausible delay
 * between our check and its check can close the gap. Narrowing this below 5 minutes puts the
 * library's refresh — which runs outside the lock — back in play.
 */
export const WEBHOOK_REFRESH_WINDOW_MS = 10 * 60 * 1000;

export async function authenticateWebhookSerialized(request: Request): Promise<WebhookContext> {
  const shop = request.headers.get("x-shopify-shop-domain");

  if (!shop) {
    // No shop to key a lock on. Let the library reject it — an unsigned/malformed request
    // never reaches the refresh path anyway.
    return authenticate.webhook(request);
  }

  try {
    await ensureFreshOfflineSession(shop, WEBHOOK_REFRESH_WINDOW_MS);
  } catch (error) {
    if (!(error instanceof ReauthRequiredError)) {
      // Transient. Do NOT continue: the token is still near expiry, so the library would
      // refresh it outside the lock and race the agent's token route. Shopify redelivers.
      console.error(`Could not refresh ${shop}'s offline token for a webhook:`, error);
      throw new Response("Token refresh unavailable", { status: 503 });
    }
    // Permanent — the chain is dead and no retry fixes it. We cannot answer the request here
    // (the HMAC is still unverified), so hand it to the library. Its refresh will fail and
    // 500, which is what happened before this fix too; a dead chain cannot be damaged further.
    console.warn(`${shop} needs to re-authenticate; passing the webhook through:`, error);
  }

  return authenticate.webhook(request);
}
