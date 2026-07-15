import "@shopify/shopify-app-react-router/adapters/node";
import {
  ApiVersion,
  AppDistribution,
  shopifyApp,
} from "@shopify/shopify-app-react-router/server";
import { shopifyApi } from "@shopify/shopify-api";
import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";
import prisma from "./db.server";
import { connectShop } from "./lib/agent.server";

// Shared so the app and the API client below cannot drift apart on the values that
// govern OAuth.
const API_KEY = process.env.SHOPIFY_API_KEY || "";
const API_SECRET = process.env.SHOPIFY_API_SECRET || "";
const APP_URL = process.env.SHOPIFY_APP_URL || "";
const SCOPES = process.env.SCOPES?.split(",");
const EXPIRING_OFFLINE_ACCESS_TOKENS = true;

const shopify = shopifyApp({
  apiKey: API_KEY,
  apiSecretKey: API_SECRET,
  apiVersion: ApiVersion.July26,
  scopes: SCOPES,
  appUrl: APP_URL,
  authPathPrefix: "/auth",
  sessionStorage: new PrismaSessionStorage(prisma),
  distribution: AppDistribution.AppStore,
  future: {
    // Offline tokens expire (~60 min) and are refreshed here, in the app shell — which
    // is therefore the SINGLE refresh authority. Minting a new token invalidates the
    // previous one's refresh token, so nothing else may refresh. The agent holds no
    // Shopify credential; it pulls short-lived tokens from
    // /internal/shops/:shop/admin-token. Do not disable this: public apps created after
    // 2026-04-01 must use expiring offline tokens.
    expiringOfflineAccessTokens: EXPIRING_OFFLINE_ACCESS_TOKENS,
  },
  hooks: {
    afterAuth: async ({ session }) => {
      // Tell the agent a shop connected; it registers the shop and enqueues catalog
      // ingestion. No token is sent — the agent asks for one when it needs one.
      //
      // Deliberately swallow failures: a throw here would abort the OAuth callback and
      // the merchant's install. Ingestion is idempotent and re-triggered on the next
      // load, so a missed call is recoverable; a broken install is not.
      try {
        await connectShop(session.shop);
      } catch (error) {
        console.error(`Failed to notify the agent that ${session.shop} connected:`, error);
      }
    },
  },
  ...(process.env.SHOP_CUSTOM_DOMAIN
    ? { customShopDomains: [process.env.SHOP_CUSTOM_DOMAIN] }
    : {}),
});

/**
 * A direct Shopify API client, used ONLY by lib/admin-token.server.ts.
 *
 * `shopifyApp()` does not expose its internal `api` object, and its `unauthenticated.admin()`
 * refresh path flattens every OAuth failure except InvalidJwtError/`invalid_subject_token`
 * into an anonymous `Response(500)` — which makes a permanently dead refresh chain
 * indistinguishable from a transient blip. We need that distinction to flag a shop
 * `reauth_required` instead of retrying it forever, so we call `auth.refreshToken()`
 * ourselves and read the real error.
 *
 * This does NOT create a second refresh authority: it is the same process, and it is the
 * only place a refresh is performed. Session creation and the refresh request itself stay
 * library-owned — we only classify the error. Config is derived from the same constants as
 * `shopifyApp()` above so the two cannot drift.
 */
// Note: `expiringOfflineAccessTokens` is deliberately NOT passed here. It is an
// *app*-level future flag (shopifyApp), not an api-level one — the api FutureFlags
// interface has no such key. Nothing is lost: createSession() reads refresh_token and
// refresh_token_expires_in straight off the token response regardless of any flag.
export const api = shopifyApi({
  apiKey: API_KEY,
  apiSecretKey: API_SECRET,
  apiVersion: ApiVersion.July26,
  scopes: SCOPES,
  hostName: APP_URL.replace(/^https?:\/\//, ""),
  isEmbeddedApp: true,
});

export default shopify;
export const apiVersion = ApiVersion.July26;
export const addDocumentResponseHeaders = shopify.addDocumentResponseHeaders;
export const authenticate = shopify.authenticate;
// `unauthenticated` is deliberately NOT re-exported. Both `unauthenticated.admin()` and
// `unauthenticated.storefront()` refresh the offline token outside our per-shop rotation
// lock, which would race the token route and invalidate the refresh chain. Use
// `getAdminToken()` (lib/admin-token.server.ts) instead — it is the serialized path.
export const login = shopify.login;
export const registerWebhooks = shopify.registerWebhooks;
export const sessionStorage = shopify.sessionStorage;
