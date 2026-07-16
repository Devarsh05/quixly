import type { ActionFunctionArgs } from "react-router";
import db from "../db.server";
import { authenticateWebhookSerialized } from "../lib/webhook-auth.server";

export const action = async ({ request }: ActionFunctionArgs) => {
    // Serialized: webhook auth can itself rotate the offline token — see webhook-auth.server.
    const { payload, session, topic, shop } = await authenticateWebhookSerialized(request);
    console.log(`Received ${topic} webhook for ${shop}`);

    const current = payload.current as string[];
    if (session) {
        await db.session.update({   
            where: {
                id: session.id
            },
            data: {
                scope: current.toString(),
            },
        });
    }
    return new Response();
};
