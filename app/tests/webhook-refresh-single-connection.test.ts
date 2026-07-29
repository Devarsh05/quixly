/**
 * Webhook authentication must borrow ZERO second connections while it holds the rotation lock.
 *
 * `withShopRefreshLock` runs its critical section inside `prisma.$transaction`, which pins one
 * pooled connection and holds `pg_advisory_xact_lock` for the whole transaction. If anything
 * inside that section reaches the database through the GLOBAL Prisma client, it needs a second
 * connection from the same pool — and when the pool is no larger than the number of concurrent
 * same-shop webhooks, every connection is held by a transaction and the lock winner cannot get
 * the one it needs. That is a resource deadlock; Prisma breaks it by timing out (`P2024`), so
 * the merchant-visible symptom is failed webhooks and Shopify redeliveries, not a hung process.
 *
 * **The pool cap of ONE is the proof, not a stress test.** A section that needs two connections
 * cannot complete on a pool that can supply one — not "usually fails", cannot. So these tests
 * passing is a structural statement about the number of connections the section borrows, and it
 * holds regardless of timing, host core count, or scheduler luck.
 *
 * Note the first test needs no expiring token at all: `createOrLoadOfflineSession` calls
 * `sessionStorage.loadSession` unconditionally, before any expiry check, so *every* webhook
 * borrowed the second connection — not only the refreshing ones.
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

const SHOP = "webhook-pool-one.myshopify.com";
const SESSION_ID = `offline_${SHOP}`;
const HOUR_MS = 60 * 60 * 1000;

vi.mock("../app/db.server", async () => {
  const { PrismaClient } = await import("@prisma/client");
  const { poolUrl: url } = await import("./helpers/webhook-harness");
  // A pool of exactly one. See the file header: this is the experiment.
  return { default: new PrismaClient({ datasourceUrl: url(1, 2) }) };
});

vi.mock("../app/shopify.server", async () => {
  const { buildShopifyServerMock } = await import("./helpers/webhook-harness");
  return buildShopifyServerMock();
});

// The fetch stub must be installed before the mocked `shopify.server` is first imported —
// see installFetchStub().
installFetchStub();

const { default: prisma } = await import("../app/db.server");
const { authenticate } = await import("../app/shopify.server");
const { authenticateWebhookSerialized, WEBHOOK_REFRESH_WINDOW_MS } = await import(
  "../app/lib/webhook-auth.server"
);

const sessionStorage = new PrismaSessionStorage(prisma);

/** A token nowhere near expiry: neither our window nor the library's should fire. */
async function seedFreshSession() {
  await sessionStorage.storeSession(
    makeSession({
      shop: SHOP,
      accessToken: "token_v1",
      refreshToken: "refresh_v1",
      expiresInMs: HOUR_MS,
    }),
  );
}

async function seedSession(expiresInMs: number, refreshTokenExpiresInMs?: number) {
  await sessionStorage.storeSession(
    makeSession({
      shop: SHOP,
      accessToken: "token_v1",
      refreshToken: "refresh_v1",
      expiresInMs,
      ...(refreshTokenExpiresInMs === undefined ? {} : { refreshTokenExpiresInMs }),
    }),
  );
}

/** What a successful rotation hands back. */
function rotatedSession() {
  return {
    session: makeSession({
      shop: SHOP,
      accessToken: "token_v2",
      refreshToken: "refresh_v2",
      expiresInMs: HOUR_MS,
    }),
  };
}

