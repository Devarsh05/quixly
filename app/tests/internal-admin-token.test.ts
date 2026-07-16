/**
 * The internal admin-token route is the agent's only way to obtain a Shopify token, and
 * the app shell's only job as the single refresh authority.
 *
 * The status code is load-bearing: the agent treats 404 as PERMANENT (flag the shop
 * reauth_required) and everything else as TRANSIENT (retry). A dead refresh chain
 * (invalid_grant) must therefore return the SAME 404 as a missing session row — mapping
 * it to 502 would retry a 90-day-idle shop forever and it would never be flagged.
 *
 * These run against REAL Postgres and the REAL PrismaSessionStorage — only Shopify's HTTP
 * refresh call is faked. The refresh path reads and persists the session on the rotation
 * lock's own transaction connection (see admin-token.server.ts), NOT through an injectable
 * seam, so a test that merely mocked session storage would 404 on an empty table without ever
 * reaching the refresh it claims to assert. Every refresh-path test therefore seeds a real row
 * and asserts `refreshToken` was actually called; the pre-refresh 404s seed the exact condition
 * that triggers them and assert `refreshToken` was NOT called.
 *
 * Requires Postgres with the Session table migrated (`npx prisma migrate deploy`).
 */

import { HttpResponseError, InvalidJwtError, Session } from "@shopify/shopify-api";
import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

import prisma from "../app/db.server";

const SHOP = "quixly-dev.myshopify.com";
const SESSION_ID = `offline_${SHOP}`;
const KEY = "test-internal-key";
const TOKEN = "shpat_secret";

const refreshToken = vi.fn();

// Real session storage against real Postgres; only the Shopify API call is faked. The refresh
// path's inner load/store run on the lock's tx connection (not through this instance), so
// seeding a real row is what makes both the fast-path and the inner read observe the session.
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

const { action } = await import("../app/routes/internal.shops.$shop.admin-token");

function makeSession({
  accessToken = TOKEN,
  refreshToken: rt = "refresh_abc",
  expiresInMs,
  refreshTokenExpiresInMs = 90 * 24 * 60 * 60 * 1000,
}: {
  accessToken?: string;
  refreshToken?: string | null;
  expiresInMs: number;
  refreshTokenExpiresInMs?: number | null;
}): Session {
  return new Session({
    id: SESSION_ID,
    shop: SHOP,
    state: "",
    isOnline: false,
    accessToken,
    scope: "write_products",
    expires: new Date(Date.now() + expiresInMs),
    refreshToken: rt ?? undefined,
    refreshTokenExpires:
      refreshTokenExpiresInMs === null
        ? undefined
        : new Date(Date.now() + refreshTokenExpiresInMs),
  });
}

/** Seed a real session row for SHOP into Postgres. */
async function seedSession(opts: Parameters<typeof makeSession>[0]) {
  await realSessionStorage.storeSession(makeSession(opts));
}

/** Shopify's OAuth token endpoint rejecting a grant, as thrown by throwFailedRequest. */
function oauthError(error: string, code = 400) {
  return new HttpResponseError({
    message: `Received an error response (${code})`,
    code,
    statusText: "Bad Request",
    body: { error },
    headers: {},
  });
}

function callAction(headers: Record<string, string> = { "x-internal-api-key": KEY }) {
  // The action only reads `request` and `params`; the rest of ActionFunctionArgs
  // (context, url, pattern) is React Router plumbing it never touches.
  return action({
    request: new Request(`http://localhost/internal/shops/${SHOP}/admin-token`, {
      method: "POST",
      headers,
    }),
    params: { shop: SHOP },
  } as unknown as Parameters<typeof action>[0]) as Promise<Response>;
}

/** The route signals auth failure by *throwing* a Response. */
async function statusOf(promise: Promise<unknown>): Promise<number> {
  try {
    return ((await promise) as Response).status;
  } catch (thrown) {
    if (thrown instanceof Response) return thrown.status;
    throw thrown;
  }
}

