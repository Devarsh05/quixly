/**
 * The internal admin-token route is the agent's only way to obtain a Shopify token, and
 * the app shell's only job as the single refresh authority. It must be closed to anyone
 * without the shared secret, and must distinguish "shop is gone" from "something broke".
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const SHOP = "quixly-dev.myshopify.com";
const KEY = "test-internal-key";
const TOKEN = "shpat_secret";

const unauthenticatedAdmin = vi.fn();

vi.mock("../app/shopify.server", () => ({
  unauthenticated: {
    admin: (shop: string) => unauthenticatedAdmin(shop),
  },
}));

// SessionNotFoundError is what the library throws when a shop has no session; the route
// keys its 404 off it, so the test needs the real class.
vi.mock("@shopify/shopify-app-react-router/server", async () => {
  class SessionNotFoundError extends Error {}
  return { SessionNotFoundError };
});

const { SessionNotFoundError } = await import("@shopify/shopify-app-react-router/server");
const { action } = await import("../app/routes/internal.shops.$shop.admin-token");

function request(headers: Record<string, string> = {}): Request {
  return new Request(`http://localhost/internal/shops/${SHOP}/admin-token`, {
    method: "POST",
    headers,
  });
}

function callAction(headers: Record<string, string> = {}) {
  // The action only reads `request` and `params`; the rest of ActionFunctionArgs
  // (context, url, pattern) is React Router plumbing it never touches.
  return action({
    request: request(headers),
    params: { shop: SHOP },
  } as unknown as Parameters<typeof action>[0]);
}

/** The route signals auth failure by *throwing* a Response. */
async function statusOf(promise: Promise<unknown>): Promise<number> {
  try {
    const result = await promise;
    return (result as Response).status;
  } catch (thrown) {
    if (thrown instanceof Response) return thrown.status;
    throw thrown;
  }
}

describe("POST /internal/shops/:shop/admin-token", () => {
  beforeEach(() => {
    vi.stubEnv("INTERNAL_API_KEY", KEY);
    unauthenticatedAdmin.mockReset();
  });

  it("rejects a request with no key", async () => {
    expect(await statusOf(callAction())).toBe(401);
    expect(unauthenticatedAdmin).not.toHaveBeenCalled();
  });

  it("rejects a wrong key", async () => {
    const status = await statusOf(callAction({ "x-internal-api-key": "wrong" }));
    expect(status).toBe(401);
    expect(unauthenticatedAdmin).not.toHaveBeenCalled();
  });

  it("rejects a key that is a prefix of the real one", async () => {
    const status = await statusOf(callAction({ "x-internal-api-key": KEY.slice(0, -1) }));
    expect(status).toBe(401);
  });

  it("fails closed when INTERNAL_API_KEY is unset", async () => {
    vi.stubEnv("INTERNAL_API_KEY", "");
    const status = await statusOf(callAction({ "x-internal-api-key": "anything" }));
    expect(status).toBe(500);
    expect(unauthenticatedAdmin).not.toHaveBeenCalled();
  });

  it("returns the token and its expiry with a valid key", async () => {
    const expires = new Date(Date.now() + 60 * 60 * 1000);
    unauthenticatedAdmin.mockResolvedValue({
      session: { accessToken: TOKEN, expires },
    });

    const response = (await callAction({ "x-internal-api-key": KEY })) as Response;

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      access_token: TOKEN,
      expires_at: expires.toISOString(),
    });
    expect(unauthenticatedAdmin).toHaveBeenCalledWith(SHOP);
  });

  it("returns 404 when the shop has no session", async () => {
    unauthenticatedAdmin.mockRejectedValue(new SessionNotFoundError("nope"));

    const response = (await callAction({ "x-internal-api-key": KEY })) as Response;

    expect(response.status).toBe(404);
  });

  it("returns 502, not 404, on a transient failure", async () => {
    // A 404 would make the agent permanently flag a healthy shop as needing re-auth.
    unauthenticatedAdmin.mockRejectedValue(new Error("shopify unreachable"));

    const response = (await callAction({ "x-internal-api-key": KEY })) as Response;

    expect(response.status).toBe(502);
  });
});
