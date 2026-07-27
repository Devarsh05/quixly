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

export type CompetitorRate = { mention_rate: number; mentions: number };

export type EngineReport = {
  engine: string;
  our_rate: number | null; // null = "no data" (degraded engine) — never render as 0%
  our_mentions: number | null;
  total_queries: number | null;
  coverage: number;
  competitor_rates: Record<string, CompetitorRate>;
};

export type ScanReport = {
  run_id: number;
  status: "running" | "completed" | "failed";
  period: string | null;
  started_at: string | null;
  completed_at: string | null;
  engines: EngineReport[];
};

export type ScanStart = { run_id: number; status: string };

/** Where a proposed value literally came from — rendered as the merchant's trust signal. */
export type Citation = {
  attribute: string | null;
  source_field: string | null;
  snippet: string | null;
};

export type AddedLine = { label: string; value: string };

/**
 * One fix, PRE-RENDERED by the agent. The shell derives nothing from this — `approvable`,
 * `added_lines` and `block_reason` are all decided agent-side so the business rules live in one
 * place (CLAUDE.md: keep the shell thin).
 */
export type FixView = {
  id: number;
  type: "description" | "category" | "metafield" | "merchant_todo";
  target: string;
  status: string;
  reason: string | null;
  diff: string | null;
  citations: Citation[];
  approvable: boolean;
  block_reason: string | null;
  added_lines: AddedLine[];
  category_from: string | null;
  category_to: string | null;
  metafield_value: string | null;
  metafield_key: string | null;
};

export type ProductFixes = {
  product_id: number;
  title: string | null;
  severity: string | null;
  approvable: FixView[];
  not_publishable: FixView[];
  needs_input: FixView[];
};

export type FixList = {
  run_id: number | null;
  status: "running" | "completed" | "failed" | null;
  products: ProductFixes[];
};

export type FixRunStart = { run_id: number; status: string };

export type FixDecision = { fix_id: number; status: string };

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

/**
 * Start a visibility scan for the shop. Async on the agent side: this returns a run_id
 * immediately (202); the report is polled separately until the run is terminal.
 */
export function startScan(shopDomain: string): Promise<ScanStart> {
  return post<ScanStart>(`/shops/by-domain/${encodeURIComponent(shopDomain)}/scan`, {});
}

/**
 * The share-of-model report for a run (the shop's latest run if runId is omitted), or null
 * if the shop has never scanned. Read-only — the audit page polls this via the loader.
 */
export async function getReport(
  shopDomain: string,
  runId?: number,
): Promise<ScanReport | null> {
  const query = runId != null ? `?run_id=${runId}` : "";
  const response = await fetch(
    agentUrl(`/shops/by-domain/${encodeURIComponent(shopDomain)}/report${query}`),
    { headers: internalHeaders() },
  );

  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(`Agent report returned ${response.status}`);
  }
  return (await response.json()) as ScanReport;
}

/**
 * Start an audit + optimize run. Async agent-side (202 + run_id), polled like a scan.
 *
 * NOT a publish: this only proposes fixes into `fixes.status = proposed`. Nothing reaches the
 * merchant's store until it is approved AND the step-4 Publisher runs.
 */
export function startFixRun(shopDomain: string): Promise<FixRunStart> {
  return post<FixRunStart>(
    `/shops/by-domain/${encodeURIComponent(shopDomain)}/fixes/run`,
    {},
  );
}

/**
 * Proposed fixes for a run, grouped by product and already split into approvable /
 * not-publishable / needs-input by the agent.
 *
 * A shop that has never proposed fixes returns `run_id: null` with an empty list — that is a
 * normal empty state, NOT an error, and is distinct from the agent being unreachable.
 */
export async function getFixes(shopDomain: string, runId?: number): Promise<FixList> {
  const query = runId != null ? `?run_id=${runId}` : "";
  const response = await fetch(
    agentUrl(`/shops/by-domain/${encodeURIComponent(shopDomain)}/fixes${query}`),
    { headers: internalHeaders() },
  );

  if (!response.ok) {
    throw new Error(`Agent fixes returned ${response.status}`);
  }
  return (await response.json()) as FixList;
}

/**
 * THE APPROVAL GATE. Flips `fixes.status` proposed -> approved | rejected and nothing else.
 *
 * No Shopify write happens here or anywhere in this step. `shopDomain` always comes from the
 * authenticated session, never from the request body — it is what scopes the fix to its owner.
 * The agent re-checks ownership and refuses a non-approvable type with 409, so a crafted request
 * cannot approve a taxonomy fix the UI declines to offer.
 */
export function decideFix(
  shopDomain: string,
  fixId: number,
  decision: "approve" | "reject",
): Promise<FixDecision> {
  return post<FixDecision>(
    `/shops/by-domain/${encodeURIComponent(shopDomain)}/fixes/${fixId}/${decision}`,
    {},
  );
}
