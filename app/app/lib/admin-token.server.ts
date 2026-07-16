/**
 * Mint a short-lived Shopify admin access token for the agent.
 *
 * Why this refreshes explicitly instead of calling `unauthenticated.admin()`:
 *
 * `unauthenticated.admin()` refreshes via the library's `helpers/refresh-token.js`, which
 * rethrows only InvalidJwtError and HttpResponseError(400, "invalid_subject_token").
 * Every other failure — including a 400 `invalid_grant`, which is what an OAuth token
 * endpoint returns when a refresh token is expired or already rotated — is flattened into
 * an anonymous `Response(500)`. That makes a permanently dead refresh chain
 * indistinguishable from a transient network blip, so a 90-day-idle shop would be retried
 * forever and never surfaced to the merchant.
 *
 * The refresh request and session creation stay library-owned (`api.auth.refreshToken`);
 * all this module adds is the permanent-vs-transient classification. The app shell remains
 * the SINGLE refresh authority — this *is* the app shell, and this is the only place a
 * refresh happens.
 */

import type { Prisma, PrismaClient } from "@prisma/client";
import type { Session } from "@shopify/shopify-api";
import { HttpResponseError, InvalidJwtError } from "@shopify/shopify-api";
import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";

import { withShopRefreshLock } from "./shop-lock.server";
import { api, sessionStorage } from "../shopify.server";

/** Mirrors the library's own refresh window (helpers/ensure-offline-token-is-not-expired.js). */
const WITHIN_MILLISECONDS_OF_EXPIRY = 5 * 60 * 1000;

/**
 * OAuth errors that mean the grant is dead and no retry can fix it. Both spellings are
 * covered because Shopify's exact code for an expired refresh token is not documented:
 * `invalid_grant` is the OAuth 2.0 standard, `invalid_subject_token` is what the library
 * itself special-cases. Treating either as permanent is correct; treating either as
 * transient is not.
 */
const PERMANENT_GRANT_ERRORS = new Set([
  "invalid_grant",
  "invalid_subject_token",
  "invalid_token",
  "invalid_client",
  "unauthorized_client",
]);

/** The shop must re-authenticate. Permanent: never retry — flag it and stop. */
export class ReauthRequiredError extends Error {}

export type AdminToken = {
  accessToken: string;
  expiresAt: Date | null;
};

function isPermanentGrantFailure(error: unknown): boolean {
  if (error instanceof InvalidJwtError) return true;

  if (error instanceof HttpResponseError) {
    const { code, body } = error.response;
    const oauthError = (body as { error?: string } | undefined)?.error;
    // A 400 from the token endpoint is a rejected grant, not a server fault. A 5xx is.
    if (code === 400 && oauthError && PERMANENT_GRANT_ERRORS.has(oauthError)) return true;
  }

  return false;
}

function tokenOf(session: Session): AdminToken {
  return {
    accessToken: session.accessToken!,
    expiresAt: session.expires ?? null,
  };
}

/**
 * Returns a valid admin token for `shop`, refreshing it if it is near expiry.
 *
 * Throws ReauthRequiredError when the shop has no session or its refresh chain is dead.
 * Any other throw is transient and safe to retry.
 */
export async function getAdminToken(shop: string): Promise<AdminToken> {
  const sessionId = api.session.getOfflineId(shop);
  const session = await sessionStorage.loadSession(sessionId);

  if (!session) {
    throw new ReauthRequiredError(`No session stored for ${shop}`);
  }

  // Fast path: still valid, no rotation needed, so don't pay for the lock.
  if (!session.isExpired(WITHIN_MILLISECONDS_OF_EXPIRY)) {
    return tokenOf(session);
  }

  // Rotation must be serialized per shop: two concurrent refreshes would invalidate each
  // other's refresh token and permanently break the chain. Callers block here rather than
  // racing. See shop-lock.server.ts.
  return withShopRefreshLock(shop, (tx) => refreshUnderLock(tx, shop, sessionId));
}

/** Must only be called while holding the shop's rotation lock. */
async function refreshUnderLock(
  tx: Prisma.TransactionClient,
  shop: string,
  sessionId: string,
): Promise<AdminToken> {
  // Read and write through the lock's own transaction connection, never the global client.
  // The advisory lock is held for the whole transaction; if the inner I/O borrowed a second
  // pooled connection, N concurrent callers could exhaust the pool with lock-holders and
  // deadlock the one that needs the extra connection. Cast: the adapter is generic over
  // <T extends PrismaClient> and only touches `.session`, which TransactionClient has, but
  // TransactionClient is not assignable to PrismaClient (it omits $transaction/$connect/…).
  const txStorage = new PrismaSessionStorage(tx as unknown as PrismaClient);

  // Re-read AFTER acquiring the lock. Whoever held it before us may have just rotated the
  // token, in which case there is nothing to do — this is what collapses N concurrent
  // callers into exactly one Shopify refresh.
  const session = await txStorage.loadSession(sessionId);

  if (!session) {
    throw new ReauthRequiredError(`No session stored for ${shop}`);
  }

  if (!session.isExpired(WITHIN_MILLISECONDS_OF_EXPIRY)) {
    return tokenOf(session);
  }

  if (!session.refreshToken) {
    // An expired access token with nothing to refresh from is terminal.
    throw new ReauthRequiredError(`Session for ${shop} is expired and has no refresh token`);
  }

  // Refresh tokens die after 90 days of disuse. When we can already see the chain has
  // lapsed, say so directly rather than making Shopify tell us — it is the same permanent
  // answer, and it does not depend on which OAuth error code Shopify happens to return.
  if (session.refreshTokenExpires && session.refreshTokenExpires.getTime() <= Date.now()) {
    throw new ReauthRequiredError(`Refresh token for ${shop} expired; re-auth required`);
  }

  let refreshed;
  try {
    ({ session: refreshed } = await api.auth.refreshToken({
      shop,
      refreshToken: session.refreshToken,
    }));
  } catch (error) {
    if (isPermanentGrantFailure(error)) {
      throw new ReauthRequiredError(`Refresh chain for ${shop} is dead; re-auth required`);
    }
    // Transient (5xx, network, throttling): let the caller retry.
    throw error;
  }

  // Persist the rotation before releasing the lock. The old token — and its refresh token —
  // died the instant this one was issued, so losing this write would strand the shop.
  await txStorage.storeSession(refreshed);

  return tokenOf(refreshed);
}
