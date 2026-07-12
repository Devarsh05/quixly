import "@shopify/shopify-app-react-router/adapters/node";
import {
  ApiVersion,
  AppDistribution,
  shopifyApp,
} from "@shopify/shopify-app-react-router/server";
import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";
import prisma from "./db.server";
import { connectShop } from "./lib/agent.server";

const shopify = shopifyApp({
  apiKey: process.env.SHOPIFY_API_KEY,
  apiSecretKey: process.env.SHOPIFY_API_SECRET || "",
  apiVersion: ApiVersion.October25,
  scopes: process.env.SCOPES?.split(","),
  appUrl: process.env.SHOPIFY_APP_URL || "",
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
    expiringOfflineAccessTokens: true,
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

export default shopify;
export const apiVersion = ApiVersion.October25;
export const addDocumentResponseHeaders = shopify.addDocumentResponseHeaders;
export const authenticate = shopify.authenticate;
export const unauthenticated = shopify.unauthenticated;
export const login = shopify.login;
export const registerWebhooks = shopify.registerWebhooks;
export const sessionStorage = shopify.sessionStorage;
