import { useEffect } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import { Form, redirect, useLoaderData, useRevalidator } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import type { EngineReport } from "../lib/agent.server";
import { getReport, startScan } from "../lib/agent.server";
import { authenticate } from "../shopify.server";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);

  const runIdParam = new URL(request.url).searchParams.get("run_id");
  const runId = runIdParam && /^\d+$/.test(runIdParam) ? Number(runIdParam) : undefined;

  // The agent may be briefly unreachable; that must degrade this page (a banner), not break
  // it — and it is a DISTINCT case from "the shop has never scanned" (report === null).
  try {
    return { report: await getReport(session.shop, runId), agentReachable: true };
  } catch {
    return { report: null, agentReachable: false };
  }
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const { run_id } = await startScan(session.shop);
  // Thread the new run_id into the URL so the loader (and the poll) track this exact scan.
  return redirect(`/app/audit?run_id=${run_id}`);
};

const POLL_INTERVAL_MS = 3000;

/** 0..1 → "42%". Callers handle the null ("no data") case before calling. */
function formatPct(rate: number): string {
  return `${Math.round(rate * 100)}%`;
}

function AuditButton({ label }: { label: string }) {
  return (
    <Form method="post">
      <s-button type="submit" variant="primary">
        {label}
      </s-button>
    </Form>
  );
}

function CompetitorTable({ engine }: { engine: EngineReport }) {
  const competitors = Object.entries(engine.competitor_rates).sort(
    (a, b) => b[1].mention_rate - a[1].mention_rate,
  );
  if (competitors.length === 0) return null;

  return (
    <s-table>
      <s-table-header-row>
        <s-table-header>Competitor</s-table-header>
        <s-table-header>Mention rate</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {competitors.map(([name, rate]) => (
          <s-table-row key={name}>
            <s-table-cell>{name}</s-table-cell>
            <s-table-cell>{formatPct(rate.mention_rate)}</s-table-cell>
          </s-table-row>
        ))}
      </s-table-body>
    </s-table>
  );
}

function EngineResult({ engine }: { engine: EngineReport }) {
  // NULL our_rate = the engine returned no usable data — "No data", never rendered as 0%.
  const headline = engine.our_rate === null ? "No data" : formatPct(engine.our_rate);

  return (
    <s-section heading={`AI visibility — ${engine.engine}`}>
      <s-stack direction="block" gap="base">
        <s-stack direction="inline" gap="base">
          <s-heading>{headline}</s-heading>
          <s-badge tone="info">{engine.engine}</s-badge>
        </s-stack>
        <s-paragraph>
          {engine.our_rate === null
            ? "This engine returned no usable data for your store in this scan."
            : `Your store was recommended in ${engine.our_mentions} of ${engine.total_queries} buyer queries.`}
        </s-paragraph>
        <CompetitorTable engine={engine} />
      </s-stack>
    </s-section>
  );
}

export default function Audit() {
  const { report, agentReachable } = useLoaderData<typeof loader>();
  const revalidator = useRevalidator();

  const scanning = report?.status === "running";

  // Poll only while the scan is running. completed / failed are terminal — stop, never spin
  // forever.
  useEffect(() => {
    if (!scanning) return;
    const timer = setInterval(() => revalidator.revalidate(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [scanning, revalidator]);

  return (
    <s-page heading="AI visibility audit">
      {/* 1. Agent unreachable — distinct from "no runs yet". */}
      {!agentReachable && (
        <s-section heading="Audit">
          <s-stack direction="block" gap="base">
            <s-banner tone="critical" heading="Couldn't reach the audit service">
              <s-paragraph>
                Your store is connected; this is a temporary problem reaching the Quixly
                agent. Try again in a moment.
              </s-paragraph>
            </s-banner>
            <AuditButton label="Retry audit" />
          </s-stack>
        </s-section>
      )}

      {/* 2. Reachable, but the shop has never scanned. */}
      {agentReachable && report === null && (
        <s-section heading="See where AI shopping engines recommend you">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              Run a free audit to find out how often ChatGPT, Perplexity, and other AI
              shopping engines recommend your store for real buyer questions.
            </s-paragraph>
            <AuditButton label="Run your first audit" />
          </s-stack>
        </s-section>
      )}

      {/* 3. Scan failed — the agent recorded the failure (HTTP 200, status "failed"). */}
      {report?.status === "failed" && (
        <s-section heading="Audit">
          <s-stack direction="block" gap="base">
            <s-banner tone="critical" heading="Audit failed">
              <s-paragraph>
                The last scan didn&apos;t finish. This is usually temporary — run it again.
              </s-paragraph>
            </s-banner>
            <AuditButton label="Retry audit" />
          </s-stack>
        </s-section>
      )}

      {/* 4. Scan in progress. */}
      {report?.status === "running" && (
        <s-section heading="Audit">
          <s-stack direction="block" gap="base">
            <s-paragraph>Scanning across AI engines… this takes about 40 seconds.</s-paragraph>
            <s-spinner accessibilityLabel="Scanning" />
          </s-stack>
        </s-section>
      )}

      {/* 5. Completed — the results. our_rate is the headline finding. */}
      {report?.status === "completed" && (
        <>
          {report.engines.length === 0 ? (
            <s-section heading="Audit">
              <s-paragraph>
                This scan produced no engine results. Run the audit again.
              </s-paragraph>
            </s-section>
          ) : (
            report.engines.map((engine) => (
              <EngineResult key={engine.engine} engine={engine} />
            ))
          )}
          <s-section heading="Run again">
            <AuditButton label="Run audit again" />
          </s-section>
        </>
      )}
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
