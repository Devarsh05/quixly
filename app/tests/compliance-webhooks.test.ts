/**
 * Shopify's three MANDATORY compliance webhooks — the App Store submission gate.
 *
 * The automated review probes each endpoint, including with a deliberately invalid HMAC, and
 * rejects the app if any of them misbehaves. So these tests run the REAL `authenticate.webhook()`
 * against REAL signatures rather than mocking auth away: a mocked verifier would prove nothing
 * about the one thing the review actually checks.
 *
 * THREE REJECTION SHAPES, and they are not interchangeable:
 *   - valid HMAC        -> the handler runs, 200
 *   - INVALID HMAC      -> the library throws Response(401). This is the case the review sends,
 *                          and the case Shopify's docs require a 401 for.
 *   - MISSING HMAC header -> the library throws Response(400) (`MissingHeaders`), NOT 401. That is
 *                          a different branch of `validate.ts`, asserted here as the real
 *                          behaviour rather than wished into a 401.
 *
 * The two `customers/*` handlers are acknowledge-only because Quixly stores no end-customer
 * personal data. "Acknowledge-only" is a claim about SIDE EFFECTS, so it is tested as one: the
 * agent is never called and no session row moves. A future change that starts forwarding customer
 * identifiers to the agent fails here.
 *
 * Needs Postgres with the Session table migrated, like the other webhook tests — the advisory lock
 * and PrismaSessionStorage are real. Run with `?schema=shopify` on DATABASE_URL.
 */

import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

import {
  installFetchStub,
  libraryFetch,
  makeSession,
  refreshTokenSpy,
  signedWebhookRequest,
} from "./helpers/webhook-harness";

const SHOP = "compliance-test.myshopify.com";
/** A second tenant, seeded in every case: an erasure must not reach across shops. */
const NEIGHBOUR = "compliance-neighbour.myshopify.com";
const HOUR_MS = 60 * 60 * 1000;

const forwardWebhook = vi.fn();

vi.mock("../app/db.server", async () => {
  const { PrismaClient } = await import("@prisma/client");
  return { default: new PrismaClient() };
});

vi.mock("../app/shopify.server", async () => {
  const { buildShopifyServerMock } = await import("./helpers/webhook-harness");
  return buildShopifyServerMock();
});

vi.mock("../app/lib/agent.server", () => ({
  forwardWebhook: (...args: unknown[]) => forwardWebhook(...args),
  connectShop: vi.fn(),
}));

installFetchStub();

const { default: prisma } = await import("../app/db.server");
const dataRequestRoute = await import("../app/routes/webhooks.customers.data_request");
const customersRedactRoute = await import("../app/routes/webhooks.customers.redact");
const shopRedactRoute = await import("../app/routes/webhooks.shop.redact");

const sessionStorage = new PrismaSessionStorage(prisma);

type RouteModule = { action: (args: { request: Request }) => Promise<Response> };

/** What Shopify actually posts for each topic — the payload the review sends. */
const ENDPOINTS: { name: string; topic: string; route: RouteModule; payload: unknown }[] = [
  {
    name: "customers/data_request",
    topic: "customers/data_request",
    route: dataRequestRoute as RouteModule,
    payload: {
      shop_domain: SHOP,
      customer: { id: 191167, email: "shopper@example.com", phone: "555-625-1199" },
      orders_requested: [299938, 280263],
    },
  },
  {
    name: "customers/redact",
    topic: "customers/redact",
    route: customersRedactRoute as RouteModule,
    payload: {
      shop_domain: SHOP,
      customer: { id: 191167, email: "shopper@example.com", phone: "555-625-1199" },
      orders_to_redact: [299938],
    },
  },
  {
    name: "shop/redact",
    topic: "shop/redact",
    route: shopRedactRoute as RouteModule,
    payload: { shop_id: 954889, shop_domain: SHOP },
  },
];

function call(route: RouteModule, request: Request): Promise<Response> {
  return route.action({ request });
}

async function seedSession(shop: string) {
  await sessionStorage.storeSession(
    makeSession({
      shop,
      accessToken: `token_${shop}`,
      refreshToken: `refresh_${shop}`,
      // Comfortably outside WEBHOOK_REFRESH_WINDOW_MS, so nothing rotates and the tests are
      // about compliance behaviour rather than token custody.
      expiresInMs: HOUR_MS,
    }),
  );
}

async function sessionCount(shop: string): Promise<number> {
  return prisma.session.count({ where: { shop } });
}

beforeEach(async () => {
  forwardWebhook.mockReset();
  refreshTokenSpy.mockReset();
  libraryFetch.mockClear();
  await prisma.session.deleteMany({ where: { shop: { in: [SHOP, NEIGHBOUR] } } });
});

afterAll(async () => {
  await prisma.session.deleteMany({ where: { shop: { in: [SHOP, NEIGHBOUR] } } });
  await prisma.$disconnect();
});

