/**
 * Per-shop serialization of Shopify token rotation.
 *
 * Rotating a shop's offline token retires the previous token AND invalidates its refresh
 * token immediately. So two rotations racing for the same shop is not a lost update — it
 * permanently breaks the refresh chain, and the merchant has to reinstall. Rotation must
 * therefore be mutually exclusive per shop.
 *
 * A Postgres advisory lock is used rather than an in-process mutex because the app shell
 * runs as multiple processes/instances; an in-process lock would only serialize one of
 * them. `pg_advisory_xact_lock` is held for the duration of the surrounding transaction
 * and released automatically on commit/rollback — including if the process dies, which a
 * row-based lock could not guarantee.
 *
 * Every code path that can rotate a token must run inside this lock, and must re-read the
 * session AFTER acquiring it (another holder may have just rotated it).
 */

import type { Prisma } from "@prisma/client";

import prisma from "../db.server";

/** Namespace so our keys cannot collide with any other advisory lock in this database. */
const LOCK_NAMESPACE = 0x5158n; // "QX"

/** A refresh does an HTTP round-trip to Shopify, so allow well beyond Prisma's 5s default. */
const LOCK_TIMEOUT_MS = 20_000;
const LOCK_MAX_WAIT_MS = 15_000;

/**
 * Namespace and shop hash packed into one signed 64-bit key.
 *
 * Postgres offers `pg_advisory_xact_lock(bigint)` and `(int4, int4)` but NOT
 * `(bigint, bigint)` — and Prisma binds JS numbers as bigint, so the two-arg form fails to
 * resolve. Packing into a single bigint sidesteps that entirely.
 */
function shopLockKey(shop: string): bigint {
  let hash = 0x811c9dc5;
  for (let i = 0; i < shop.length; i++) {
    hash ^= shop.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  // >>> 0 makes it unsigned before widening, so the low 32 bits are the hash exactly.
  const key = (LOCK_NAMESPACE << 32n) | BigInt(hash >>> 0);
  // Wrap into signed int64 range, which is what bigint means in Postgres.
  return BigInt.asIntN(64, key);
}

/**
 * Run `fn` holding the exclusive rotation lock for `shop`.
 *
 * Callers block (they do not fail) while another holder rotates, then observe the rotated
 * session. Re-read session state inside `fn` — whatever you loaded before calling this may
 * be stale by the time the lock is granted.
 */
export async function withShopRefreshLock<T>(
  shop: string,
  fn: (tx: Prisma.TransactionClient) => Promise<T>,
): Promise<T> {
  return prisma.$transaction(
    async (tx) => {
      await tx.$executeRaw`SELECT pg_advisory_xact_lock(${shopLockKey(shop)}::bigint)`;
      return fn(tx);
    },
    { timeout: LOCK_TIMEOUT_MS, maxWait: LOCK_MAX_WAIT_MS },
  );
}
