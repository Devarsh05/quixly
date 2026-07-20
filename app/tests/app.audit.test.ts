/**
 * The audit route's loader + action. The loader must keep THREE cases distinct — agent
 * unreachable (getReport throws), scan failed / running / completed (200 body), and no runs yet
 * (report === null) — and the action must start a scan and redirect to that run's URL. Shopify
 * auth and the agent client are mocked; the route is imported after the mocks (repo idiom).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const getReport = vi.fn();
const startScan = vi.fn();
const authenticateAdmin = vi.fn();

vi.mock("../app/lib/agent.server", () => ({
  getReport: (...args: unknown[]) => getReport(...args),
  startScan: (...args: unknown[]) => startScan(...args),
}));

vi.mock("../app/shopify.server", () => ({
  authenticate: { admin: (...args: unknown[]) => authenticateAdmin(...args) },
}));

vi.mock("@shopify/shopify-app-react-router/server", () => ({
  boundary: { headers: vi.fn(), error: vi.fn() },
}));

const { loader, action } = await import("../app/routes/app.audit");

const SHOP = "audit-shop.myshopify.com";

function callLoader(url = "http://localhost/app/audit") {
  return loader({
    request: new Request(url),
  } as unknown as Parameters<typeof loader>[0]);
}

function callAction() {
  return action({
    request: new Request("http://localhost/app/audit", { method: "POST" }),
  } as unknown as Parameters<typeof action>[0]) as Promise<Response>;
}

beforeEach(() => {
  getReport.mockReset();
  startScan.mockReset();
  authenticateAdmin.mockReset();
  authenticateAdmin.mockResolvedValue({ session: { shop: SHOP } });
});

describe("app.audit loader", () => {
  it("returns the report and agentReachable when the agent responds", async () => {
    const report = {
      run_id: 1,
      status: "completed",
      period: null,
      started_at: null,
      completed_at: null,
      engines: [],
    };
    getReport.mockResolvedValue(report);

    const result = await callLoader();

    expect(result).toEqual({ report, agentReachable: true });
    expect(getReport).toHaveBeenCalledWith(SHOP, undefined);
  });

  it("passes a numeric run_id from the query string to getReport", async () => {
    getReport.mockResolvedValue(null);

    await callLoader("http://localhost/app/audit?run_id=42");

    expect(getReport).toHaveBeenCalledWith(SHOP, 42);
  });

  it("ignores a non-numeric run_id", async () => {
    getReport.mockResolvedValue(null);

    await callLoader("http://localhost/app/audit?run_id=abc");

    expect(getReport).toHaveBeenCalledWith(SHOP, undefined);
  });

  it("returns the empty state (report null) when the shop has never scanned", async () => {
    getReport.mockResolvedValue(null);

    const result = await callLoader();

    expect(result).toEqual({ report: null, agentReachable: true });
  });

  it("degrades to agentReachable:false when getReport throws (distinct from no-runs)", async () => {
    getReport.mockRejectedValue(new Error("agent down"));

    const result = await callLoader();

    expect(result).toEqual({ report: null, agentReachable: false });
  });
});

describe("app.audit action", () => {
  it("starts a scan and redirects to that run's URL", async () => {
    startScan.mockResolvedValue({ run_id: 99, status: "running" });

    const response = await callAction();

    expect(startScan).toHaveBeenCalledWith(SHOP);
    expect(response.status).toBe(302);
    expect(response.headers.get("Location")).toBe("/app/audit?run_id=99");
  });
});
