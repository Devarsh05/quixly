/**
 * The approval-gate route's loader + action.
 *
 * The load-bearing assertions are the ones that keep the gate honest:
 *  - the shop identity comes from the authenticated SESSION, never from the request — it is what
 *    scopes a decision to its owner;
 *  - the action never calls anything that writes to Shopify; it only calls decideFix/startFixRun;
 *  - an unreachable agent DEGRADES the page (banner) instead of throwing, and is distinct from
 *    "this shop has no fixes yet" (run_id === null).
 *
 * Shopify auth and the agent client are mocked; the route is imported after the mocks (repo idiom).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const getFixes = vi.fn();
const startFixRun = vi.fn();
const decideFix = vi.fn();
const authenticateAdmin = vi.fn();

vi.mock("../app/lib/agent.server", () => ({
  getFixes: (...args: unknown[]) => getFixes(...args),
  startFixRun: (...args: unknown[]) => startFixRun(...args),
  decideFix: (...args: unknown[]) => decideFix(...args),
}));

vi.mock("../app/shopify.server", () => ({
  authenticate: { admin: (...args: unknown[]) => authenticateAdmin(...args) },
}));

vi.mock("@shopify/shopify-app-react-router/server", () => ({
  boundary: { headers: vi.fn(), error: vi.fn() },
}));

const { loader, action } = await import("../app/routes/app.fixes");

const SHOP = "fixes-shop.myshopify.com";
const EMPTY = { run_id: null, status: null, products: [] };

function callLoader(url = "http://localhost/app/fixes") {
  return loader({
    request: new Request(url),
  } as unknown as Parameters<typeof loader>[0]);
}

function callAction(body: Record<string, string>) {
  const form = new URLSearchParams(body);
  return action({
    request: new Request("http://localhost/app/fixes", { method: "POST", body: form }),
  } as unknown as Parameters<typeof action>[0]) as Promise<Response>;
}

beforeEach(() => {
  getFixes.mockReset();
  startFixRun.mockReset();
  decideFix.mockReset();
  authenticateAdmin.mockReset();
  authenticateAdmin.mockResolvedValue({ session: { shop: SHOP } });
});

describe("app.fixes loader", () => {
  it("asks the agent for the SESSION's shop, never a request-supplied one", async () => {
    getFixes.mockResolvedValue(EMPTY);

    await callLoader("http://localhost/app/fixes?shop=attacker.myshopify.com");

    expect(getFixes).toHaveBeenCalledWith(SHOP, undefined);
  });

  it("threads a numeric run_id through and ignores a non-numeric one", async () => {
    getFixes.mockResolvedValue(EMPTY);

    await callLoader("http://localhost/app/fixes?run_id=42");
    expect(getFixes).toHaveBeenCalledWith(SHOP, 42);

    getFixes.mockClear();
    await callLoader("http://localhost/app/fixes?run_id=not-a-number");
    expect(getFixes).toHaveBeenCalledWith(SHOP, undefined);
  });

  it("returns the payload with agentReachable when the agent responds", async () => {
    const payload = { run_id: 7, status: "completed", products: [] };
    getFixes.mockResolvedValue(payload);

    await expect(callLoader()).resolves.toEqual({ fixes: payload, agentReachable: true });
  });

  it("degrades to agentReachable:false instead of throwing when the agent is down", async () => {
    getFixes.mockRejectedValue(new Error("ECONNREFUSED"));

    await expect(callLoader()).resolves.toEqual({ fixes: null, agentReachable: false });
  });

  it("keeps 'no fixes yet' distinct from 'agent unreachable'", async () => {
    getFixes.mockResolvedValue(EMPTY);

    const result = await callLoader();

    expect(result.agentReachable).toBe(true);
    expect(result.fixes?.run_id).toBeNull();
  });
});

describe("app.fixes action", () => {
  it("starts a fix run and redirects to that run's URL", async () => {
    startFixRun.mockResolvedValue({ run_id: 91, status: "running" });

    const response = await callAction({ intent: "run" });

    expect(startFixRun).toHaveBeenCalledWith(SHOP);
    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe("/app/fixes?run_id=91");
  });

  it("approves a fix for the SESSION's shop", async () => {
    decideFix.mockResolvedValue({ fix_id: 5, status: "approved" });

    await callAction({ intent: "approve", fix_id: "5" });

    expect(decideFix).toHaveBeenCalledWith(SHOP, 5, "approve");
  });

  it("rejects a fix", async () => {
    decideFix.mockResolvedValue({ fix_id: 6, status: "rejected" });

    await callAction({ intent: "reject", fix_id: "6" });

    expect(decideFix).toHaveBeenCalledWith(SHOP, 6, "reject");
  });

  it("treats any non-approve intent as a reject — never approves by accident", async () => {
    // A malformed/unknown intent must not fall through to the destructive-by-default direction.
    decideFix.mockResolvedValue({ fix_id: 7, status: "rejected" });

    await callAction({ intent: "something-else", fix_id: "7" });

    expect(decideFix).toHaveBeenCalledWith(SHOP, 7, "reject");
  });

  it("never starts a run while deciding a fix", async () => {
    decideFix.mockResolvedValue({ fix_id: 8, status: "approved" });

    await callAction({ intent: "approve", fix_id: "8" });

    expect(startFixRun).not.toHaveBeenCalled();
  });
});
