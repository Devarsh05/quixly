/**
 * The approval gate (step 3) and the publish trigger (step 4) — review fixes, approve them, and
 * send the approved ones to the store.
 *
 * **Approving still writes nothing to Shopify.** It flips `fixes.status` proposed -> approved.
 * Publishing is a SECOND, explicit merchant action, and it publishes exactly the approved rows —
 * the request carries no fix ids, so it can never widen past what was individually approved.
 *
 * Mirrors `app.audit.tsx`: `authenticate.admin` loader -> typed agent client -> Polaris `s-*`
 * components -> `boundary.headers`, polling only while a run is in flight. The shell holds NO
 * business logic: the agent decides what is approvable, what is ready to publish, what each fix's
 * readable diff is, and why anything failed (CLAUDE.md: keep the app shell thin).
 */

import { useEffect } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import {
  Form,
  redirect,
  useActionData,
  useLoaderData,
  useRevalidator,
} from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import type { FixView, ProductFixes } from "../lib/agent.server";
import {
  AgentError,
  decideFix,
  getFixes,
  publishFixes,
  startFixRun,
} from "../lib/agent.server";
import { authenticate } from "../shopify.server";

function numericParam(url: URL, name: string): number | undefined {
  const raw = url.searchParams.get(name);
  return raw && /^\d+$/.test(raw) ? Number(raw) : undefined;
}

/**
 * This page's own URL, rebuilt from scratch — carrying ONLY the two params the loader reads.
 *
 * **Never redirect to `request.url`.** In an embedded app that absolute URL also carries the
 * one-time Shopify auth params (`id_token`, `hmac`, `session`, `timestamp`, `host`, `embedded`);
 * redirecting back onto them re-enters `authenticate.admin` with a spent token, which throws an
 * `ErrorResponse`. `boundary.error` renders an `ErrorResponse` as `error.data` in a bare
 * `dangerouslySetInnerHTML` div — no app chrome, no nav — so the merchant dead-ends on a page
 * showing the raw body. That is what this rebuild exists to prevent, and it is why the two
 * branches below that already build an explicit relative path never had the bug.
 */
function fixesPath(request: Request): string {
  const source = new URL(request.url).searchParams;
  const params = new URLSearchParams();
  for (const name of ["run_id", "publish_run_id"]) {
    const value = source.get(name);
    if (value && /^\d+$/.test(value)) params.set(name, value);
  }
  return params.size > 0 ? `/app/fixes?${params}` : "/app/fixes";
}

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);

  const url = new URL(request.url);
  const runId = numericParam(url, "run_id");
  // Carried in the URL so the page can poll a publish in flight — `agent_runs` has no run-kind
  // discriminator, so the id the 202 handed us is what identifies it.
  const publishRunId = numericParam(url, "publish_run_id");

  // The agent may be briefly unreachable; that must degrade this page (a banner), not break it —
  // and it is DISTINCT from "this shop has no fixes yet" (run_id === null).
  try {
    return {
      fixes: await getFixes(session.shop, runId, publishRunId),
      agentReachable: true,
    };
  } catch {
    return { fixes: null, agentReachable: false };
  }
};

export const action = async ({ request }: ActionFunctionArgs) => {
  // session.shop is the ONLY source of the shop identity — never a form field. It is what scopes
  // every decision to its owner; the agent re-checks ownership and returns 404 on a mismatch.
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const intent = String(form.get("intent") ?? "");

  if (intent === "run") {
    const { run_id } = await startFixRun(session.shop);
    return redirect(`/app/fixes?run_id=${run_id}`);
  }

  if (intent === "publish") {
    const { run_id } = await publishFixes(session.shop);
    const existing = new URL(request.url).searchParams.get("run_id");
    const scope = existing ? `run_id=${existing}&` : "";
    return redirect(`/app/fixes?${scope}publish_run_id=${run_id}`);
  }

  const fixId = Number(form.get("fix_id"));
  const decision = intent === "approve" ? "approve" : "reject";

  // A refusal must stay ON this page. Letting it throw would surface as an `ErrorResponse` in
  // the app-level boundary, which renders the raw body with no chrome — a dead end for what is
  // often a recoverable state (409 = someone already decided this fix).
  try {
    await decideFix(session.shop, fixId, decision);
  } catch (error) {
    return {
      error: {
        decision,
        status: error instanceof AgentError ? error.status : null,
      },
    };
  }

  // Re-read rather than mutating client state: the agent may have superseded sibling rows.
  return redirect(fixesPath(request));
};

const POLL_INTERVAL_MS = 3000;

