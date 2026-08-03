import { useId } from "react";
import type { HeadersFunction, LoaderFunctionArgs } from "react-router";
import { useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import type { EngineDelta, VerificationRun } from "../lib/agent.server";
import { getVerificationSeries } from "../lib/agent.server";
import { authenticate } from "../shopify.server";

/**
 * The uplift surface — READ-ONLY presentation over what the Verifier already measured.
 *
 * No action, no POST, no Shopify call. It renders rows; it never produces them.
 *
 * Every display decision is made agent-side (`services/uplift.py`) and arrives as `state` /
 * `deltas_reportable`. Do not re-derive them here: this app's vitest harness runs
 * `environment: "node"` with no DOM, so nothing in this file can be unit-tested. Logic that lives
 * here is logic nothing checks.
 *
 * There is deliberately NO POLLING. A verification writes its rows only at aggregation time, so
 * an in-flight run does not appear in the series at all — there is no in-flight state to watch,
 * unlike the audit and fixes pages which poll because they own a run they just started.
 */
export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);

  // The agent may be briefly unreachable; that must degrade this page (a banner), not break it —
  // and it is a DISTINCT case from "this shop has never been measured" (series.runs === []).
  try {
    return { series: await getVerificationSeries(session.shop), agentReachable: true };
  } catch {
    return { series: null, agentReachable: false };
  }
};

/** 0..1 -> "42%". Callers handle the null ("no data") case before calling. */
export function formatPct(rate: number): string {
  return `${Math.round(rate * 100)}%`;
}

/**
 * A delta is a difference between two rates, so it is measured in percentage POINTS, not percent.
 * Small non-zero movements keep a decimal rather than rounding to a misleading "+0".
 */
export function formatDelta(delta: number): string {
  const points = delta * 100;
  const magnitude =
    points !== 0 && Math.abs(points) < 1 ? points.toFixed(1) : String(Math.round(points));
  return `${points > 0 ? "+" : ""}${magnitude} points`;
}

/** ISO date, deliberately not locale-formatted: this renders on the server AND the client. */
export function settlesOn(run: VerificationRun): string | null {
  if (run.published_at_max === null) return null;
  const at =
    new Date(run.published_at_max).getTime() + run.settle_hours_required * 3600 * 1000;
  return new Date(at).toISOString().slice(0, 10);
}

function queriesSentence(engine: EngineDelta): string | null {
  if (engine.post_mentions === null || engine.post_total_queries === null) return null;
  return `Recommended in ${engine.post_mentions} of ${engine.post_total_queries} buyer queries this period.`;
}

const CHART_WIDTH = 320;
const LABEL_WIDTH = 52;
const VALUE_WIDTH = 48;
const TRACK_WIDTH = CHART_WIDTH - LABEL_WIDTH - VALUE_WIDTH;
const BAR_HEIGHT = 18;
const ROW_HEIGHT = 28;

/**
 * Paired before/after bars. Hand-rolled because this app has no Polaris React (and so no
 * polaris-viz) — the UI is Polaris web components, and one two-bar chart does not justify a
 * second UI stack.
 *
 * Every bar is labelled with its own value: meaning is never carried by colour alone.
 */
function UpliftBars({ engine }: { engine: EngineDelta }) {
  const titleId = useId();

  // Only ever called with both sides present; a no-data side has no bar to draw.
  if (engine.pre_rate === null || engine.post_rate === null) return null;

  const rows = [
    { label: "Before", rate: engine.pre_rate, fill: "#8c9196" },
    { label: "After", rate: engine.post_rate, fill: "#2c6ecb" },
  ];

  return (
    <svg
      viewBox={`0 0 ${CHART_WIDTH} ${ROW_HEIGHT * rows.length}`}
      width="100%"
      style={{ maxWidth: `${CHART_WIDTH}px` }}
      role="img"
      aria-labelledby={titleId}
    >
      <title id={titleId}>
        {`${engine.engine}: recommendation rate ${formatPct(engine.pre_rate)} before, ${formatPct(engine.post_rate)} after`}
      </title>
      {rows.map((row, index) => (
        <g key={row.label} transform={`translate(0, ${index * ROW_HEIGHT})`}>
          <text x={0} y={BAR_HEIGHT - 4} fontSize="12" fill="currentColor">
            {row.label}
          </text>
          <rect
            x={LABEL_WIDTH}
            y={0}
            width={TRACK_WIDTH}
            height={BAR_HEIGHT}
            rx={3}
            fill="currentColor"
            opacity={0.08}
          />
          <rect
            x={LABEL_WIDTH}
            y={0}
            width={Math.max(row.rate * TRACK_WIDTH, row.rate > 0 ? 2 : 0)}
            height={BAR_HEIGHT}
            rx={3}
            fill={row.fill}
          />
          <text
            x={LABEL_WIDTH + TRACK_WIDTH + 6}
            y={BAR_HEIGHT - 4}
            fontSize="12"
            fill="currentColor"
          >
            {formatPct(row.rate)}
          </text>
        </g>
      ))}
    </svg>
  );
}

