/**
 * Internal route: hand the agent a short-lived Shopify admin access token.
 *
 * The app shell is the SINGLE refresh authority for a shop's offline token. Offline
 * tokens expire after ~60 minutes, and minting a new one retires the previous token and
 * invalidates its refresh token immediately — so a second refresher would silently break
 * this one. `unauthenticated.admin()` refreshes the session when it is within 5 minutes
 * of expiry and persists the rotation to session storage; the agent never holds a
 * refresh token and never persists an access token.
 *
 * Guarded by INTERNAL_API_KEY. Never link this route from the UI; never expose it
 * publicly.
 */

import type { ActionFunctionArgs } from "react-router";
import { SessionNotFoundError } from "@shopify/shopify-app-react-router/server";

import { requireInternalApiKey } from "../lib/internal-auth.server";
import { unauthenticated } from "../shopify.server";

export const action = async ({ request, params }: ActionFunctionArgs) => {
  // Throws a 401 Response. Deliberately outside the try/catch below, so an auth failure
  // is never mistaken for a missing session.
  requireInternalApiKey(request);

  const shop = params.shop;
  if (!shop) {
    return Response.json({ error: "Missing shop" }, { status: 400 });
  }

  try {
    const { session } = await unauthenticated.admin(shop);

    return Response.json({
      access_token: session.accessToken,
      expires_at: session.expires ? session.expires.toISOString() : null,
    });
  } catch (error) {
    if (error instanceof SessionNotFoundError) {
      // Never installed, uninstalled, or the refresh chain lapsed (refresh tokens die
      // after 90 days of disuse). Permanent: the agent surfaces this as "re-auth
      // required" rather than retrying.
      return Response.json({ error: "No session for shop" }, { status: 404 });
    }

    // Anything else (Shopify unreachable, refresh call failed) is transient. It must NOT
    // read as 404, or the agent would permanently flag a healthy shop for re-auth.
    console.error(`Failed to mint an admin token for ${shop}:`, error);
    return Response.json({ error: "Token fetch failed" }, { status: 502 });
  }
};
