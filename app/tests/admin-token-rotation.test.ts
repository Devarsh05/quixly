/**
 * Token rotation: serialization and refresh-token persistence.
 *
 * These run against a REAL Postgres advisory lock and the REAL PrismaSessionStorage —
 * only Shopify's HTTP call is faked. A mocked lock would prove nothing about
 * serialization, which is the entire point: rotating a shop's offline token retires the
 * previous token and invalidates its refresh token, so two concurrent rotations do not
 * merely duplicate work — they permanently break the chain and force a reinstall.
 *
 * Requires Postgres with the Session table migrated (`npx prisma migrate deploy`).
 */

import { Session } from "@shopify/shopify-api";
import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

import prisma from "../app/db.server";

const SHOP = "rotation-test.myshopify.com";
const SESSION_ID = `offline_${SHOP}`;

const refreshToken = vi.fn();

// Real session storage against real Postgres; only the Shopify API call is faked.
const realSessionStorage = new PrismaSessionStorage(prisma);

vi.mock("../app/shopify.server", async () => {
  const { PrismaSessionStorage: Storage } = await import(
    "@shopify/shopify-app-session-storage-prisma"
  );
  const { default: client } = await import("../app/db.server");
  return {
    sessionStorage: new Storage(client),
    api: {
      session: { getOfflineId: (shop: string) => `offline_${shop}` },
      auth: { refreshToken: (args: unknown) => refreshToken(args) },
    },
  };
});

const { getAdminToken } = await import("../app/lib/admin-token.server");

function makeSession({
  accessToken,
  refreshToken: rt,
  expiresInMs,
  refreshTokenExpiresInMs = 90 * 24 * 60 * 60 * 1000,
}: {
  accessToken: string;
  refreshToken: string;
  expiresInMs: number;
  refreshTokenExpiresInMs?: number;
}): Session {
  return new Session({
    id: SESSION_ID,
    shop: SHOP,
    state: "",
    isOnline: false,
    accessToken,
    scope: "write_products",
    expires: new Date(Date.now() + expiresInMs),
    refreshToken: rt,
    refreshTokenExpires: new Date(Date.now() + refreshTokenExpiresInMs),
  });
}

/** Seed a stored session that is due for refresh (inside the 5-minute window). */
async function seedExpiredSession() {
  await realSessionStorage.storeSession(
    makeSession({ accessToken: "token_v1", refreshToken: "refresh_v1", expiresInMs: -60_000 }),
  );
}

describe("token rotation", () => {
  beforeEach(async () => {
    refreshToken.mockReset();
    await prisma.session.deleteMany({ where: { shop: SHOP } });
  });

  afterAll(async () => {
    await prisma.session.deleteMany({ where: { shop: SHOP } });
    await prisma.$disconnect();
  });

  it("serializes concurrent callers into exactly ONE Shopify refresh", async () => {
    await seedExpiredSession();

    refreshToken.mockImplementation(async () => {
      // Widen the race window: without the advisory lock, every caller would already be
      // past the staleness check and would each issue their own refresh.
      await new Promise((resolve) => setTimeout(resolve, 50));
      return {
        session: makeSession({
          accessToken: "token_v2",
          refreshToken: "refresh_v2",
          expiresInMs: 60 * 60 * 1000,
        }),
      };
    });

    const CONCURRENCY = 10;
    const results = await Promise.all(
      Array.from({ length: CONCURRENCY }, () => getAdminToken(SHOP)),
    );

    // The whole point: N callers, ONE rotation. More than one would have invalidated the
    // chain and left the shop needing a reinstall.
    expect(refreshToken).toHaveBeenCalledTimes(1);

    // Everyone gets the rotated token, and nobody was told to re-auth.
    expect(results).toHaveLength(CONCURRENCY);
    for (const result of results) {
      expect(result.accessToken).toBe("token_v2");
    }

    // And the rotation is what is persisted.
    const stored = await realSessionStorage.loadSession(SESSION_ID);
    expect(stored?.accessToken).toBe("token_v2");
    expect(stored?.refreshToken).toBe("refresh_v2");
  });

  it("persists the rotated refresh token: a second refresh uses the token the first returned", async () => {
    await seedExpiredSession();

    // Each refresh hands back a session that is ITSELF already inside the refresh window,
    // so the next call must rotate again — which is what lets us observe which refresh
    // token the second call actually sends.
    refreshToken
      .mockResolvedValueOnce({
        session: makeSession({
          accessToken: "token_v2",
          refreshToken: "refresh_v2",
          expiresInMs: 60_000,
        }),
      })
      .mockResolvedValueOnce({
        session: makeSession({
          accessToken: "token_v3",
          refreshToken: "refresh_v3",
          expiresInMs: 60 * 60 * 1000,
        }),
      });

    const first = await getAdminToken(SHOP);
    expect(first.accessToken).toBe("token_v2");

    const second = await getAdminToken(SHOP);
    expect(second.accessToken).toBe("token_v3");

    expect(refreshToken).toHaveBeenCalledTimes(2);

    // The first call must have sent the seeded refresh token...
    expect(refreshToken).toHaveBeenNthCalledWith(1, {
      shop: SHOP,
      refreshToken: "refresh_v1",
    });
    // ...and the second must have sent the ROTATED one. If the rotation were not persisted,
    // this would replay refresh_v1 — a token Shopify already invalidated — and the chain
    // would be dead.
    expect(refreshToken).toHaveBeenNthCalledWith(2, {
      shop: SHOP,
      refreshToken: "refresh_v2",
    });

    const stored = await realSessionStorage.loadSession(SESSION_ID);
    expect(stored?.refreshToken).toBe("refresh_v3");
  });

  it("does not refresh at all when the stored token is still valid", async () => {
    await realSessionStorage.storeSession(
      makeSession({
        accessToken: "token_fresh",
        refreshToken: "refresh_v1",
        expiresInMs: 60 * 60 * 1000,
      }),
    );

    const result = await getAdminToken(SHOP);

    expect(result.accessToken).toBe("token_fresh");
    expect(refreshToken).not.toHaveBeenCalled();
  });
});