/** The three no-data states, each explained as an ENGINE problem, never as a loss of visibility. */
function NoDataEngine({ engine }: { engine: EngineDelta }) {
  // Derived from the rates rather than `state`, so the wording can never contradict the data.
  const explanation =
    engine.pre_rate === null && engine.post_rate === null
      ? "This engine returned no usable answers in either scan, so there is nothing to compare."
      : engine.post_rate === null
        ? "This engine returned no usable answers this period, so there is nothing to compare. That is a gap in the engine's response, not a change in your visibility."
        : "There is no usable baseline reading for this engine, so there is nothing to compare against.";

  return (
    <s-section heading={`AI visibility — ${engine.engine}`}>
      <s-stack direction="block" gap="base">
        <s-stack direction="inline" gap="base">
          <s-heading>No data this period</s-heading>
          <s-badge tone="info">{engine.engine}</s-badge>
        </s-stack>
        <s-paragraph>{explanation}</s-paragraph>
      </s-stack>
    </s-section>
  );
}

/**
 * One engine's result.
 *
 * `reportable` is the server's `deltas_reportable` and gates the delta figure and the chart.
 * When it is false the numbers are still shown — they are real readings — but framed as a reading
 * in progress, never as a finding.
 */
function EngineResult({
  engine,
  reportable,
}: {
  engine: EngineDelta;
  reportable: boolean;
}) {
  // Guard on the VALUES, not only on `state`. The agent's classification already implies the
  // rates are present, but keying the null check off the raw fields means a future state we do
  // not recognise degrades to "no data" rather than rendering NaN at a merchant.
  if (engine.pre_rate === null || engine.post_rate === null) {
    return <NoDataEngine engine={engine} />;
  }

  const before = formatPct(engine.pre_rate);
  const after = formatPct(engine.post_rate);

  if (!reportable) {
    return (
      <s-section heading={`AI visibility — ${engine.engine}`}>
        <s-stack direction="block" gap="base">
          <s-stack direction="inline" gap="base">
            <s-heading>Reading so far</s-heading>
            <s-badge tone="caution">{engine.engine}</s-badge>
          </s-stack>
          <s-paragraph>
            Recommendation rate — before {before}, after {after}.
          </s-paragraph>
          <s-text color="subdued">
            This is an early reading, not a result. It is shown for transparency only.
          </s-text>
        </s-stack>
      </s-section>
    );
  }

  const tone =
    engine.state === "improved"
      ? "success"
      : engine.state === "declined"
        ? "critical"
        : "info";
  const headline =
    engine.state === "no_movement" || engine.delta === null
      ? "No change"
      : formatDelta(engine.delta);
  const queries = queriesSentence(engine);

  return (
    <s-section heading={`AI visibility — ${engine.engine}`}>
      <s-stack direction="block" gap="base">
        <s-stack direction="inline" gap="base">
          <s-heading>{headline}</s-heading>
          <s-badge tone={tone}>{engine.engine}</s-badge>
        </s-stack>
        <s-paragraph>
          Recommendation rate — before {before}, after {after}.
        </s-paragraph>
        <UpliftBars engine={engine} />
        {queries && <s-text color="subdued">{queries}</s-text>}
      </s-stack>
    </s-section>
  );
}

/**
 * What was live during the measured window.
 *
 * Provenance, NOT attribution — the measurement grain is (run, engine), so no number here belongs
 * to any individual fix. The copy stays observational for exactly that reason.
 */
function MeasuredFixes({ run }: { run: VerificationRun }) {
  if (run.measured_fixes.length === 0) return null;

  return (
    <s-section heading="What was live during this measurement">
      <s-stack direction="block" gap="small-300">
        <s-paragraph>
          Measured after {run.measured_fixes.length}{" "}
          {run.measured_fixes.length === 1 ? "fix went" : "fixes went"} live.
        </s-paragraph>
        <s-unordered-list>
          {run.measured_fixes.map((fix) => (
            <s-list-item key={fix.fix_id}>
              {fix.type} on product {fix.product_id} — published{" "}
              {fix.published_at.slice(0, 10)}
            </s-list-item>
          ))}
        </s-unordered-list>
        <s-text color="subdued">
          These changes were live while the follow-up scan ran. The rates above describe the store
          as a whole over a fixed set of buyer questions, not any single product.
        </s-text>
      </s-stack>
    </s-section>
  );
}