describe("webhook authentication on a single-connection pool", () => {
  beforeEach(async () => {
    refreshTokenSpy.mockReset();
    libraryFetch.mockClear();
    await prisma.session.deleteMany({ where: { shop: SHOP } });
  });

  afterAll(async () => {
    await prisma.session.deleteMany({ where: { shop: SHOP } });
    await prisma.$disconnect();
  });

  it("completes a single webhook on a pool of ONE connection", async () => {
    await seedFreshSession();

    const context = await authenticateWebhookSerialized(
      signedWebhookRequest({ shop: SHOP }),
    );

    expect(context.shop).toBe(SHOP);
    expect(context.session?.accessToken).toBe("token_v1");
    // Nothing was near expiry, so neither refresh path had any reason to run.
    expect(refreshTokenSpy).not.toHaveBeenCalled();
    expect(libraryFetch).not.toHaveBeenCalled();
  });

  it("completes 5 concurrent same-shop webhooks on a pool of ONE connection, with exactly one refresh", async () => {
    await seedSession(-60_000);
    refreshTokenSpy.mockImplementation(async () => {
      // Widen the window in which a second caller could start its own refresh.
      await new Promise((resolve) => setTimeout(resolve, 50));
      return rotatedSession();
    });

    const contexts = await Promise.all(
      Array.from({ length: 5 }, () =>
        authenticateWebhookSerialized(signedWebhookRequest({ shop: SHOP })),
      ),
    );

    expect(contexts).toHaveLength(5);
    for (const context of contexts) {
      expect(context.session?.accessToken).toBe("token_v2");
    }
    // Five callers, ONE rotation: a second would have invalidated the chain.
    expect(refreshTokenSpy).toHaveBeenCalledTimes(1);
    expect(libraryFetch).not.toHaveBeenCalled();
  });

  it("refreshes a token that is inside our window but outside the library's", async () => {
    // 7 minutes: past our 10-minute window, short of the library's 5-minute one. Ours must
    // be the path that fires — that is what keeps the library's refresh unreachable.
    await seedSession(7 * 60 * 1000);
    refreshTokenSpy.mockResolvedValue(rotatedSession());

    const context = await authenticateWebhookSerialized(
      signedWebhookRequest({ shop: SHOP }),
    );

    expect(refreshTokenSpy).toHaveBeenCalledTimes(1);
    expect(libraryFetch).not.toHaveBeenCalled();
    expect(context.session?.accessToken).toBe("token_v2");
    expect((await sessionStorage.loadSession(SESSION_ID))?.accessToken).toBe("token_v2");
  });

  it("does not refresh a token outside our window", async () => {
    await seedSession(30 * 60 * 1000);

    await authenticateWebhookSerialized(signedWebhookRequest({ shop: SHOP }));

    expect(refreshTokenSpy).not.toHaveBeenCalled();
    expect(libraryFetch).not.toHaveBeenCalled();
  });

  it("keeps the library's own refresh threshold strictly inside ours", async () => {
    // A CROSS-BOUNDARY INVARIANT: our window must stay wider than the library's internal
    // threshold (5 minutes in `ensure-offline-token-is-not-expired.js`), or the library can
    // refresh outside the lock. That constant is not exported and can move on a dependency
    // bump, so probe the library's real behaviour instead of trusting the number: just inside
    // our window, the library must still consider the token fine. Calling `authenticate.webhook`
    // DIRECTLY is deliberate — going through our wrapper would refresh the session first and
    // the library would never see it.
    await seedSession(WEBHOOK_REFRESH_WINDOW_MS - 30_000);

    const context = await authenticate.webhook(signedWebhookRequest({ shop: SHOP }));

    expect(libraryFetch).not.toHaveBeenCalled();
    expect(context.session?.accessToken).toBe("token_v1");
  });

  it("rejects a forged HMAC with 401", async () => {
    await seedFreshSession();

    const rejection = await authenticateWebhookSerialized(
      signedWebhookRequest({ shop: SHOP, hmac: "not-a-real-signature" }),
    ).catch((error: unknown) => error);

    expect(rejection).toBeInstanceOf(Response);
    expect((rejection as Response).status).toBe(401);
    expect(refreshTokenSpy).not.toHaveBeenCalled();
    expect(libraryFetch).not.toHaveBeenCalled();
  });

  it("still refreshes before rejecting a forged HMAC — the accepted pre-HMAC trade-off", async () => {
    // Documented, deliberate: the shop is read from an unverified header, so a forged request
    // for a real shop whose token is near expiry can advance that shop's refresh. It cannot
    // raise the refresh RATE — one rotation moves the token ~60 minutes out, so every
    // subsequent forgery is a no-op. Pinned here so the trade-off cannot change unnoticed.
    await seedSession(-60_000);
    refreshTokenSpy.mockResolvedValue(rotatedSession());

    const rejection = await authenticateWebhookSerialized(
      signedWebhookRequest({ shop: SHOP, hmac: "not-a-real-signature" }),
    ).catch((error: unknown) => error);

    expect((rejection as Response).status).toBe(401);
    expect(refreshTokenSpy).toHaveBeenCalledTimes(1);
  });

  it("normalizes the REST topic header to the canonical form", async () => {
    await seedFreshSession();

    const context = await authenticateWebhookSerialized(
      signedWebhookRequest({ shop: SHOP, topic: "products/update" }),
    );

    // The agent dispatches on this exact string. The REST form silently no-ops every
    // forwarded webhook — see CLAUDE.md > "Forwarded webhooks dispatch on the canonical form".
    expect(context.topic).toBe("PRODUCTS_UPDATE");
  });

  it("processes a webhook for a shop with no stored session", async () => {
    // `app/uninstalled` can arrive after the session row is gone. It must still authenticate.
    const context = await authenticateWebhookSerialized(
      signedWebhookRequest({ shop: SHOP, topic: "app/uninstalled" }),
    );

    expect(context.topic).toBe("APP_UNINSTALLED");
    expect(context.session).toBeUndefined();
    expect(refreshTokenSpy).not.toHaveBeenCalled();
    expect(libraryFetch).not.toHaveBeenCalled();
  });

  it("delegates to the library when the shop header is missing", async () => {
    const rejection = await authenticateWebhookSerialized(
      signedWebhookRequest({ shop: undefined }),
    ).catch((error: unknown) => error);

    // No shop means no lock key and nothing to pre-refresh; the library rejects it on
    // missing headers, exactly as before.
    expect(rejection).toBeInstanceOf(Response);
    expect((rejection as Response).status).toBe(400);
    expect(refreshTokenSpy).not.toHaveBeenCalled();
  });

  it("fails fast with 503 when the refresh fails transiently", async () => {
    await seedSession(-60_000);
    refreshTokenSpy.mockRejectedValue(new Error("socket hang up"));
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});

    const rejection = await authenticateWebhookSerialized(
      signedWebhookRequest({ shop: SHOP }),
    ).catch((error: unknown) => error);

    // Proceeding would hand the library a still-expired session on a LIVE chain, and its
    // refresh runs outside the lock. Better to make Shopify redeliver.
    expect(rejection).toBeInstanceOf(Response);
    expect((rejection as Response).status).toBe(503);
    expect(libraryFetch).not.toHaveBeenCalled();
    expect(logged).toHaveBeenCalled();
    logged.mockRestore();
  });

  it("proceeds when the refresh chain is already dead", async () => {
    // Refresh token lapsed: permanent. We cannot decide the request here — the HMAC has not
    // been checked yet — so we hand it to the library, whose refresh fails and 500s. A dead
    // chain cannot be damaged further, and this is what the code did before the fix too.
    await seedSession(-60_000, -1_000);
    const logged = vi.spyOn(console, "warn").mockImplementation(() => {});

    const rejection = await authenticateWebhookSerialized(
      signedWebhookRequest({ shop: SHOP }),
    ).catch((error: unknown) => error);

    expect(refreshTokenSpy).not.toHaveBeenCalled();
    expect(rejection).toBeInstanceOf(Response);
    expect((rejection as Response).status).toBe(500);
    // The shop needs re-auth; that has to be visible in the logs, not silent.
    expect(logged).toHaveBeenCalled();
    logged.mockRestore();
  });
});