function FixRunButton({ label, variant }: { label: string; variant?: "primary" }) {
  return (
    <Form method="post">
      <input type="hidden" name="intent" value="run" />
      <s-button type="submit" variant={variant}>
        {label}
      </s-button>
    </Form>
  );
}

function DecisionButtons({ fixId }: { fixId: number }) {
  return (
    <s-stack direction="inline" gap="small-300">
      <Form method="post">
        <input type="hidden" name="intent" value="approve" />
        <input type="hidden" name="fix_id" value={fixId} />
        <s-button type="submit" variant="primary">
          Approve
        </s-button>
      </Form>
      <Form method="post">
        <input type="hidden" name="intent" value="reject" />
        <input type="hidden" name="fix_id" value={fixId} />
        <s-button type="submit">Reject</s-button>
      </Form>
    </s-stack>
  );
}

/**
 * Where the value came from. This is the trust mechanism that makes a merchant comfortable
 * approving — not optional chrome. A fix that arrives WITHOUT a citation is a bug, so it is
 * flagged loudly rather than rendered as though it were grounded.
 */
function Citations({ fix }: { fix: FixView }) {
  if (fix.citations.length === 0) {
    return <s-badge tone="critical">No source citation — do not approve</s-badge>;
  }

  return (
    <s-stack direction="block" gap="small-500">
      {fix.citations.map((citation, index) => (
        <s-text key={index} color="subdued">
          Grounded from {citation.source_field ?? "source"}: “{citation.snippet ?? ""}”
        </s-text>
      ))}
    </s-stack>
  );
}

/** A description fix: show what changes in the merchant's live copy, never raw HTML. */
function DescriptionFix({ fix }: { fix: FixView }) {
  return (
    <s-stack direction="block" gap="base">
      <s-text>These lines are added to the end of your product description:</s-text>
      <s-unordered-list>
        {fix.added_lines.map((line, index) => (
          <s-list-item key={index}>
            {line.label ? `${line.label}: ${line.value}` : line.value}
          </s-list-item>
        ))}
      </s-unordered-list>
      <s-text color="subdued">Your existing description is not changed.</s-text>
      <Citations fix={fix} />
      <DecisionButtons fixId={fix.id} />
    </s-stack>
  );
}

/**
 * A category fix is PUBLISH-CLASS: assigning a Standard-taxonomy category has real tax and
 * sales-channel consequences. It gets its own warning-toned section above the routine fixes and
 * its own approve control, so it can never be rubber-stamped alongside a copy tweak.
 */
function CategoryFix({ fix }: { fix: FixView }) {
  return (
    <s-banner tone="warning" heading="Set product category — please read">
      <s-stack direction="block" gap="base">
        <s-paragraph>
          Assigning a category changes how this product is taxed and which sales channels can
          list it. Approve it only if the category below is right.
        </s-paragraph>
        <s-stack direction="block" gap="small-500">
          <s-text color="subdued">From: {fix.category_from}</s-text>
          <s-text>To: {fix.category_to}</s-text>
        </s-stack>
        <Citations fix={fix} />
        <DecisionButtons fixId={fix.id} />
      </s-stack>
    </s-banner>
  );
}

/**
 * Identified but not publishable — the taxonomy metafield path is blocked on a Shopify permission
 * the app has not been granted. Deliberately renders NO approve control at all (not a disabled
 * one): there must be no approvable path to a write that cannot execute.
 */
function NotPublishableFix({ fix }: { fix: FixView }) {
  return (
    <s-stack direction="block" gap="small-500">
      <s-stack direction="inline" gap="small-300">
        <s-badge>Identified, not yet publishable</s-badge>
        <s-text>
          {fix.metafield_key}: {fix.metafield_value}
        </s-text>
      </s-stack>
      {fix.block_reason && <s-text color="subdued">{fix.block_reason}</s-text>}
      <Citations fix={fix} />
    </s-stack>
  );
}

/** One line describing what a fix does, for the lists where the full diff is already reviewed. */
function fixSummary(fix: FixView): string {
  if (fix.type === "category") return `Set category to ${fix.category_to}`;
  if (fix.type === "description") {
    const count = fix.added_lines.length;
    return `Add ${count} detail line${count === 1 ? "" : "s"} to the description`;
  }
  return fix.target;
}

/** Approved and waiting. This list is exactly what Publish would write — never more. */
function ReadyFix({ fix }: { fix: FixView }) {
  return (
    <s-stack direction="inline" gap="small-300">
      <s-badge tone="caution">Ready</s-badge>
      <s-text>{fixSummary(fix)}</s-text>
    </s-stack>
  );
}