describe("POST /internal/shops/:shop/admin-token", () => {
  beforeEach(async () => {
    vi.stubEnv("INTERNAL_API_KEY", KEY);
    refreshToken.mockReset();
    await prisma.session.deleteMany({ where: { shop: SHOP } });
  });

  afterAll(async () => {
    await prisma.session.deleteMany({ where: { shop: SHOP } });
    await prisma.$disconnect();
  });

  describe("shared-secret guard", () => {
    it("rejects a request with no key", async () => {
      expect(await statusOf(callAction({}))).toBe(401);
      expect(refreshToken).not.toHaveBeenCalled();
    });

    it("rejects a wrong key", async () => {
      expect(await statusOf(callAction({ "x-internal-api-key": "wrong" }))).toBe(401);
      expect(refreshToken).not.toHaveBeenCalled();
    });

    it("rejects a key that is a prefix of the real one", async () => {
      const status = await statusOf(callAction({ "x-internal-api-key": KEY.slice(0, -1) }));
      expect(status).toBe(401);
    });

    it("fails closed when INTERNAL_API_KEY is unset", async () => {
      vi.stubEnv("INTERNAL_API_KEY", "");
      expect(await statusOf(callAction({ "x-internal-api-key": "anything" }))).toBe(500);
      expect(refreshToken).not.toHaveBeenCalled();
    });
  });

  describe("happy path", () => {
    it("returns a still-valid token without refreshing", async () => {
      await seedSession({ expiresInMs: 60 * 60 * 1000 });

      const response = await callAction();

      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toMatchObject({ access_token: TOKEN });
      expect(refreshToken).not.toHaveBeenCalled();
    });

    it("refreshes a near-expiry token and persists the rotation", async () => {
      // Inside the 5-minute window, so a refresh is required.
      await seedSession({ expiresInMs: 60 * 1000 });
      const rotated = makeSession({
        accessToken: "shpat_rotated",
        refreshToken: "refresh_rotated",
        expiresInMs: 60 * 60 * 1000,
      });
      refreshToken.mockResolvedValue({ session: rotated });

      const response = await callAction();

      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toMatchObject({ access_token: "shpat_rotated" });
      // The refresh was actually reached — not an empty-table pass.
      expect(refreshToken).toHaveBeenCalledTimes(1);
      // The old token dies the instant the new one is issued — the rotation must be persisted,
      // and on the lock's own transaction connection at that.
      const stored = await realSessionStorage.loadSession(SESSION_ID);
      expect(stored?.accessToken).toBe("shpat_rotated");
      expect(stored?.refreshToken).toBe("refresh_rotated");
    });
  });

  describe("PERMANENT failures — all must return 404 so the agent flags reauth_required", () => {
    it("returns 404 when there is no session row", async () => {
      // No seed: the empty table IS the case under test.
      expect((await callAction()).status).toBe(404);
      expect(refreshToken).not.toHaveBeenCalled();
    });

    it("returns 404 on invalid_grant — the 90-day-idle / dead refresh chain", async () => {
      // A session row EXISTS, but Shopify rejects the refresh token. The library would flatten
      // this into an anonymous Response(500); if the route let that become a 502, the agent
      // would treat a permanently dead shop as a transient blip and retry it forever.
      await seedSession({ expiresInMs: 60 * 1000 });
      refreshToken.mockRejectedValue(oauthError("invalid_grant"));

      const response = await callAction();

      expect(response.status).toBe(404);
      await expect(response.json()).resolves.toMatchObject({ reauth_required: true });
      // This is the dead-chain mapping, reached via a real refresh — not a missing row.
      expect(refreshToken).toHaveBeenCalledTimes(1);
      // A dead chain must not be persisted.
      const stored = await realSessionStorage.loadSession(SESSION_ID);
      expect(stored?.accessToken).toBe(TOKEN);
    });

    it("returns the same 404 for invalid_grant as for a missing session row", async () => {
      // Missing row: no seed → fast-path 404.
      const missingRow = (await callAction()).status;

      // Dead chain: a seeded row whose refresh Shopify rejects.
      await seedSession({ expiresInMs: 60 * 1000 });
      refreshToken.mockRejectedValue(oauthError("invalid_grant"));
      const deadChain = (await callAction()).status;

      // Both are permanent. If these ever diverge, one of them is being retried forever.
      expect(deadChain).toBe(missingRow);
      expect(refreshToken).toHaveBeenCalledTimes(1);
    });

    it.each(["invalid_subject_token", "invalid_token", "unauthorized_client", "invalid_client"])(
      "returns 404 on %s",
      async (code) => {
        await seedSession({ expiresInMs: 60 * 1000 });
        refreshToken.mockRejectedValue(oauthError(code));

        expect((await callAction()).status).toBe(404);
        expect(refreshToken).toHaveBeenCalledTimes(1);
      },
    );

    it("returns 404 on InvalidJwtError", async () => {
      await seedSession({ expiresInMs: 60 * 1000 });
      refreshToken.mockRejectedValue(new InvalidJwtError("bad jwt"));

      expect((await callAction()).status).toBe(404);
      expect(refreshToken).toHaveBeenCalledTimes(1);
    });

    it("returns 404 when an expired session has no refresh token at all", async () => {
      await seedSession({ expiresInMs: 60 * 1000, refreshToken: null });

      expect((await callAction()).status).toBe(404);
      expect(refreshToken).not.toHaveBeenCalled();
    });

    it("returns 404 when the refresh token itself has expired (90-day-idle shop)", async () => {
      // The refresh chain is visibly dead before we even ask Shopify, so don't ask.
      await seedSession({ expiresInMs: 60 * 1000, refreshTokenExpiresInMs: -1000 });

      expect((await callAction()).status).toBe(404);
      expect(refreshToken).not.toHaveBeenCalled();
    });
  });

  describe("TRANSIENT failures — must NOT return 404", () => {
    it("returns 502 when Shopify is unreachable", async () => {
      await seedSession({ expiresInMs: 60 * 1000 });
      refreshToken.mockRejectedValue(new Error("ECONNRESET"));

      expect((await callAction()).status).toBe(502);
      expect(refreshToken).toHaveBeenCalledTimes(1);
    });

    it("returns 502 on a 5xx from the token endpoint", async () => {
      await seedSession({ expiresInMs: 60 * 1000 });
      refreshToken.mockRejectedValue(oauthError("server_error", 500));

      // A server fault is not a rejected grant — retrying can still succeed.
      expect((await callAction()).status).toBe(502);
      expect(refreshToken).toHaveBeenCalledTimes(1);
    });
  });
});
