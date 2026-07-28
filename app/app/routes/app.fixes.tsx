/**
 * The approval gate (Phase 3, step 3) — review proposed fixes and approve or reject them.
 *
 * **Nothing here writes to Shopify.** An approval flips `fixes.status` proposed -> approved; the
 * step-4 Publisher is what later acts on approved rows. This page reads and decides, nothing else.
 *
 * Mirrors `app.audit.tsx` exactly: `authenticate.admin` loader -> typed agent client -> Polaris
 * `s-*` components -> `boundary.headers`, polling only while a run is in flight. The shell holds
 * NO business logic: the agent already decided what is approvable, what each fix's readable diff
 * is, and why a blocked row is blocked (CLAUDE.md: keep the app shell thin).
 */

import { useEffect } from "react";
import type {
  ActionFunctionArgs,
  HeadersFunction,
  LoaderFunctionArgs,
} from "react-router";
import { Form, redirect, useLoaderData, useRevalidator } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import type { FixView, ProductFixes } from "../lib/agent.server";
import { decideFix, getFixes, startFixRun } from "../lib/agent.server";
import { authenticate } from "../shopify.server";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);

  const runIdParam = new URL(request.url).searchParams.get("run_id");
  const runId = runIdParam && /^\d+$/.test(runIdParam) ? Number(runIdParam) : undefined;

  // The agent may be briefly unreachable; that must degrade this page (a banner), not break it —
  // and it is DISTINCT from "this shop has no fixes yet" (run_id === null).
  try {
    return { fixes: await getFixes(session.shop, runId), agentReachable: true };
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

  const fixId = Number(form.get("fix_id"));
  const decision = intent === "approve" ? "approve" : "reject";
  await decideFix(session.shop, fixId, decision);
  // Re-read rather than mutating client state: the agent may have superseded sibling rows.
  return redirect(request.url);
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

function ProductCard({ product }: { product: ProductFixes }) {
  const category = product.approvable.filter((f) => f.type === "category");
  const routine = product.approvable.filter((f) => f.type !== "category");

  return (
    <s-section heading={product.title ?? `Product ${product.product_id}`}>
      <s-stack direction="block" gap="large">
        {product.severity && <s-badge tone="info">{product.severity} priority</s-badge>}

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
  const revalidator = useRevalidator();

  const running = fixes?.status === "running";

  // Poll only while a run is in flight. completed / failed are terminal — never spin forever.
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => revalidator.revalidate(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [running, revalidator]);

  const hasProducts = (fixes?.products.length ?? 0) > 0;

  return (
    <s-page heading="Review fixes">
      {/* 1. Agent unreachable — distinct from "no fixes yet". */}
      {!agentReachable && (
        <s-section heading="Fixes">
          <s-banner tone="critical" heading="Couldn't reach the fix service">
            <s-paragraph>
              Your store is connected; this is a temporary problem reaching the Quixly agent. Try
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

      {/* 6. The gate itself. */}
      {hasProducts && (
        <>
          <s-section heading="Approve what goes live">
            <s-paragraph>
              Nothing below has been sent to your store. Each change shows exactly what it edits
              and where the information came from.
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
