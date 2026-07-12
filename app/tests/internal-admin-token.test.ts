/**
 * The internal admin-token route is the agent's only way to obtain a Shopify token, and
 * the app shell's only job as the single refresh authority.
 *
 * The status code is load-bearing: the agent treats 404 as PERMANENT (flag the shop
 * reauth_required) and everything else as TRANSIENT (retry). A dead refresh chain
 * (invalid_grant) must therefore return the SAME 404 as a missing session row — mapping
 * it to 502 would retry a 90-day-idle shop forever and it would never be flagged.
 */

import { HttpResponseError, InvalidJwtError } from "@shopify/shopify-api";
import { beforeEach, describe, expect, it, vi } from "vitest";

const SHOP = "quixly-dev.myshopify.com";
const KEY = "test-internal-key";
const TOKEN = "shpat_secret";

const loadSession = vi.fn();
const storeSession = vi.fn();
const refreshToken = vi.fn();

vi.mock("../app/shopify.server", () => ({
  sessionStorage: {
    loadSession: (id: string) => loadSession(id),
    storeSession: (session: unknown) => storeSession(session),
  },
  api: {
    session: { getOfflineId: (shop: string) => `offline_${shop}` },
    auth: { refreshToken: (args: unknown) => refreshToken(args) },
  },
}));

const { action } = await import("../app/routes/internal.shops.$shop.admin-token");

