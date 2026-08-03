/**
 * The fixes page, actually RENDERED, on the failure path.
 *
 * The action test proves a refused decision comes back as data instead of throwing. This proves
 * what the page does with it — and the assertion that matters is the negative one: the fixes
 * list is STILL THERE underneath the banner. That is the difference between an error and a dead
 * end, and it is only provable by rendering.
 *
 * The bug this guards: a thrown `ErrorResponse` reaches the app-level boundary, and
 * `boundary.error` renders it as `error.data` inside a bare `dangerouslySetInnerHTML` div — no
 * chrome, no nav, no list. The merchant saw a page containing only the text "200".
 *
 * Rendering to static markup needs no DOM, so this runs in the repo's `environment: "node"`
 * config, exactly like `app.uplift.render.test.ts`.
 */

import { createElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FixList } from "../app/lib/agent.server";

let loaderData: unknown = { fixes: null, agentReachable: true };
let actionData: unknown = undefined;

vi.mock("react-router", () => ({
  useLoaderData: () => loaderData,
  useActionData: () => actionData,
  // The poll effect never runs under renderToStaticMarkup, so a stub is enough.
  useRevalidator: () => ({ revalidate: vi.fn() }),
  Form: ({ children }: { children?: ReactNode }) => createElement("form", null, children),
  redirect: vi.fn(),
}));
vi.mock("../app/lib/agent.server", () => ({
  AgentError: class extends Error {},
  getFixes: vi.fn(),
  startFixRun: vi.fn(),
  decideFix: vi.fn(),
  publishFixes: vi.fn(),
}));
vi.mock("../app/shopify.server", () => ({
  authenticate: { admin: vi.fn() },
}));
vi.mock("@shopify/shopify-app-react-router/server", () => ({
  boundary: { headers: vi.fn(), error: vi.fn() },
}));

const Fixes = (await import("../app/routes/app.fixes")).default;

/** One product with one approvable description fix — enough to prove the list survived. */
function fixList(): FixList {
  return {
    run_id: 7,
    status: "completed",
    publish_run_id: null,
    publish_status: null,
    products: [
      {
        product_id: 113,
        title: "Ethiopia Yirgacheffe",
        severity: "high",
        approvable: [
          {
            id: 5,
            type: "description",
            target: "descriptionHtml",
            status: "proposed",
            reason: null,
            diff: null,
            citations: [
              { attribute: "roast", source_field: "body_html", snippet: "medium roast" },
            ],
            approvable: true,
            block_reason: null,
            publish_error: null,
            published_at: null,
            added_lines: [{ label: "Roast", value: "Medium" }],
            category_from: null,
            category_to: null,
            metafield_value: null,
            metafield_key: null,
          },
        ],
        not_publishable: [],
        needs_input: [],
        ready: [],
        settled: [],
      },
    ],
  };
}

function render(): string {
  return renderToStaticMarkup(createElement(Fixes));
}

beforeEach(() => {
  loaderData = { fixes: fixList(), agentReachable: true };
  actionData = undefined;
});

describe("app.fixes render — a refused decision", () => {
  it("renders no banner at all when the last decision succeeded", () => {
    const html = render();

    expect(html).not.toContain("wasn&#x27;t approved");
    expect(html).toContain("Ethiopia Yirgacheffe");
  });

  it("shows a recoverable warning for a 409, WITHOUT hiding the list", () => {
    actionData = { error: { decision: "approve", status: 409 } };

    const html = render();

    expect(html).toContain("tone=\"warning\"");
    expect(html).toContain("already been decided");
    // The load-bearing assertion: this is an error, not a dead end.
    expect(html).toContain("Ethiopia Yirgacheffe");
    expect(html).toContain("Approve");
  });

  it("shows a critical banner for a transport failure, and still keeps the list", () => {
    actionData = { error: { decision: "approve", status: null } };

    const html = render();

    expect(html).toContain("tone=\"critical\"");
    expect(html).toContain("reach the fix service");
    expect(html).toContain("Ethiopia Yirgacheffe");
  });

  it("says 'rejected' when the refused decision was a reject", () => {
    actionData = { error: { decision: "reject", status: 409 } };

    expect(render()).toContain("rejected");
  });

  it("never renders a raw status code as the page body", () => {
    // The exact symptom: a page whose entire content was the text "200".
    actionData = { error: { decision: "approve", status: 502 } };

    const html = render();

    expect(html).not.toContain(">502<");
    expect(html).toContain("Review fixes");
  });
});
