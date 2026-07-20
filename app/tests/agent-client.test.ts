/**
 * The agent scan/report client (agent.server.ts) — the app's server-only proxy to the agent.
 * Hermetic: only the global fetch and env are stubbed (no Postgres, no Shopify). The point is
 * that the internal key + agent URL are used correctly server-side and never leak.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getReport, startScan } from "../app/lib/agent.server";

const AGENT_URL = "http://agent.test";
const KEY = "test-internal-key";
const SHOP = "audit-shop.myshopify.com";

const fetchMock = vi.fn();

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function headerOf(init: unknown, name: string): string | undefined {
  const headers = (init as { headers?: Record<string, string> }).headers ?? {};
  return headers[name];
}

describe("agent.server scan/report client", () => {
  beforeEach(() => {
    vi.stubEnv("AGENT_SERVICE_URL", AGENT_URL);
    vi.stubEnv("INTERNAL_API_KEY", KEY);
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  describe("startScan", () => {
    it("POSTs to the by-domain scan URL with the internal key header", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ run_id: 7, status: "running" }));

      const result = await startScan(SHOP);

      expect(result).toEqual({ run_id: 7, status: "running" });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0];
      expect(url).toBe(`${AGENT_URL}/shops/by-domain/${encodeURIComponent(SHOP)}/scan`);
      expect((init as { method: string }).method).toBe("POST");
      expect(headerOf(init, "X-Internal-Api-Key")).toBe(KEY);
    });

    it("throws on a non-OK response", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ detail: "nope" }, 500));
      await expect(startScan(SHOP)).rejects.toThrow(/500/);
    });

    it("throws when AGENT_SERVICE_URL is unset", async () => {
      vi.stubEnv("AGENT_SERVICE_URL", "");
      await expect(startScan(SHOP)).rejects.toThrow(/AGENT_SERVICE_URL/);
    });

    it("throws when INTERNAL_API_KEY is unset", async () => {
      vi.stubEnv("INTERNAL_API_KEY", "");
      await expect(startScan(SHOP)).rejects.toThrow(/INTERNAL_API_KEY/);
    });
  });

  describe("getReport", () => {
    const report = {
      run_id: 7,
      status: "completed",
      period: "2026-07-20",
      started_at: null,
      completed_at: null,
      engines: [
        {
          engine: "perplexity",
          our_rate: 0.0,
          our_mentions: 0,
          total_queries: 24,
          coverage: 1.0,
          competitor_rates: { "Blue Bottle": { mention_rate: 0.0, mentions: 0 } },
        },
      ],
    };

    it("GETs the by-domain report URL with the internal key and returns the body", async () => {
      fetchMock.mockResolvedValue(jsonResponse(report));

      const result = await getReport(SHOP);

      expect(result).toEqual(report);
      const [url, init] = fetchMock.mock.calls[0];
      expect(url).toBe(`${AGENT_URL}/shops/by-domain/${encodeURIComponent(SHOP)}/report`);
      expect(headerOf(init, "X-Internal-Api-Key")).toBe(KEY);
    });

    it("threads run_id as a query param", async () => {
      fetchMock.mockResolvedValue(jsonResponse({ ...report, run_id: 42 }));

      await getReport(SHOP, 42);

      const [url] = fetchMock.mock.calls[0];
      expect(url).toBe(`${AGENT_URL}/shops/by-domain/${encodeURIComponent(SHOP)}/report?run_id=42`);
    });

    it("returns null on 404 (the shop has never scanned)", async () => {
      fetchMock.mockResolvedValue(new Response("", { status: 404 }));
      expect(await getReport(SHOP)).toBeNull();
    });

    it("throws on a non-OK, non-404 response", async () => {
      fetchMock.mockResolvedValue(new Response("", { status: 502 }));
      await expect(getReport(SHOP)).rejects.toThrow(/502/);
    });
  });
});