/**
 * What a publish run actually did. A failure is never a dead end: the agent's `publish_error` says
 * why — the write was refused, or the product changed after approval — and is shown verbatim.
 *
 * `verified` is the ONLY success state: it means we re-read the product from Shopify afterwards
 * and confirmed the change is live. A `published` row is mid-flight, not a success.
 */
function SettledFix({ fix }: { fix: FixView }) {
  const tone =
    fix.status === "verified" ? "success" : fix.status === "published" ? "info" : "critical";
  const label =
    fix.status === "verified"
      ? "Live"
      : fix.status === "published"
        ? "Confirming…"
        : fix.status === "stale"
          ? "Skipped"
          : "Didn't publish";

  return (
    <s-stack direction="block" gap="small-500">
      <s-stack direction="inline" gap="small-300">
        <s-badge tone={tone}>{label}</s-badge>
        <s-text>{fixSummary(fix)}</s-text>
      </s-stack>
      {fix.publish_error && <s-text color="subdued">{fix.publish_error}</s-text>}
    </s-stack>
  );
}

/**
 * A decision the agent refused. Rendered as a banner ON the page — the list below it is still
 * live and still current (React Router revalidates the loader after an action), so this is a
 * recoverable state, not a dead end.
 *
 * The agent's own error message embeds the internal API path, so it is never shown; only the
 * status is translated. 409 is the interesting one: the fix was already decided, or a sibling
 * row superseded it, and the refreshed list below already reflects that.
 */
function DecisionError({ decision, status }: { decision: string; status: number | null }) {
  const verb = decision === "approve" ? "approved" : "rejected";

  return status === 409 ? (
    <s-banner tone="warning" heading={`That change wasn't ${verb}`}>
      <s-paragraph>
        It had already been decided — either by you in another tab, or because approving a
        different change for the same product replaced it. The list below is up to date.
      </s-paragraph>
    </s-banner>
  ) : (
    <s-banner tone="critical" heading={`That change wasn't ${verb}`}>
      <s-paragraph>
        We couldn&apos;t reach the fix service. Nothing was changed and nothing was sent to your
        store — try again in a moment.
      </s-paragraph>
    </s-banner>
  );
}

function ProductCard({ product }: { product: ProductFixes }) {
  const category = product.approvable.filter((f) => f.type === "category");
  const routine = product.approvable.filter((f) => f.type !== "category");

  return (
    <s-section heading={product.title ?? `Product ${product.product_id}`}>
      <s-stack direction="block" gap="large">
        {product.severity && <s-badge tone="info">{product.severity} priority</s-badge>}

        {product.settled.length > 0 && (
          <s-stack direction="block" gap="base">
            <s-heading>Publish results</s-heading>
            {product.settled.map((fix) => (
              <SettledFix key={fix.id} fix={fix} />
            ))}
          </s-stack>
        )}

        {product.ready.length > 0 && (
          <s-stack direction="block" gap="base">
            <s-heading>Approved, ready to publish</s-heading>
            {product.ready.map((fix) => (
              <ReadyFix key={fix.id} fix={fix} />
            ))}
          </s-stack>
        )}

        {/* Publish-class first, visually separated. */}
        {category.map((fix) => (
          <CategoryFix key={fix.id} fix={fix} />
        ))}

        {routine.map((fix) => (
          <DescriptionFix key={fix.id} fix={fix} />
        ))}

        {product.not_publishable.length > 0 && (
          <s-stack direction="block" gap="base">
            <s-heading>Identified, not yet publishable</s-heading>
            {product.not_publishable.map((fix) => (
              <NotPublishableFix key={fix.id} fix={fix} />
            ))}
          </s-stack>
        )}

        {product.needs_input.length > 0 && (
          <s-stack direction="block" gap="base">
            <s-heading>Needs your input</s-heading>
            <s-unordered-list>
              {product.needs_input.map((fix) => (
                <s-list-item key={fix.id}>{fix.reason}</s-list-item>
              ))}
            </s-unordered-list>
          </s-stack>
        )}
      </s-stack>
    </s-section>
  );
}

