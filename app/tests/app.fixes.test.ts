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
const publishFixes = vi.fn();
const authenticateAdmin = vi.fn();

// AgentError is a real class, not a stub: the action branches on `instanceof`, so a mock that
// replaced it would let a broken branch pass.
class AgentError extends Error {
  constructor(
    readonly status: number,
    path: string,
  ) {
    super(`Agent ${path} returned ${status}`);
    this.name = "AgentError";
  }
}

vi.mock("../app/lib/agent.server", () => ({
  AgentError,
  getFixes: (...args: unknown[]) => getFixes(...args),
  startFixRun: (...args: unknown[]) => startFixRun(...args),
  decideFix: (...args: unknown[]) => decideFix(...args),
  publishFixes: (...args: unknown[]) => publishFixes(...args),
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

function callAction(body: Record<string, string>, url = "http://localhost/app/fixes") {
  const form = new URLSearchParams(body);
  return action({
    request: new Request(url, { method: "POST", body: form }),
  } as unknown as Parameters<typeof action>[0]) as Promise<Response>;
}

/** The action's non-redirect exit: a plain object rendered as a banner, never a thrown response. */
type DecisionError = { error: { decision: string; status: number | null } };

function callActionForData(body: Record<string, string>, url?: string) {
  return callAction(body, url) as unknown as Promise<DecisionError>;
}

/**
 * A real embedded-app URL. Shopify appends these one-time auth params to every page load, and
 * `request.url` inside the action carries all of them — which is exactly why redirecting to
 * `request.url` dead-ended the merchant on a raw `200` body.
 */
const EMBEDDED_URL =
  "https://p01--quixly-app--x.code.run/app/fixes?embedded=1&hmac=abc123" +
  "&host=YWRtaW4uc2hvcGlmeS5jb20&id_token=eyJhbGciOiJIUzI1NiJ9.payload.sig" +
  "&locale=en&session=deadbeef&shop=fixes-shop.myshopify.com&timestamp=1754179200";

beforeEach(() => {
  getFixes.mockReset();
  startFixRun.mockReset();
  decideFix.mockReset();
  publishFixes.mockReset();
  authenticateAdmin.mockReset();
  authenticateAdmin.mockResolvedValue({ session: { shop: SHOP } });
});

describe("app.fixes loader", () => {
  it("asks the agent for the SESSION's shop, never a request-supplied one", async () => {
    getFixes.mockResolvedValue(EMPTY);

    await callLoader("http://localhost/app/fixes?shop=attacker.myshopify.com");

    expect(getFixes).toHaveBeenCalledWith(SHOP, undefined, undefined);
  });

  it("threads a numeric run_id through and ignores a non-numeric one", async () => {
    getFixes.mockResolvedValue(EMPTY);

    await callLoader("http://localhost/app/fixes?run_id=42");
    expect(getFixes).toHaveBeenCalledWith(SHOP, 42, undefined);

    getFixes.mockClear();
    await callLoader("http://localhost/app/fixes?run_id=not-a-number");
    expect(getFixes).toHaveBeenCalledWith(SHOP, undefined, undefined);
  });

  it("threads publish_run_id through so a publish in flight can be polled", async () => {
    getFixes.mockResolvedValue(EMPTY);

    await callLoader("http://localhost/app/fixes?run_id=42&publish_run_id=99");
    expect(getFixes).toHaveBeenCalledWith(SHOP, 42, 99);

    getFixes.mockClear();
    await callLoader("http://localhost/app/fixes?publish_run_id=oops");
    expect(getFixes).toHaveBeenCalledWith(SHOP, undefined, undefined);
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

  it("publishes for the SESSION's shop and carries no fix ids", async () => {
    // The publish request must not be able to name what gets written: the work set is the shop's
    // approved rows, so a crafted form field can never widen it past what was approved.
    publishFixes.mockResolvedValue({ run_id: 77, status: "running" });

    const response = await callAction({ intent: "publish", fix_id: "1234" });

    expect(publishFixes).toHaveBeenCalledWith(SHOP);
    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe("/app/fixes?publish_run_id=77");
  });

  it("keeps the reviewed run in scope when publishing from a run-scoped URL", async () => {
    publishFixes.mockResolvedValue({ run_id: 78, status: "running" });

    const response = await callAction(
      { intent: "publish" },
      "http://localhost/app/fixes?run_id=42",
    );

    expect(response.headers.get("location")).toBe("/app/fixes?run_id=42&publish_run_id=78");
  });

  it("never publishes while approving or running — publishing is its own explicit action", async () => {
    decideFix.mockResolvedValue({ fix_id: 5, status: "approved" });
    startFixRun.mockResolvedValue({ run_id: 91, status: "running" });

    await callAction({ intent: "approve", fix_id: "5" });
    await callAction({ intent: "run" });

    expect(publishFixes).not.toHaveBeenCalled();
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

/**
 * The approve/reject exit path.
 *
 * This is the branch that dead-ended the merchant on a blank page reading only "200": it
 * redirected to `request.url`, which in an embedded app replays Shopify's one-time auth params
 * and throws an `ErrorResponse` — and `boundary.error` renders an `ErrorResponse` as its raw
 * body in a bare div, with no app chrome and no way back. The earlier tests above asserted only
 * that `decideFix` was *called*, never what came back, which is how it shipped.
 */
describe("app.fixes action — the decision's exit path", () => {
  beforeEach(() => {
    decideFix.mockResolvedValue({ fix_id: 5, status: "approved" });
  });

  it("redirects approve to a RELATIVE /app/fixes, never an absolute URL", async () => {
    const response = await callAction({ intent: "approve", fix_id: "5" }, EMBEDDED_URL);

    expect(response.status).toBe(302);
    const location = response.headers.get("location");
    expect(location).toBe("/app/fixes");
    expect(location?.startsWith("http")).toBe(false);
  });

  it("strips Shopify's one-time auth params from the redirect — the regression guard", async () => {
    // Replaying id_token/hmac/session is what re-entered authenticate.admin with a spent token.
    const response = await callAction({ intent: "approve", fix_id: "5" }, EMBEDDED_URL);

    const location = response.headers.get("location") ?? "";
    for (const param of ["id_token", "hmac", "session", "timestamp", "host", "embedded"]) {
      expect(location).not.toContain(param);
    }
  });

  it("keeps run_id and publish_run_id so the page stays scoped to the run under review", async () => {
    const response = await callAction(
      { intent: "approve", fix_id: "5" },
      "http://localhost/app/fixes?run_id=42&publish_run_id=99",
    );

    expect(response.headers.get("location")).toBe("/app/fixes?run_id=42&publish_run_id=99");
  });

  it("ignores a non-numeric run_id rather than echoing it back into the URL", async () => {
    const response = await callAction(
      { intent: "approve", fix_id: "5" },
      "http://localhost/app/fixes?run_id=not-a-number",
    );

    expect(response.headers.get("location")).toBe("/app/fixes");
  });

  it("redirects reject the same way", async () => {
    decideFix.mockResolvedValue({ fix_id: 6, status: "rejected" });

    const response = await callAction({ intent: "reject", fix_id: "6" }, EMBEDDED_URL);

    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe("/app/fixes");
  });

  it("returns a 409 refusal as data — it never throws into the error boundary", async () => {
    // Throwing here is what produced the raw-body dead end. The page must stay alive.
    decideFix.mockRejectedValue(new AgentError(409, "/shops/by-domain/x/fixes/5/approve"));

    const result = await callActionForData({ intent: "approve", fix_id: "5" }, EMBEDDED_URL);

    expect(result).toEqual({ error: { decision: "approve", status: 409 } });
  });

  it("returns a transport failure as data with a null status", async () => {
    decideFix.mockRejectedValue(new Error("ECONNREFUSED"));

    const result = await callActionForData({ intent: "reject", fix_id: "6" });

    expect(result).toEqual({ error: { decision: "reject", status: null } });
  });

  it("never leaks the agent's internal path to the merchant", async () => {
    // The thrown message embeds /shops/by-domain/... — only the status may cross into the UI.
    decideFix.mockRejectedValue(new AgentError(502, "/shops/by-domain/x/fixes/5/approve"));

    const result = await callActionForData({ intent: "approve", fix_id: "5" });

    expect(JSON.stringify(result)).not.toContain("by-domain");
    expect(result.error.status).toBe(502);
  });
});
