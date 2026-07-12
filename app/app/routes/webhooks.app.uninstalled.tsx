import type { ActionFunctionArgs } from "react-router";
import { authenticate } from "../shopify.server";
import db from "../db.server";
import { forwardWebhook } from "../lib/agent.server";

export const action = async ({ request }: ActionFunctionArgs) => {
  const { shop, session, topic } = await authenticate.webhook(request);

  console.log(`Received ${topic} webhook for ${shop}`);

  // Tell the agent BEFORE deleting the session. On failure we return a non-2xx so
  // Shopify redelivers, and the session still being present means the retry can still
  // authenticate. Deleting first would strand the agent believing the shop is live,
  // with no way to recover.
  try {
    await forwardWebhook(topic, shop, {});
  } catch (error) {
    console.error(`Failed to forward ${topic} for ${shop} to the agent:`, error);
    return new Response("Agent unavailable", { status: 500 });
  }

  // Webhook requests can trigger multiple times and after an app has already been uninstalled.
  // If this webhook already ran, the session may have been deleted previously.
  if (session) {
    await db.session.deleteMany({ where: { shop } });
  }

  return new Response();
};