export default function Fixes() {
  const { fixes, agentReachable } = useLoaderData<typeof loader>();
  const actionData = useActionData<typeof action>();
  const revalidator = useRevalidator();

  const running = fixes?.status === "running";
  const publishing = fixes?.publish_status === "running";
  const inFlight = running || publishing;

  // Poll only while a run is in flight. completed / failed are terminal — never spin forever.
  useEffect(() => {
    if (!inFlight) return;
    const timer = setInterval(() => revalidator.revalidate(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [inFlight, revalidator]);

  const hasProducts = (fixes?.products.length ?? 0) > 0;
  const readyCount =
    fixes?.products.reduce((total, product) => total + product.ready.length, 0) ?? 0;

  return (
    <s-page heading="Review fixes">
      {/* 0. A refused approve/reject. Shown above everything, with the live list still below. */}
      {actionData?.error && (
        <s-section heading="Your last decision">
          <DecisionError
            decision={actionData.error.decision}
            status={actionData.error.status}
          />
        </s-section>
      )}

      {/* 1. Agent unreachable — distinct from "no fixes yet". */}
      {!agentReachable && (
        <s-section heading="Fixes">
          <s-banner tone="critical" heading="Couldn't reach the fix service">
            <s-paragraph>
              Your store is connected; this is a temporary problem reaching the SixRise agent. Try
              again in a moment.
            </s-paragraph>
          </s-banner>
        </s-section>
      )}

      {/* 2. Reachable, but this shop has never proposed fixes. */}
      {agentReachable && fixes?.run_id === null && (
        <s-section heading="Find what's holding your products back">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              We&apos;ll read your catalog and propose grounded improvements — every one backed by
              a quote from your own product data. Nothing is changed until you approve it.
            </s-paragraph>
            <FixRunButton label="Find fixes" variant="primary" />
          </s-stack>
        </s-section>
      )}

      {/* 3. A run is in flight. */}
      {running && (
        <s-section heading="Finding fixes">
          <s-stack direction="block" gap="base">
            <s-paragraph>Reading your catalog and grounding proposals…</s-paragraph>
            <s-spinner accessibilityLabel="Finding fixes" />
          </s-stack>
        </s-section>
      )}

      {/* 4. Run failed. */}
      {fixes?.status === "failed" && (
        <s-section heading="Fixes">
          <s-stack direction="block" gap="base">
            <s-banner tone="critical" heading="That run didn't finish">
              <s-paragraph>This is usually temporary — run it again.</s-paragraph>
            </s-banner>
            <FixRunButton label="Try again" />
          </s-stack>
        </s-section>
      )}

      {/* 5. Completed with nothing left to review — every fix was already decided. */}
      {fixes?.status === "completed" && !hasProducts && (
        <s-section heading="Nothing to review">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              You&apos;ve reviewed everything from the last run. Run it again after you&apos;ve
              edited your products.
            </s-paragraph>
            <FixRunButton label="Find fixes again" />
          </s-stack>
        </s-section>
      )}

      {/* 6. A publish run is in flight. */}
      {publishing && (
        <s-section heading="Publishing to your store">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              Sending your approved changes, then re-reading each product to confirm they landed…
            </s-paragraph>
            <s-spinner accessibilityLabel="Publishing" />
          </s-stack>
        </s-section>
      )}

      {/* 7. A publish run failed before it wrote anything. */}
      {fixes?.publish_status === "failed" && (
        <s-section heading="Publishing">
          <s-banner tone="critical" heading="That publish run didn't finish">
            <s-paragraph>
              Nothing partial was left behind — every change is either live and confirmed, or
              untouched and still approved. See each product below for detail.
            </s-paragraph>
          </s-banner>
        </s-section>
      )}

      {/* 8. THE PUBLISH TRIGGER — the only control on this page that writes to the store. */}
      {readyCount > 0 && !publishing && (
        <s-section heading="Publish approved changes">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              {readyCount} approved change{readyCount === 1 ? "" : "s"} will be written to your
              store. Each one is re-checked against the live product first and confirmed by
              re-reading it afterwards — anything that changed since you approved it is skipped,
              not forced through.
            </s-paragraph>
            <Form method="post">
              <input type="hidden" name="intent" value="publish" />
              <s-button type="submit" variant="primary">
                Publish {readyCount} change{readyCount === 1 ? "" : "s"}
              </s-button>
            </Form>
          </s-stack>
        </s-section>
      )}

      {/* 9. The gate itself. */}
      {hasProducts && (
        <>
          <s-section heading="Approve what goes live">
            <s-paragraph>
              Approving does not send anything to your store — publishing is a separate step. Each
              change shows exactly what it edits and where the information came from.
            </s-paragraph>
          </s-section>
          {fixes?.products.map((product) => (
            <ProductCard key={product.product_id} product={product} />
          ))}
          <s-section heading="Run again">
            <FixRunButton label="Find fixes again" />
          </s-section>
        </>
      )}
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
