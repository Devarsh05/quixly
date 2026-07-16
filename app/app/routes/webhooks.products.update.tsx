import type { ActionFunctionArgs } from "react-router";

import { forwardWebhook } from "../lib/agent.server";
import { authenticateWebhookSerialized } from "../lib/webhook-auth.server";

export const action = async ({ request }: ActionFunctionArgs) => {
  // Verifies the HMAC. Anything past this line is provably from Shopify.
  // Serialized: webhook auth can itself rotate the offline token — see webhook-auth.server.
  const { shop, topic, payload } = await authenticateWebhookSerialized(request);

  // Shopify expects a fast 200, so this hands off to the agent and does nothing else —
  // all catalog logic lives in agent/.
  try {
    await forwardWebhook(topic, shop, payload);
  } catch (error) {
    // Fail loudly rather than swallowing: a non-2xx makes Shopify redeliver, so a brief
    // agent outage delays the update instead of losing it. The agent's handler is
    // idempotent, so a duplicate delivery is harmless.
    console.error(`Failed to forward ${topic} for ${shop} to the agent:`, error);
    return new Response("Agent unavailable", { status: 500 });
  }

  return new Response();
};