export default function Uplift() {
  const { series, agentReachable } = useLoaderData<typeof loader>();

  const runs = series?.runs ?? [];
  const latest: VerificationRun | undefined = runs[runs.length - 1];
  const settleDate = latest ? settlesOn(latest) : null;

  return (
    <s-page heading="Uplift">
      {/* 1. Agent unreachable — distinct from "never measured". */}
      {!agentReachable && (
        <s-section heading="Uplift">
          <s-banner tone="critical" heading="Couldn't reach the measurement service">
            <s-paragraph>
              Your store is connected; this is a temporary problem reaching the SixRise agent. Try
              again in a moment.
            </s-paragraph>
          </s-banner>
        </s-section>
      )}

      {/* 2. Reachable, but the agent has no record of this shop. */}
      {agentReachable && series === null && (
        <s-section heading="Uplift">
          <s-paragraph>
            This store isn&apos;t set up for measurement yet. Run an audit first.
          </s-paragraph>
        </s-section>
      )}

      {/* 3. Known shop, nothing measured yet — a normal state, not an error. */}
      {series !== null && runs.length === 0 && (
        <s-section heading="No uplift measurement yet">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              Uplift compares how often AI shopping engines recommend your store before and after
              your approved fixes go live.
            </s-paragraph>
            <s-paragraph>
              Approve and publish fixes first. Engines need time to re-crawl your store, so the
              first measurement is taken about a week after a publish.
            </s-paragraph>
            <s-link href="/app/fixes">Review fixes</s-link>
          </s-stack>
        </s-section>
      )}

      {/* 4. In flight (only reachable via an explicit ?run_id — the series omits running runs). */}
      {latest?.state === "running" && (
        <s-section heading="Uplift">
          <s-stack direction="block" gap="base">
            <s-paragraph>Measuring across AI engines…</s-paragraph>
            <s-spinner accessibilityLabel="Measuring" />
          </s-stack>
        </s-section>
      )}

      {/* 5. The measurement job aborted. */}
      {latest?.state === "failed" && (
        <s-section heading="Uplift">
          <s-banner tone="critical" heading="Measurement didn't finish">
            <s-paragraph>
              The last measurement failed before it produced a result. Nothing on your store was
              changed.
            </s-paragraph>
          </s-banner>
        </s-section>
      )}

      {/* 6. Terminal but empty. */}
      {latest?.state === "empty" && (
        <s-section heading="Uplift">
          <s-paragraph>The last measurement produced no engine results.</s-paragraph>
        </s-section>
      )}

      {/*
        7. THE INVARIANT. The run completed and holds real numbers, but engines had not
        re-crawled yet — so it measured nothing. It is never shown as a result, whatever its
        delta says. Keyed on `state`, never on the delta's value.
      */}
      {latest?.state === "unsettled" && (
        <>
          <s-section heading="Uplift">
            <s-banner tone="warning" heading="Measurement pending">
              <s-stack direction="block" gap="small-300">
                <s-paragraph>
                  Your fixes went live too recently to measure. AI engines re-crawl on their own
                  schedule, so a before/after comparison isn&apos;t meaningful yet.
                </s-paragraph>
                {latest.settle_hours !== null && (
                  <s-paragraph>
                    {Math.round(latest.settle_hours)} of the{" "}
                    {Math.round(latest.settle_hours_required)} hours needed have passed
                    {settleDate ? `; a settled result is available from ${settleDate}.` : "."}
                  </s-paragraph>
                )}
              </s-stack>
            </s-banner>
          </s-section>
          {latest.engines.map((engine) => (
            <EngineResult key={engine.engine} engine={engine} reportable={false} />
          ))}
          <MeasuredFixes run={latest} />
        </>
      )}

      {/* 8. Settled — the only state whose deltas are presented as a finding. */}
      {latest?.state === "settled" && (
        <>
          {latest.engines.length === 0 ? (
            <s-section heading="Uplift">
              <s-paragraph>This measurement produced no engine results.</s-paragraph>
            </s-section>
          ) : (
            latest.engines.map((engine) => (
              <EngineResult key={engine.engine} engine={engine} reportable />
            ))
          )}
          <MeasuredFixes run={latest} />
        </>
      )}
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