// --- HMAC: the same contract on all three endpoints ------------------------------------

describe.each(ENDPOINTS)("$name", ({ topic, route, payload }) => {
  it("returns 200 for a validly signed request", async () => {
    await seedSession(SHOP);

    const response = await call(route, signedWebhookRequest({ shop: SHOP, topic, payload }));

    expect(response.status).toBe(200);
  });

  it("rejects an INVALID HMAC with 401", async () => {
    await seedSession(SHOP);

    // Shopify's automated review sends exactly this: a well-formed delivery signed wrong.
    const request = signedWebhookRequest({
      shop: SHOP,
      topic,
      payload,
      hmac: "bm90LWEtdmFsaWQtc2lnbmF0dXJl",
    });

    const thrown = await call(route, request).catch((error: unknown) => error);

    expect(thrown).toBeInstanceOf(Response);
    expect((thrown as Response).status).toBe(401);
  });

  it("rejects a MISSING HMAC header with 400", async () => {
    await seedSession(SHOP);

    const request = signedWebhookRequest({ shop: SHOP, topic, payload, omitHmac: true });

    const thrown = await call(route, request).catch((error: unknown) => error);

    // Not 401: `validate.ts` reports a missing header as MissingHeaders, a separate 400 branch.
    // Asserting the library's real behaviour, not the behaviour we might have assumed.
    expect(thrown).toBeInstanceOf(Response);
    expect((thrown as Response).status).toBe(400);
  });

  it("never reaches the handler on a rejected signature", async () => {
    await seedSession(SHOP);

    await call(route, signedWebhookRequest({ shop: SHOP, topic, payload, hmac: "forged" })).catch(
      () => undefined,
    );

    expect(forwardWebhook).not.toHaveBeenCalled();
    expect(await sessionCount(SHOP)).toBe(1);
  });
});

// --- customers/*: acknowledge-only means NO side effects -------------------------------

describe.each(ENDPOINTS.filter((endpoint) => endpoint.name.startsWith("customers/")))(
  "$name is acknowledge-only",
  ({ topic, route, payload }) => {
    it("does not call the agent and leaves stored sessions untouched", async () => {
      await seedSession(SHOP);
      await seedSession(NEIGHBOUR);

      const response = await call(route, signedWebhookRequest({ shop: SHOP, topic, payload }));

      expect(response.status).toBe(200);
      // The payload carries a customer id, email and phone. Nothing may forward it onward —
      // the agent has never held a customer identifier and must not start now.
      expect(forwardWebhook).not.toHaveBeenCalled();
      expect(await sessionCount(SHOP)).toBe(1);
      expect(await sessionCount(NEIGHBOUR)).toBe(1);
    });
  },
);

// --- shop/redact: the one with real work -----------------------------------------------

describe("shop/redact", () => {
  const { topic, route, payload } = ENDPOINTS[2];

  const request = () => signedWebhookRequest({ shop: SHOP, topic, payload });

  it("forwards the canonical topic so the agent can queue its purge", async () => {
    await seedSession(SHOP);

    const response = await call(route, request());

    expect(response.status).toBe(200);
    // UPPER_SNAKE — `topicForStorage()` form. The agent dispatches on this; the REST form
    // `shop/redact` would fall through to a 204 no-op that queues nothing.
    expect(forwardWebhook).toHaveBeenCalledWith("SHOP_REDACT", SHOP, {});
  });

  it("deletes the shop's session rows", async () => {
    await seedSession(SHOP);

    await call(route, request());

    expect(await sessionCount(SHOP)).toBe(0);
  });

  it("leaves another shop's session rows alone", async () => {
    await seedSession(SHOP);
    await seedSession(NEIGHBOUR);

    await call(route, request());

    expect(await sessionCount(NEIGHBOUR)).toBe(1);
  });

  it("succeeds with no session stored — the normal case 48h after uninstall", async () => {
    // app/uninstalled deleted the session two days ago. The HMAC is signed with the app secret,
    // not the session, so verification is unaffected and the purge must still be requested.
    expect(await sessionCount(SHOP)).toBe(0);

    const response = await call(route, request());

    expect(response.status).toBe(200);
    expect(forwardWebhook).toHaveBeenCalledWith("SHOP_REDACT", SHOP, {});
    // And the library's own refresh never fired on the way through.
    expect(libraryFetch).not.toHaveBeenCalled();
  });

  it("returns 500 and keeps the sessions when the agent is unreachable", async () => {
    // Shopify redelivers on a non-2xx. The rows must still be here for the retry to erase,
    // otherwise a brief agent outage silently loses the app-shell half of the erasure.
    await seedSession(SHOP);
    forwardWebhook.mockRejectedValueOnce(new Error("Agent /webhooks/shopify returned 503"));

    const response = await call(route, request());

    expect(response.status).toBe(500);
    expect(await sessionCount(SHOP)).toBe(1);
  });
});
