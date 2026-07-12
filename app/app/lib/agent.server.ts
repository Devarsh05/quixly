/**
 * Typed client for the internal agent API.
 *
 * The app shell is a thin Shopify-facing layer: it tells the agent *what happened*
 * (a shop installed, a product changed) and never makes product decisions itself.
 * All business logic lives in agent/.
 */

type ConnectResponse = {
  shop_id: number;
  run_id: number;
  status: string;
  already_running: boolean;
};

export type IngestRun = {
  run_id: number;
  status: "queued" | "running" | "complete" | "failed";
  products_seen: number;
  products_written: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
};

function agentUrl(path: string): string {
  const base = process.env.AGENT_SERVICE_URL;
  if (!base) throw new Error("AGENT_SERVICE_URL is not set");
  return `${base}${path}`;
}

function internalHeaders(): HeadersInit {
  const key = process.env.INTERNAL_API_KEY;
  if (!key) throw new Error("INTERNAL_API_KEY is not set");
  return { "Content-Type": "application/json", "X-Internal-Api-Key": key };
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(agentUrl(path), {
    method: "POST",
    headers: internalHeaders(),
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(`Agent ${path} returned ${response.status}`);
  }

  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

/** Register a shop with the agent and kick off catalog ingestion. Idempotent. */
export function connectShop(shopDomain: string): Promise<ConnectResponse> {
  return post<ConnectResponse>("/shops/connect", { shop_domain: shopDomain });
}

/** Forward a Shopify webhook the app shell has already HMAC-verified. */
export function forwardWebhook(
  topic: string,
  shopDomain: string,
  payload: unknown,
): Promise<void> {
  return post<void>("/webhooks/shopify", {
    topic,
    shop_domain: shopDomain,
    payload: payload ?? {},
  });
}

/**
 * The shop's most recent ingest run, or null if it has never ingested.
 *
 * Read-only by design — the post-install page polls this. Calling connectShop() on every
 * render would re-enqueue an ingest each time.
 */
export async function getLatestIngestRun(shopDomain: string): Promise<IngestRun | null> {
  const response = await fetch(
    agentUrl(`/shops/by-domain/${encodeURIComponent(shopDomain)}/ingest/latest`),
    { headers: internalHeaders() },
  );

  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(`Agent ingest status returned ${response.status}`);
  }
  return (await response.json()) as IngestRun;
}
