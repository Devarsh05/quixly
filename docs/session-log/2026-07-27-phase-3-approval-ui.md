# 2026-07-27 — Phase 3 Step 3: the approval gate

**Landed as `e0b1b82` on `phase-3-approval-ui`** (one commit; PR'd to `main` 2026-07-28).
**Goal:** give the merchant the gate the PRD is built around — review each proposed fix with its
before/after diff and its source, then approve or reject it. Nothing reaches a store without
passing through here.

---

## Boundary (the thing to hold onto)

This step **READS `fixes` and WRITES exactly one column, `fixes.status`.** Zero Shopify Admin API
calls. The Optimizer and the not-yet-existing Publisher are untouched. Approval is a status
transition — `proposed → approved | rejected` — and Step 4 is what later acts on approved rows.
Keeping "decide" and "write to the store" in different steps is what makes the gate auditable.

## Agent — `agent/app/api/fixes.py` (new router, mirrors `api/scan.py`)

- **`POST .../fixes/run`** enqueues an audit+optimize run, committing the run **before** the enqueue.
  `run_fix_task` was registered in the worker but *nothing triggered it* — a fresh shop's page could
  never be populated. This closed that hole.
- **`GET .../fixes`** returns the render payload, run-scoped exactly like `get_report`. `agent_runs`
  has no run-kind discriminator, so "latest fix run" derives from `MAX(fixes.run_id)` rather than
  adding a column for it. An empty catalog returns `status=running, products: []` — no
  special-casing, and a shop with nothing to review is a normal state, not a 404.
- **`POST .../fixes/{id}/approve|reject`** flips the status.

**The agent computes the render payload so the shell derives nothing** — `added_lines` for a
description fix, the category from/to, the metafield key/value, and the `approvable` /
`block_reason` split. `_added_lines` extracts the appended block from the append-only body diff with
a `startswith` guard that is *a guard, not an assumption*: if the composer's shape ever changes it
falls back to showing the whole block rather than a confidently wrong diff.

## The four decisions worth remembering

1. **Approvability is enforced server-side, not just hidden in the UI.** `APPROVABLE_TYPES` is
   exactly `{description, category}`. A taxonomy `metafield` fix is blocked on
   `read_metaobject_definitions`; a `merchant_todo` has no `after_json` and is not publishable at
   all. Both are refused with **409**, with the reason shown to the merchant verbatim. The UI is not
   a security boundary, and the invariant is: **no approvable path to a write that cannot execute.**
2. **A non-`proposed` state 409s — deliberately not a silent no-op.** Repeating the *same* decision
   is idempotent; anything else conflicts. A silent success is precisely what would hide the
   double-submit and stale-UI bugs this gate exists to catch.
3. **Ownership is a join, and a mismatch is 404 rather than 403.** `fixes → products → shops`.
   Without the join a shop could act on another shop's fixes by guessing an integer; with a 403, ids
   could still be probed for existence.
4. **Supersede — at most ONE approved fix per `(product_id, target)`.** Approving marks the other
   `proposed`/`approved` rows on that target `stale`, because Step 4's description composer
   **appends**: two approved rows for one body would append the Details block twice. Conversely,
   approved and rejected rows from earlier runs are **never touched by a new run** — they are
   merchant decisions, and discarding them would silently drop consent.

## App shell — `app/app/routes/app.fixes.tsx`

Mirrors `app.audit.tsx`: `authenticate.admin` loader, typed agent client, Polaris `s-*` components,
poll only while a run is in flight. **The shop identity comes from the session, never a form field.**
Four row types render distinctly, because collapsing them would misrepresent the risk:

- **description** — the added lines as readable text, not raw HTML
- **category** — publish-class (tax/channel consequences), so it gets its own warning-toned banner
  *above* the routine fixes, with its own control
- **taxonomy metafield** — **no approve control at all**, rather than a disabled one
- **merchant to-do** — a separate "needs your input" list

Every approvable fix shows where its value came from; a fix arriving **without** a citation is
flagged rather than silently rendered — the citation is the trust mechanism, not optional chrome.

## Schema

**No change. No migration.** `fixes.status` already carried `proposed/approved/rejected/stale`.

## Evidence

- `agent`: 306 passed, 2 skipped (`tests/test_fixes_route.py`, 425 lines); `ruff check` clean.
- `app`: lint, typecheck, 45 tests (`tests/app.fixes.test.ts`), build — all clean.
- **Live against the dev store:** a real run produced product 113's four row types correctly;
  approve/reject landed in `psql`; **both 409 guards fired** (non-approvable type, and a re-decide
  on a non-`proposed` row); a second run left exactly one approved row per `(product, target)`.

## Where Phase 3 stands

- **Done:** per-product audit + three-state model (Gate G/M); write-target spike (2c); grounded
  Optimizer with taxonomy/description/category routing + gap→row invariant (2d); negative grounding
  (2e); scope grant + live channel diagnostic (step 4-preamble); **the approval gate (step 3)**.
- **Remaining:** Step 4 Publisher — Admin API writes on **`category` + `description`** (both proven
  writable), dev store first, re-read + parse-check after publish. The **taxonomy metafield** path
  stays deferred: blocked on `read_metaobject_definitions` (a third merchant consent) *plus* an
  unproven metaobject-entry surface. See `docs/backlog.md` → Taxonomy write path.