/** A stored offline session. `expiresInMs` drives whether a refresh is attempted. */
function session({
  expiresInMs = 60 * 60 * 1000,
  refreshToken: rt = "refresh_abc",
  refreshTokenExpiresInMs = 90 * 24 * 60 * 60 * 1000,
  accessToken = TOKEN,
}: {
  expiresInMs?: number;
  refreshToken?: string | null;
  refreshTokenExpiresInMs?: number | null;
  accessToken?: string;
} = {}) {
  const expires = new Date(Date.now() + expiresInMs);
  return {
    accessToken,
    refreshToken: rt,
    refreshTokenExpires:
      refreshTokenExpiresInMs === null ? null : new Date(Date.now() + refreshTokenExpiresInMs),
    expires,
    // Mirrors Session.isExpired(withinMillisecondsOfExpiry).
    isExpired: (within = 0) => expires.getTime() - within <= Date.now(),
  };
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
  beforeEach(() => {
    vi.stubEnv("INTERNAL_API_KEY", KEY);
    loadSession.mockReset();
    storeSession.mockReset();
    refreshToken.mockReset();
  });

  describe("shared-secret guard", () => {
    it("rejects a request with no key", async () => {
      expect(await statusOf(callAction({}))).toBe(401);
      expect(loadSession).not.toHaveBeenCalled();
    });

    it("rejects a wrong key", async () => {
      expect(await statusOf(callAction({ "x-internal-api-key": "wrong" }))).toBe(401);
      expect(loadSession).not.toHaveBeenCalled();
    });

    it("rejects a key that is a prefix of the real one", async () => {
      const status = await statusOf(callAction({ "x-internal-api-key": KEY.slice(0, -1) }));
      expect(status).toBe(401);
    });

    it("fails closed when INTERNAL_API_KEY is unset", async () => {
      vi.stubEnv("INTERNAL_API_KEY", "");
      expect(await statusOf(callAction({ "x-internal-api-key": "anything" }))).toBe(500);
      expect(loadSession).not.toHaveBeenCalled();
    });
  });

  describe("happy path", () => {
    it("returns a still-valid token without refreshing", async () => {
      const stored = session();
      loadSession.mockResolvedValue(stored);

      const response = await callAction();

      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toEqual({
        access_token: TOKEN,
        expires_at: stored.expires.toISOString(),
      });
      expect(refreshToken).not.toHaveBeenCalled();
    });

    it("refreshes a near-expiry token and persists the rotation", async () => {
      // Inside the 5-minute window, so a refresh is required.
      loadSession.mockResolvedValue(session({ expiresInMs: 60 * 1000 }));
      const rotated = session({ accessToken: "shpat_rotated" });
      refreshToken.mockResolvedValue({ session: rotated });

      const response = await callAction();

      expect(response.status).toBe(200);
      await expect(response.json()).resolves.toMatchObject({ access_token: "shpat_rotated" });
      // The old token dies the instant the new one is issued — the rotation must be stored.
      expect(storeSession).toHaveBeenCalledWith(rotated);
    });
  });

  describe("PERMANENT failures — all must return 404 so the agent flags reauth_required", () => {
    it("returns 404 when there is no session row", async () => {
      loadSession.mockResolvedValue(null);

      expect((await callAction()).status).toBe(404);
    });

    it("returns 404 on invalid_grant — the 90-day-idle / dead refresh chain", async () => {
      // The case that matters: a session row EXISTS, but Shopify rejects the refresh
      // token. The library would flatten this into an anonymous Response(500); if the
      // route let that become a 502, the agent would treat a permanently dead shop as a
      // transient blip and retry it forever instead of flagging it.
      loadSession.mockResolvedValue(session({ expiresInMs: 60 * 1000 }));
      refreshToken.mockRejectedValue(oauthError("invalid_grant"));

      const response = await callAction();

      expect(response.status).toBe(404);
      await expect(response.json()).resolves.toMatchObject({ reauth_required: true });
      expect(storeSession).not.toHaveBeenCalled();
    });

    it("returns the same 404 for invalid_grant as for a missing session row", async () => {
      loadSession.mockResolvedValue(null);
      const missingRow = (await callAction()).status;

      loadSession.mockResolvedValue(session({ expiresInMs: 60 * 1000 }));
      refreshToken.mockRejectedValue(oauthError("invalid_grant"));
      const deadChain = (await callAction()).status;

      // Both are permanent. If these ever diverge, one of them is being retried forever.
      expect(deadChain).toBe(missingRow);
    });

    it.each(["invalid_subject_token", "invalid_token", "unauthorized_client", "invalid_client"])(
      "returns 404 on %s",
      async (code) => {
        loadSession.mockResolvedValue(session({ expiresInMs: 60 * 1000 }));
        refreshToken.mockRejectedValue(oauthError(code));

        expect((await callAction()).status).toBe(404);
      },
    );

    it("returns 404 on InvalidJwtError", async () => {
      loadSession.mockResolvedValue(session({ expiresInMs: 60 * 1000 }));
      refreshToken.mockRejectedValue(new InvalidJwtError("bad jwt"));

      expect((await callAction()).status).toBe(404);
    });

    it("returns 404 when an expired session has no refresh token at all", async () => {
      loadSession.mockResolvedValue(session({ expiresInMs: 60 * 1000, refreshToken: null }));

      expect((await callAction()).status).toBe(404);
      expect(refreshToken).not.toHaveBeenCalled();
    });

    it("returns 404 when the refresh token itself has expired (90-day-idle shop)", async () => {
      // The refresh chain is visibly dead before we even ask Shopify, so don't ask.
      loadSession.mockResolvedValue(
        session({ expiresInMs: 60 * 1000, refreshTokenExpiresInMs: -1000 }),
      );

      expect((await callAction()).status).toBe(404);
      expect(refreshToken).not.toHaveBeenCalled();
    });
  });

  describe("TRANSIENT failures — must NOT return 404", () => {
    it("returns 502 when Shopify is unreachable", async () => {
      loadSession.mockResolvedValue(session({ expiresInMs: 60 * 1000 }));
      refreshToken.mockRejectedValue(new Error("ECONNRESET"));

      expect((await callAction()).status).toBe(502);
    });

    it("returns 502 on a 5xx from the token endpoint", async () => {
      loadSession.mockResolvedValue(session({ expiresInMs: 60 * 1000 }));
      refreshToken.mockRejectedValue(oauthError("server_error", 500));

      // A server fault is not a rejected grant — retrying can still succeed.
      expect((await callAction()).status).toBe(502);
    });
  });
});
