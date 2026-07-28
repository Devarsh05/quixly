# 2026-07-28 — Phase 3 Step 4: the Publisher

Branch `phase-3-publisher` off `main` (`8e04917`, Step 3 merged as PR #18). Commit `743aa01`.
**The first code in this project that writes to a live merchant store.**

## What shipped

Approved fixes are published on the two paths proven writable in
`docs/decisions/2c-write-target.md`, as **two risk tiers rather than one publish loop**:
an append-only `productUpdate(descriptionHtml)` (LOW — reversible, the field Copilot reads) and a
scalar-GID `productUpdate(category)` (HIGH — real tax/sales-channel consequences).

`agent/app/graph/publisher.py` is the state machine; `agent/app/jobs/publish.py` drives the run;
`POST /shops/by-domain/{shop}/fixes/publish` is the only route that leads to a merchant write.

## The token decision, and why

**The agent performs the write**, pulling short-lived tokens through the existing
`ShopifyAdminClient` → `TokenProvider`, i.e. minted by the app shell. No new token path, no change
to `admin-token.server.ts`, no new internal endpoint.

The alternative considered was "the app shell performs the write, the agent tells it what". Rejected
because **the invariant is a single *refresh* authority, not a single *caller*** — the agent has
read Shopify this way since Phase 1, and a write is the same token with a different verb. Moving the
write into the shell would either drag staleness gating, per-type verification and status
transitions into the thin layer, or turn the shell into a GraphQL proxy re-implementing the
401-retry and leaky-bucket handling — a second, untested copy of the riskiest client code, on the
riskiest path.

## The three guarantees

1. **Staleness — two hard layers, both before any write.** Layer 1 is exact: the live field must
   still equal `before_json`, so a description is never appended to a changed body and a category
   never assigned over a merchant's own choice. Layer 2 recomputes `base_source_hash` from the live
   read, catching drift in fields the fix *grounded on* but does not *write*.
2. **A 200 is not a success.** Nothing reaches `verified` without a **separate** re-read; the
   mutation's own return payload is never evidence.
3. **Replay cannot double-append.** Only `approved` rows are work items, and reconciliation runs
   *before* the staleness gate, so a write that landed before a crash verifies with zero mutations.
   The only paths out of `approved` are "already landed" (no write) and "still equals `before_json`"
   (safe to write); a body matching neither is `stale`.

## Two findings that changed the design

**`base_source_hash` was not writer-stable.** `products` has two writers that store `variants_json`
in different shapes — the GraphQL ingest job and the REST `products/update` webhook — and our own
publish fires that webhook moments later. A live re-read could never have reproduced a stored
digest, so the gate would have refused every write. Fixed with a projection both writers spell
identically (`services.catalog.stable_source_hash`); `_build_source_fields` is untouched so
grounding is unchanged. Detail: `2c-write-target.md` L11.

**A refused write arrives in two shapes.** A bad `TaxonomyCategory` GID comes back as a **top-level
GraphQL error**, not `userErrors`; a nonexistent product comes back as **200 + `userErrors`**.
Checking either one alone misses the other. Both now raise and both land on `publish_failed`.
Detail: L10. This was found by a live test failing, not by reasoning.

## Schema

One migration (`530075ef94b8`), authorized in planning: `fixes.publish_error TEXT NULL` and
`fixes.published_at TIMESTAMPTZ NULL`, **one meaning per column** — `published_at` is set only on a
confirmed re-read (the publish audit trail Phase 4's Verifier reads), `publish_error` is why a
publish did not land and is what the merchant is shown. `fixes.reason` keeps grounding/to-do
semantics and the Publisher never writes it. `FixStatus.publish_failed` is code-only.
Drift check empty; downgrade round-trips.

## Live evidence (dev store)

Full pipeline, not a script. Details and raw numbers in `2c-write-target.md` L9–L12.

- **description** on Colombia Huila: body 146 → 211 chars, Details block live, merchant copy intact,
  `verified` + `published_at`.
- **category** on Kenya AA: `Uncategorized` → `fb-1-3-1`, confirmed by re-read.
- **replay**: a fix forced back to `approved` re-verified while Shopify's `updatedAt` stayed at the
  original write — **no mutation issued**. ("Body unchanged" alone would not have proved this:
  `after_json` is fixed at propose time, so a re-write produces the same bytes. The timestamp is the
  proof.)
- **staleness**: a product edited after approval was refused — `stale`, cause recorded, no write.
- **nothing approved**: HTTP 409, no run.
- **byte-identity RESOLVED**: Shopify echoes `descriptionHtml` back byte-for-byte, so the structural
  fallback is a safety net rather than the routine path.

Product 113 was used for the live contract tests and **restored** (body and category byte-identical
to baseline). The Colombia Huila / Kenya AA publishes were left in place — they are intended dev-store
demo state.

## Gates

Agent: ruff clean, **346 passed / 7 skipped**. App: lint, typecheck, **53 tests**, build all clean.
Alembic: `upgrade head` applied, autogenerate diff **empty**, `downgrade` clean.
Live contract suite (`test_publisher_live.py`, opt-in via `QUIXLY_LIVE_PUBLISH_TEST=1`): **5 passed**.

## Deliberately not done

- **No taxonomy metafield writes** — still blocked on `read_metaobject_definitions`. An approved
  `metafield` row **aborts the run** rather than being skipped: Step 3 refuses to approve one, so
  its presence would mean the gate was bypassed.
- **No Verifier / uplift measurement.** The re-read confirms the write landed; it measures no
  share-of-model uplift. That is Phase 4.
- **No rollback/revert flow** (`reverts_fix_id` stays unused), no scheduled or auto publishing, no
  new scope, no billing.
- **Publishing is not clicked through the embedded UI in this log.** The UI's loader/action wiring
  is covered by `app/tests/app.fixes.test.ts`; the live run drove the same agent routes the UI calls,
  with identical payloads.

## Rollout

`PUBLISH_ALLOWED_SHOPS` defaults to the dev store alone and the job refuses anything else **before
reading**. Widening it is a deliberate commit. Within a run, description-only products publish
first and `description` precedes `category` within a product, so a systemic problem surfaces on a
reversible write before a tax-relevant one.
