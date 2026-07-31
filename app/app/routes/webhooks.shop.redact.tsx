/**
 * `shop/redact` — the one mandatory compliance webhook with real work to do.
 *
 * Arrives 48 hours after a store owner uninstalls, and asks us to erase everything we hold about
 * the shop. That spans BOTH schemas, and each side deletes its own:
 *
 *   - `shopify` schema (Prisma, ours) — the `Session` rows, deleted here.
 *   - `public` schema (Alembic, the agent's) — shops, products, ingest_runs, query_panels,
 *     engine_runs, agent_runs, share_of_model, audits, fixes, verifications. The agent owns that
 *     deletion; the shell never reaches into it. See `agent/app/services/purge.py`.
 *
 * WHY THE AGENT SIDE IS QUEUED, NOT AWAITED. Shopify wants a 2xx within seconds. A synchronous
 * cross-service delete that times out is a failed compliance delivery, which is a submission
 * failure. So the forward only *enqueues* an arq job (`purge_shop`) and returns 204; the actual
 * cascade runs in the worker. The one delete we do inline is a single local `deleteMany`.
 *
 * ORDER IS DELIBERATE: forward first, delete sessions second. A forward that fails returns 500 so
 * Shopify redelivers, and the redelivery still finds the shop's rows intact — same reasoning as
 * `webhooks.app.uninstalled.tsx`. Deleting first would leave the agent holding data with the
 * shell already believing it was handled.
 *
 * NOTE ON THE OVERLAP WITH `app/uninstalled`: that handler only flips `shops.status` and drops the
 * cached token — it deletes NOTHING. The catalog, audits, fixes and verification history all
 * survive an uninstall by design, so this is the first and only path that removes them.
 *
 * This is normally the one webhook that arrives with NO session: `app/uninstalled` deleted it 48
 * hours ago. That is fine — `authenticate.webhook()` still verifies the HMAC (which uses the app
 * secret, not the session) and returns the context with `session: undefined`.
 */

import type { ActionFunctionArgs } from "react-router";

import db from "../db.server";
import { forwardWebhook } from "../lib/agent.server";
import { authenticateWebhookSerialized } from "../lib/webhook-auth.server";

export const action = async ({ request }: ActionFunctionArgs) => {
  // Verifies the HMAC. Anything past this line is provably from Shopify.
  // With no session stored, the wrapper's refresh throws ReauthRequiredError, which it already
  // classifies as permanent and passes through — the library does the rest.
  const { shop, topic } = await authenticateWebhookSerialized(request);

  console.log(`Received ${topic} for ${shop}; erasing all stored data.`);

  // Tell the agent BEFORE deleting anything locally. On failure we return a non-2xx so Shopify
  // redelivers; the agent's purge is idempotent, so a duplicate delivery is harmless.
  try {
    await forwardWebhook(topic, shop, {});
  } catch (error) {
    console.error(`Failed to forward ${topic} for ${shop} to the agent:`, error);
    return new Response("Agent unavailable", { status: 500 });
  }

  // The `shopify` schema's half. Normally already empty — `app/uninstalled` deleted these 48
  // hours ago — but a reinstall-then-uninstall, or a missed uninstall delivery, can leave rows
  // behind, and this webhook is the backstop that guarantees they are gone.
  await db.session.deleteMany({ where: { shop } });

  return new Response();
};
