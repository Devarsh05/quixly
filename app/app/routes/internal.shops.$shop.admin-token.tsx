/**
 * Internal route: hand the agent a short-lived Shopify admin access token.
 *
 * The app shell is the SINGLE refresh authority for a shop's offline token. Offline
 * tokens expire after ~60 minutes, and minting a new one retires the previous token and
 * invalidates its refresh token immediately — so a second refresher would silently break
 * this one. The agent never holds a refresh token and never persists an access token.
 *
 * Status codes are load-bearing — the agent classifies permanence by them:
 *   404 → PERMANENT. No session, or the refresh chain is dead (expired/rotated refresh
 *         token, i.e. the 90-day-idle case). The agent flags the shop reauth_required.
 *   502 → TRANSIENT. Shopify unreachable, 5xx, throttled. The agent retries.
 * Both "no session row" and "invalid_grant" MUST return 404: they are equally permanent,
 * and mapping the latter to 502 would retry a dead shop forever.
 *
 * Guarded by INTERNAL_API_KEY. Never link this route from the UI; never expose it
 * publicly.
 */

import type { ActionFunctionArgs } from "react-router";

import { getAdminToken, ReauthRequiredError } from "../lib/admin-token.server";
import { requireInternalApiKey } from "../lib/internal-auth.server";

export const action = async ({ request, params }: ActionFunctionArgs) => {
  // Throws a 401 Response. Deliberately outside the try/catch below, so an auth failure
  // is never mistaken for a missing session.
  requireInternalApiKey(request);

  const shop = params.shop;
  if (!shop) {
    return Response.json({ error: "Missing shop" }, { status: 400 });
  }

  try {
    const { accessToken, expiresAt } = await getAdminToken(shop);

    return Response.json({
      access_token: accessToken,
      expires_at: expiresAt ? expiresAt.toISOString() : null,
    });
  } catch (error) {
    if (error instanceof ReauthRequiredError) {
      console.error(`Re-auth required for ${shop}: ${error.message}`);
      return Response.json({ error: "Re-auth required", reauth_required: true }, { status: 404 });
    }

    // Transient only. Must NOT read as 404, or the agent would permanently flag a
    // healthy shop for re-auth on a momentary blip.
    console.error(`Failed to mint an admin token for ${shop}:`, error);
    return Response.json({ error: "Token fetch failed" }, { status: 502 });
  }
};
