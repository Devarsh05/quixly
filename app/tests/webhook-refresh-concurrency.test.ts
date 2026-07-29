/**
 * The production shape of the webhook deadlock: a small pool and a same-shop fan-out.
 *
 * On a 1-vCPU container Prisma's default pool is `num_cpus * 2 + 1` = 3, and `products/update`
 * fans out on bulk merchant edits — and fires on our own publishes. Three concurrent
 * same-shop deliveries were enough to starve the lock winner of the second connection it
 * needed. This file pins the realistic case; `webhook-refresh-single-connection.test.ts`
 * carries the structural proof.
 *
 * Unlike the pool-of-one file, the pool here is larger than one, so the transactions really do
 * run concurrently and really do contend on `pg_advisory_xact_lock` — which is what makes the
 * "exactly one refresh" assertion a statement about the lock rather than about the pool.
 *
 * Requires Postgres with the Session table migrated (`npx prisma migrate deploy`).
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

const SHOP = "webhook-pool-three.myshopify.com";
const SESSION_ID = `offline_${SHOP}`;
const HOUR_MS = 60 * 60 * 1000;
/** What a 1-vCPU container actually gets from Prisma's default `num_cpus * 2 + 1`. */
const POOL_SIZE = 3;
const CONCURRENCY = 5;

vi.mock("../app/db.server", async () => {
  const { PrismaClient } = await import("@prisma/client");
  const { poolUrl } = await import("./helpers/webhook-harness");
  return { default: new PrismaClient({ datasourceUrl: poolUrl(3, 2) }) };
});

vi.mock("../app/shopify.server", async () => {
  const { buildShopifyServerMock } = await import("./helpers/webhook-harness");
  return buildShopifyServerMock();
});

installFetchStub();

const { default: prisma } = await import("../app/db.server");
const { authenticateWebhookSerialized } = await import("../app/lib/webhook-auth.server");

const sessionStorage = new PrismaSessionStorage(prisma);

async function seedExpiredSession() {
  await sessionStorage.storeSession(
    makeSession({
      shop: SHOP,
      accessToken: "token_v1",
      refreshToken: "refresh_v1",
      expiresInMs: -60_000,
    }),
  );
}

describe(`webhook fan-out on a pool of ${POOL_SIZE}`, () => {
  beforeEach(async () => {
    refreshTokenSpy.mockReset();
    libraryFetch.mockClear();
    await prisma.session.deleteMany({ where: { shop: SHOP } });
  });

  afterAll(async () => {
    await prisma.session.deleteMany({ where: { shop: SHOP } });
    await prisma.$disconnect();
  });

  it(`completes ${CONCURRENCY} concurrent same-shop webhooks with exactly one refresh`, async () => {
    await seedExpiredSession();
    refreshTokenSpy.mockImplementation(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
      return {
        session: makeSession({
          shop: SHOP,
          accessToken: "token_v2",
          refreshToken: "refresh_v2",
          expiresInMs: HOUR_MS,
        }),
      };
    });

    const contexts = await Promise.all(
      Array.from({ length: CONCURRENCY }, () =>
        authenticateWebhookSerialized(signedWebhookRequest({ shop: SHOP })),
      ),
    );

    expect(contexts).toHaveLength(CONCURRENCY);
    for (const context of contexts) {
      expect(context.topic).toBe("PRODUCTS_UPDATE");
      expect(context.session?.accessToken).toBe("token_v2");
    }
    expect(refreshTokenSpy).toHaveBeenCalledTimes(1);
    expect(libraryFetch).not.toHaveBeenCalled();
  });

  it("sends the rotated refresh token on the next rotation", async () => {
    await seedExpiredSession();
    // The first rotation lands in the gap between the two windows: 7 minutes is fresh to the
    // library (5) and stale to us (10). So the second webhook rotates again — which is what
    // lets us observe which refresh token it sends — while the library still never fires.
    refreshTokenSpy
      .mockResolvedValueOnce({
        session: makeSession({
          shop: SHOP,
          accessToken: "token_v2",
          refreshToken: "refresh_v2",
          expiresInMs: 7 * 60 * 1000,
        }),
      })
      .mockResolvedValueOnce({
        session: makeSession({
          shop: SHOP,
          accessToken: "token_v3",
          refreshToken: "refresh_v3",
          expiresInMs: HOUR_MS,
        }),
      });

    await authenticateWebhookSerialized(signedWebhookRequest({ shop: SHOP }));
    await authenticateWebhookSerialized(signedWebhookRequest({ shop: SHOP }));

    expect(refreshTokenSpy).toHaveBeenNthCalledWith(1, {
      shop: SHOP,
      refreshToken: "refresh_v1",
    });
    // Replaying `refresh_v1` here would mean the rotation was never persisted — Shopify
    // invalidated it the moment it issued `refresh_v2`, so the chain would be dead.
    expect(refreshTokenSpy).toHaveBeenNthCalledWith(2, {
      shop: SHOP,
      refreshToken: "refresh_v2",
    });
    expect((await sessionStorage.loadSession(SESSION_ID))?.refreshToken).toBe("refresh_v3");
    expect(libraryFetch).not.toHaveBeenCalled();
  });
});
