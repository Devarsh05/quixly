/**
 * The uplift page, actually RENDERED.
 *
 * The loader test proves the data reaches the component; this proves what the component does with
 * it. Rendering to static markup needs no DOM, so it works in this harness — `react-router` is
 * mocked down to `useLoaderData`, which is the only runtime import the route takes from it.
 *
 * The assertions here are the merchant-facing invariants stated as output, not as intent:
 * an unsettled run must not produce a delta figure or a chart, and a no-data engine must not
 * produce a number at all.
 */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EngineDelta, VerificationRun } from "../app/lib/agent.server";

let loaderData: unknown = { series: null, agentReachable: true };

vi.mock("react-router", () => ({
  useLoaderData: () => loaderData,
}));
vi.mock("../app/lib/agent.server", () => ({
  getVerificationSeries: vi.fn(),
}));
vi.mock("../app/shopify.server", () => ({
  authenticate: { admin: vi.fn() },
}));
vi.mock("@shopify/shopify-app-react-router/server", () => ({
  boundary: { headers: vi.fn(), error: vi.fn() },
}));

const Uplift = (await import("../app/routes/app.uplift")).default;

function engine(overrides: Partial<EngineDelta> = {}): EngineDelta {
  return {
    engine: "perplexity",
    pre_rate: 0.0,
    post_rate: 0.0,
    delta: 0.0,
    pre_mentions: 0,
    post_mentions: 0,
    pre_total_queries: 24,
    post_total_queries: 24,
    competitors: {},
    state: "no_movement",
    ...overrides,
  };
}

function run(overrides: Partial<VerificationRun> = {}): VerificationRun {
  return {
    run_id: 2787,
    baseline_run_id: 137,
    status: "completed",
    panel_fingerprint: "1c4539c5",
    published_at_max: "2026-07-28T14:52:09.500127Z",
    settle_hours: 12.60675095388889,
    settle_satisfied: false,
    measured_fixes: [],
    engines: [engine()],
    state: "unsettled",
    deltas_reportable: false,
    measured_at: "2026-07-29T03:29:09.494766Z",
    settle_hours_required: 168,
    ...overrides,
  };
}

function render(series: { runs: VerificationRun[] } | null, agentReachable = true): string {
  loaderData = { series, agentReachable };
  return renderToStaticMarkup(createElement(Uplift));
}

describe("uplift page rendering", () => {
  beforeEach(() => {
    loaderData = { series: null, agentReachable: true };
  });

  it("renders run 2787 as measurement pending — NOT as 0% uplift", () => {
    // THE ACCEPTANCE CASE. delta = 0.0 with settle_satisfied = false measured nothing.
    const html = render({ runs: [run()] });

    expect(html).toContain("Measurement pending");
    expect(html).toContain("Reading so far");
    // No delta figure anywhere: not "0 points", not "+0 points", not "No change".
    expect(html).not.toContain("points");
    expect(html).not.toContain("No change");
    // And no chart — the bars are reserved for a settled result.
    expect(html).not.toContain("<svg");
  });

  it("shows the unsettled run's elapsed hours and the date a real result is available", () => {
    const html = render({ runs: [run()] });

    expect(html).toContain("13 of the 168 hours");
    expect(html).toContain("2026-08-04");
  });

  it("still shows the unsettled rates, framed as a reading rather than a result", () => {
    const html = render({ runs: [run()] });

    expect(html).toContain("before 0%, after 0%");
    expect(html).toContain("This is an early reading, not a result.");
  });

  it("renders a settled improvement as a delta with a chart", () => {
    const html = render({
      runs: [
        run({
          state: "settled",
          settle_satisfied: true,
          settle_hours: 170,
          engines: [
            engine({
              pre_rate: 0.125,
              post_rate: 0.25,
              delta: 0.125,
              post_mentions: 6,
              state: "improved",
            }),
          ],
        }),
      ],
    });

    expect(html).toContain("+13 points");
    expect(html).toContain("before 13%, after 25%");
    expect(html).toContain("<svg");
    expect(html).not.toContain("Measurement pending");
  });

  it("renders a settled zero delta as 'No change', distinct from no data", () => {
    const html = render({
      runs: [
        run({
          state: "settled",
          settle_satisfied: true,
          engines: [engine({ pre_rate: 0.25, post_rate: 0.25, delta: 0.0 })],
        }),
      ],
    });

    expect(html).toContain("No change");
    expect(html).toContain("before 25%, after 25%");
    expect(html).not.toContain("No data this period");
  });

  it("renders a NULL post rate as no data — never as 0% and never as a decline", () => {
    const html = render({
      runs: [
        run({
          state: "settled",
          settle_satisfied: true,
          engines: [
            engine({
              pre_rate: 0.5,
              post_rate: null,
              delta: null,
              post_mentions: null,
              post_total_queries: 0,
              state: "no_data_post",
            }),
          ],
        }),
      ],
    });

    expect(html).toContain("No data this period");
    expect(html).toContain("not a change in your visibility");
    expect(html).not.toContain("points");
    expect(html).not.toContain("0%");
    expect(html).not.toContain("<svg");
  });

  it("renders the measured fixes as provenance, never as attribution", () => {
    const html = render({
      runs: [
        run({
          state: "settled",
          settle_satisfied: true,
          engines: [engine({ pre_rate: 0.1, post_rate: 0.2, delta: 0.1, state: "improved" })],
          measured_fixes: [
            {
              fix_id: 9710,
              product_id: 115,
              type: "category",
              target: "category",
              published_at: "2026-07-28T14:51:27.129221Z",
            },
            {
              fix_id: 9702,
              product_id: 114,
              type: "description",
              target: "body_html",
              published_at: "2026-07-28T14:52:09.500127Z",
            },
          ],
        }),
      ],
    });

    expect(html).toContain("Measured after 2 fixes went live");
    // Causal language is the thing the (run, engine) grain exists to prevent.
    expect(html).not.toMatch(/caused by/i);
    expect(html).not.toMatch(/because of/i);
  });

  it("renders an empty series as an invitation, not an error", () => {
    const html = render({ runs: [] });

    expect(html).toContain("No uplift measurement yet");
    expect(html).not.toContain("Couldn't reach");
  });

  it("renders an unreachable agent as a distinct, temporary failure", () => {
    const html = render(null, false);

    expect(html).toContain("Couldn&#x27;t reach the measurement service");
    expect(html).not.toContain("No uplift measurement yet");
  });

  it("renders a failed measurement without implying anything changed on the store", () => {
    const html = render({ runs: [run({ status: "failed", state: "failed" })] });

    expect(html).toContain("Measurement didn&#x27;t finish");
    expect(html).toContain("Nothing on your store was changed");
    expect(html).not.toContain("points");
  });
});
