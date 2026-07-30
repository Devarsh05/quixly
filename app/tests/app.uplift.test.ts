/**
 * The uplift route's loader (app/routes/app.uplift.tsx).
 *
 * Loader-only by necessity: this harness runs `environment: "node"` with no DOM and no renderer,
 * so the display states themselves are asserted agent-side in
 * `agent/tests/test_uplift_states.py`. What CAN be checked here is the boundary — that the shop
 * comes from the authenticated session, that an unreachable agent degrades rather than throws,
 * and that the route stays read-only.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const getVerificationSeries = vi.fn();
const authenticateAdmin = vi.fn();

vi.mock("../app/lib/agent.server", () => ({
  getVerificationSeries: (...args: unknown[]) => getVerificationSeries(...args),
}));
vi.mock("../app/shopify.server", () => ({
  authenticate: { admin: (...args: unknown[]) => authenticateAdmin(...args) },
}));
vi.mock("@shopify/shopify-app-react-router/server", () => ({
  boundary: { headers: vi.fn(), error: vi.fn() },
}));

const route = await import("../app/routes/app.uplift");
const { loader, formatDelta, formatPct, settlesOn } = route;

const SHOP = "uplift-shop.myshopify.com";

function callLoader(url = "http://localhost/app/uplift") {
  return loader({ request: new Request(url) } as unknown as Parameters<typeof loader>[0]);
}

describe("app.uplift loader", () => {
  beforeEach(() => {
    getVerificationSeries.mockReset();
    authenticateAdmin.mockReset();
    authenticateAdmin.mockResolvedValue({ session: { shop: SHOP } });
  });

  it("returns the series for the authenticated shop", async () => {
    const series = { runs: [{ run_id: 2787, state: "unsettled", deltas_reportable: false }] };
    getVerificationSeries.mockResolvedValue(series);

    const data = await callLoader();

    expect(getVerificationSeries).toHaveBeenCalledWith(SHOP);
    expect(data).toEqual({ series, agentReachable: true });
  });

  it("ignores a shop supplied in the query string, using the session shop", async () => {
    getVerificationSeries.mockResolvedValue({ runs: [] });

    await callLoader("http://localhost/app/uplift?shop=attacker.myshopify.com");

    expect(getVerificationSeries).toHaveBeenCalledWith(SHOP);
  });

  it("degrades to agentReachable: false when the agent throws", async () => {
    getVerificationSeries.mockRejectedValue(new Error("ECONNREFUSED"));

    expect(await callLoader()).toEqual({ series: null, agentReachable: false });
  });

  it("distinguishes an unknown shop (null) from one with no measurements ([])", async () => {
    getVerificationSeries.mockResolvedValue(null);
    expect(await callLoader()).toEqual({ series: null, agentReachable: true });

    getVerificationSeries.mockResolvedValue({ runs: [] });
    expect(await callLoader()).toEqual({ series: { runs: [] }, agentReachable: true });
  });

  it("exports no action — the page is read-only and can never trigger a measurement", () => {
    expect("action" in route).toBe(false);
  });
});

describe("uplift display arithmetic", () => {
  it("renders a rate as a whole percent", () => {
    expect(formatPct(0.0)).toBe("0%");
    expect(formatPct(0.125)).toBe("13%");
    expect(formatPct(1)).toBe("100%");
  });

  it("renders a delta in percentage POINTS, signed", () => {
    // A difference between two rates is points, not percent — calling it "%" would overstate it.
    expect(formatDelta(0.125)).toBe("+13 points");
    expect(formatDelta(-0.0416)).toBe("-4 points");
    expect(formatDelta(0)).toBe("0 points");
  });

  it("keeps a decimal for a small movement rather than rounding to a misleading +0", () => {
    expect(formatDelta(0.004)).toBe("+0.4 points");
    expect(formatDelta(-0.004)).toBe("-0.4 points");
  });

  it("derives the settle date from the publish anchor, as a stable ISO date", () => {
    // Not locale-formatted: this string is produced on the server AND in the browser, and a
    // locale-dependent one would differ between them.
    const run = {
      published_at_max: "2026-07-28T14:52:09.500127Z",
      settle_hours_required: 168,
    } as Parameters<typeof settlesOn>[0];

    expect(settlesOn(run)).toBe("2026-08-04");
  });

  it("returns null when there is no publish anchor to count from", () => {
    const run = {
      published_at_max: null,
      settle_hours_required: 168,
    } as Parameters<typeof settlesOn>[0];

    expect(settlesOn(run)).toBeNull();
  });
});
