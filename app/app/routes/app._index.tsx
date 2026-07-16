import { useEffect } from "react";
import type { HeadersFunction, LoaderFunctionArgs } from "react-router";
import { useLoaderData, useRevalidator } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { getLatestIngestRun } from "../lib/agent.server";
import { authenticate } from "../shopify.server";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);

  // The agent may be starting up or briefly unreachable; that should degrade this page,
  // not break it.
  try {
    return { run: await getLatestIngestRun(session.shop), agentReachable: true };
  } catch {
    return { run: null, agentReachable: false };
  }
};

const POLL_INTERVAL_MS = 2000;

export default function Index() {
  const { run, agentReachable } = useLoaderData<typeof loader>();
  const revalidator = useRevalidator();

  const inProgress = run?.status === "queued" || run?.status === "running";

  // Poll only while ingest is actually moving. A finished (or failed) run is terminal,
  // so there is nothing left to watch.
  useEffect(() => {
    if (!inProgress) return;
    const timer = setInterval(() => revalidator.revalidate(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [inProgress, revalidator]);

  return (
    <s-page heading="Quixly">
      <s-section heading="Catalog">
        {!agentReachable && (
          <s-paragraph>
            Can&apos;t reach the Quixly agent service right now. Your store is connected;
            this page will recover once the agent is back.
          </s-paragraph>
        )}

        {agentReachable && !run && (
          <s-paragraph>
            No catalog import has run yet. It starts automatically right after install.
          </s-paragraph>
        )}

        {run && inProgress && (
          <s-stack direction="block" gap="base">
            <s-paragraph>
              Importing your catalog… {run.products_written} product
              {run.products_written === 1 ? "" : "s"} so far.
            </s-paragraph>
            <s-spinner accessibilityLabel="Importing catalog" />
          </s-stack>
        )}

        {run?.status === "complete" && (
          <s-paragraph>
            Catalog imported: {run.products_written} product
            {run.products_written === 1 ? "" : "s"}. Ready to audit.
          </s-paragraph>
        )}

        {run?.status === "failed" && (
          <s-stack direction="block" gap="base">
            <s-banner tone="critical" heading="Catalog import failed">
              <s-paragraph>
                Imported {run.products_written} product
                {run.products_written === 1 ? "" : "s"} before stopping. The import resumes
                from where it left off on the next attempt.
              </s-paragraph>
            </s-banner>
          </s-stack>
        )}
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
