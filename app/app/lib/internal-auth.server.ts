/**
 * Shared-secret guard for internal (agent -> app shell) routes.
 *
 * These routes are not Shopify-authenticated: the caller is our own agent service, not
 * a merchant. They must never be linked from the UI or exposed publicly.
 */

import { timingSafeEqual } from "node:crypto";

export const INTERNAL_API_KEY_HEADER = "x-internal-api-key";

/**
 * Throws a 401 Response unless the request carries the shared secret.
 *
 * Compared in constant time so the key cannot be recovered by timing the response.
 * A missing key is rejected exactly like a wrong one.
 */
export function requireInternalApiKey(request: Request): void {
  const expected = process.env.INTERNAL_API_KEY;

  // Fail closed: an unset key must not mean "allow everything".
  if (!expected) {
    throw new Response("INTERNAL_API_KEY is not configured", { status: 500 });
  }

  const provided = request.headers.get(INTERNAL_API_KEY_HEADER);
  if (!provided || !constantTimeEquals(provided, expected)) {
    throw new Response("Unauthorized", { status: 401 });
  }
}

function constantTimeEquals(a: string, b: string): boolean {
  const bufferA = Buffer.from(a, "utf8");
  const bufferB = Buffer.from(b, "utf8");
  // timingSafeEqual throws on length mismatch, which would itself leak length. Compare
  // lengths first and still run the constant-time check on equal-length inputs.
  if (bufferA.length !== bufferB.length) return false;
  return timingSafeEqual(bufferA, bufferB);
}
