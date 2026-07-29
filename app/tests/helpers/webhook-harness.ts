/**
 * Shared rig for the webhook-refresh tests.
 *
 * These tests run the REAL `authenticate.webhook()` against a REAL Postgres advisory lock
 * and the REAL PrismaSessionStorage, on a deliberately tiny Prisma connection pool. Only two
 * things are faked, and they are faked separately on purpose:
 *
 *   - `refreshTokenSpy` stands in for `api.auth.refreshToken`, which is the ONLY refresh
 *     `admin-token.server.ts` performs. Its call count is *our* refresh count.
 *   - `libraryFetch` replaces `globalThis.fetch`, which is how the LIBRARY's own refresh
 *     (`ensure-offline-token-is-not-expired.js` → `refresh-token.js`) reaches Shopify. Its
 *     call count is the library's refresh count, and it must stay at zero.
 *
 * Keeping the two apart is the whole point: "the library never refreshes" is an invariant we
 * assert directly, not something we infer from a blended total.
 *
 * `libraryFetch` answers every call with a 400 `invalid_grant`, so a library refresh that
 * slips through fails loudly instead of silently succeeding.
 */

import { createHmac } from "node:crypto";

import { Session } from "@shopify/shopify-api";
import { vi } from "vitest";

export const API_KEY = "test-webhook-api-key";
export const API_SECRET = "test-webhook-api-secret";
export const APP_URL = "https://webhook-harness.test";

const NINETY_DAYS_MS = 90 * 24 * 60 * 60 * 1000;

/** Our refresh path: `api.auth.refreshToken`, called only from `admin-token.server.ts`. */
export const refreshTokenSpy = vi.fn();

/**
 * The library's refresh path. A 400 `invalid_grant` is what Shopify returns for a dead
 * grant, and it is what `refresh-token.js` turns into an opaque 500 — so if the library ever
 * does refresh, the test sees both the call and the failure.
 */
export const libraryFetch = vi.fn(
  async () =>
    new Response(JSON.stringify({ error: "invalid_grant" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    }),
);

/**
 * Must run BEFORE anything imports `@shopify/shopify-app-react-router/server`.
 *
 * That module requires `@shopify/shopify-api/adapters/web-api`, which does
 * `setAbstractFetchFunc(fetch)` — capturing the global binding **by value** at import time.
 * A stub installed afterwards would never be seen, and the library's refresh would make a
 * real network call to a shop domain that does not exist.
 */
export function installFetchStub(): void {
  vi.stubGlobal("fetch", libraryFetch);
}

/** Build the `shopify.server` replacement: real `authenticate`, faked `api.auth`. */
export async function buildShopifyServerMock() {
  const { ApiVersion, AppDistribution, shopifyApp } = await import(
    "@shopify/shopify-app-react-router/server"
  );
  await import("@shopify/shopify-app-react-router/adapters/node");
  const { PrismaSessionStorage } = await import("@shopify/shopify-app-session-storage-prisma");
  const { default: client } = await import("../../app/db.server");

  const shopify = shopifyApp({
    apiKey: API_KEY,
    apiSecretKey: API_SECRET,
    apiVersion: ApiVersion.July26,
    appUrl: APP_URL,
    sessionStorage: new PrismaSessionStorage(client),
    distribution: AppDistribution.AppStore,
    // The flag under which `ensureValidOfflineSession` refreshes at all. Without it the
    // library's refresh branch is dead code and these tests would prove nothing.
    future: { expiringOfflineAccessTokens: true },
    logger: { log: () => {} },
  });

  return {
    authenticate: shopify.authenticate,
    sessionStorage: shopify.sessionStorage,
    api: {
      session: { getOfflineId: (shop: string) => `offline_${shop}` },
      auth: { refreshToken: (args: unknown) => refreshTokenSpy(args) },
    },
  };
}

/**
 * `DATABASE_URL` with an explicit Prisma pool cap.
 *
 * The cap is the experiment: a critical section that needs a second connection cannot
 * complete on a pool that cannot supply one. `pool_timeout` is kept short so a starved
 * request fails fast instead of stalling the suite.
 */
export function poolUrl(connectionLimit: number, poolTimeoutSeconds: number): string {
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error("DATABASE_URL is required — these tests need a real Postgres.");
  }
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}connection_limit=${connectionLimit}&pool_timeout=${poolTimeoutSeconds}`;
}

export function makeSession({
  shop,
  accessToken,
  refreshToken,
  expiresInMs,
  refreshTokenExpiresInMs = NINETY_DAYS_MS,
}: {
  shop: string;
  accessToken: string;
  refreshToken: string;
  expiresInMs: number;
  refreshTokenExpiresInMs?: number;
}): Session {
  return new Session({
    id: `offline_${shop}`,
    shop,
    state: "",
    isOnline: false,
    accessToken,
    scope: "write_products",
    expires: new Date(Date.now() + expiresInMs),
    refreshToken,
    refreshTokenExpires: new Date(Date.now() + refreshTokenExpiresInMs),
  });
}

/**
 * A webhook POST signed exactly the way Shopify signs one: base64 HMAC-SHA256 of the raw
 * body under the app secret. Pass `hmac` to forge it.
 */
export function signedWebhookRequest({
  shop,
  topic = "products/update",
  payload = { id: 113 },
  hmac,
}: {
  shop?: string;
  topic?: string;
  payload?: unknown;
  hmac?: string;
}): Request {
  const rawBody = JSON.stringify(payload);
  const signature =
    hmac ?? createHmac("sha256", API_SECRET).update(rawBody, "utf8").digest("base64");

  const headers = new Headers({
    "Content-Type": "application/json",
    "X-Shopify-Hmac-Sha256": signature,
    "X-Shopify-Topic": topic,
    "X-Shopify-Api-Version": "2026-07",
    "X-Shopify-Webhook-Id": "webhook-harness-delivery",
  });
  if (shop) {
    headers.set("X-Shopify-Shop-Domain", shop);
  }

  return new Request(`${APP_URL}/webhooks`, { method: "POST", headers, body: rawBody });
}
