/**
 * `customers/redact` — one of Shopify's three mandatory compliance webhooks.
 *
 * ACKNOWLEDGE-ONLY, for the same reason `customers/data_request` is: Quixly stores no end-customer
 * personal data, so the work set is empty. The full table-by-table basis for that claim is in
 * `webhooks.customers.data_request.tsx` — it is one audit covering both handlers, kept in one
 * place so the two cannot drift apart.
 *
 * An empty delete is the honest response. Returning 200 asserts "nothing of this customer's is
 * retained", which is true by construction here — not because a lookup came back empty, but
 * because no table on either side is keyed by, or contains, a customer identifier.
 *
 * IF QUIXLY EVER INGESTS CUSTOMER-LINKED DATA — orders, reviews, customer segments — this stops
 * being a no-op and must actually delete, mirroring `webhooks.shop.redact.tsx`: forward to the
 * agent and let it own deletion of its own schema.
 */

import type { ActionFunctionArgs } from "react-router";

import { authenticateWebhookSerialized } from "../lib/webhook-auth.server";

export const action = async ({ request }: ActionFunctionArgs) => {
  // Serialized: this topic arrives while the app is still installed, so the library would
  // otherwise refresh a near-expiry token outside the per-shop rotation lock.
  const { shop, topic } = await authenticateWebhookSerialized(request);

  // Shop and topic only — never the payload, which identifies the customer.
  console.log(`Received ${topic} for ${shop}; no customer data is stored, nothing to erase.`);

  return new Response();
};
