# 2026-07-30 — Phase 4 Step 2: the uplift chart

Branch `phase-4-uplift-chart` off `main` (`2b11a8b`).
**The first merchant-facing surface over the Verifier's output.** Read-only: it renders rows the
Verifier already wrote. No schema change, no migration, no Shopify call, no write of any kind.

## The problem this step is actually solving

It is not "draw a chart". It is that **the only real verification row that exists renders as a lie
if you draw it naively.**

Run 2787 holds `delta = 0.0` with `settle_satisfied = false`. It measured *nothing* — 12.6h of a
168h window had elapsed and no engine had re-crawled. Plotted as a number it reads "0% uplift,
your fixes did nothing." Separately, a flaky engine writes `pre_rate`/`post_rate` NULL over
`total_queries = 0`; one `?? 0` in a component turns that into `0.0 - pre_rate` and shows the
merchant a **fabricated regression**. Both failures are invisible at the point they'd be written.

## The decision that shaped everything: the state matrix lives in Python

`app/vitest.config.ts` is `environment: "node"` with `include: ["tests/**/*.test.ts"]` — no DOM,
no renderer, and `.tsx` is not even matched. **Display logic placed in the component is logic
nothing checks.** So the classification moved agent-side into a new pure module,
`agent/app/services/uplift.py` (no DB, no I/O), and the shell became a switch over its output.

CLAUDE.md's thin-shell rule wanted the same split for architectural reasons. Arriving at it from
testability is the stronger argument, and it is the one recorded.

Two flags carry the invariants:

- **`deltas_reportable`** — `status == "completed"` AND rows exist AND `settle_satisfied`. The
  single gate on rendering a delta figure. Unsettled shows the rates as a labelled *reading*, with
  no delta and no chart.
- **`EngineState`** — nullness is checked **before** direction, so a missing side is `no_data_*`
  and never a direction. `no_movement` (settled zero with real queries both sides) stays a
  distinct, genuine finding.

**`classify_run` is an allowlist on `completed`, not a fall-through** — this was a review catch,
and it was right. The first draft made `settled` the default branch. `agent_runs.status` is a
plain `String(32)` whose model docstring says *"adding a status is a code-only change"*, so a
future `queued` would have inherited `settled` and become reportable. Unknown → `empty`.

## Read path: extend the existing route, add one series route

`GET .../verification` was **extended, not replaced** — four additive fields
(`state`, `deltas_reportable`, `measured_at`, `settle_hours_required`) plus `state` on each engine.
`measured_at` is `verifications.created_at`, which already existed: **no column was needed**, and
the "if you think you need one, stop" instruction never fired.

Additive fields are only safe against field-level assertions, so that was **checked rather than
assumed** before writing any: every assertion in `test_verify_route.py` reads a named key, and the
only three `response.json() == {...}` in the agent suite are in `test_health.py` and
`test_fixes_route.py`, on routes this branch does not touch.

New `GET .../verifications?limit=12` returns runs **oldest → newest** — a series cannot be built
from a single-run route without N round-trips, and a chart reads left to right. A known shop with
no verifications gets **200 `{"runs": []}`**, not a 404 (following `api.fixes.list_fixes`); 404 is
reserved for an unknown shop. Both routes assemble through one `_verification_view` helper, and a
test asserts `series["runs"][-1] == single` so they cannot drift.

**Consequence worth recording: the page does not poll.** Verification rows are written only at
aggregation time, so an in-flight run never appears in the series at all — there is no in-flight
state to observe, unlike the audit and fixes pages which poll a run they just started.

## Rendering: hand-rolled SVG, because polaris-viz is a foreign stack here

`@shopify/polaris` and `@shopify/polaris-viz` are **both absent** — this app is Polaris *web
components* (`s-page`, `s-section`, …) typed by `@shopify/polaris-types`. polaris-viz is a
Polaris-**React** library, so adopting it means a second UI stack for a two-bar chart. Paired
before/after `<rect>`s in an inline `<svg>`, `role="img"` with a `<title>`, every bar labelled with
its own value so meaning is never colour-only. Standard JSX intrinsics, so `app-bridge.d.ts` needed
no new ambient declaration.

One typecheck catch: **`s-banner` takes `tone="warning"`; `caution` is `s-badge`'s vocabulary.**

## Evidence

- **agent**: 420 passed / 7 skipped (392 before — 28 new: 16 pure-matrix, 12 route). `ruff` clean
  after one E501. Alembic autogenerate diff **empty** (a confirmation, not a risk: no model
  changed). *Caveat: the local DB has no `shopify.Session`, since `app/.env` points at Neon, so the
  cross-tool half of the fence was not exercised this session.*
- **app**: lint, typecheck and build clean; 57 tests green across the five non-DB files.
- **The acceptance case is proven by rendering, not by intent.**
  `app/tests/app.uplift.render.test.ts` renders the component to static markup (no DOM needed) with
  run 2787's exact row shape and asserts the output contains "Measurement pending" and **does not
  contain** a delta figure or `<svg>`. The negative assertions are falsifiable: the settled cases
  in the same file assert those same strings *are* present.

## Two environment facts that cost time

- **Both `app/.env` and `agent/.env` point at Neon**, and the campus network blocks outbound 5432,
  so anything touching a database fails locally with `P1001`. The agent suite was run against the
  docker-compose Postgres with a per-command `$env:DATABASE_URL` override — the same precedence
  CLAUDE.md warns about as an *accidental* footgun, used deliberately.
- **The app's DB-touching tests (`admin-token-rotation`, `webhook-refresh-*`) cannot pass on this
  workstation** — they need the Prisma `session` table on an unreachable Neon. Confirmed
  **pre-existing** by stashing the branch and reproducing the identical failure on a clean tree,
  rather than by assuming.

## Deliberately not done

- **No verify trigger.** The page has no `action` and cannot start a measurement; a test asserts
  the module exports none. Wiring `POST .../verify` means rendering its four distinct 409 branches
  as merchant copy — a separate increment.
- **No competitor overlay** (the data is already in `competitor_deltas_json`; a later component,
  not a contract change) and **no trajectory chart** — the contract holds a series, but with one
  run a second chart would be dead code.
- **No visual confirmation against real data.** Run 2787 lives on Neon; the rendered-markup tests
  cover its shape, but nobody has looked at the deployed page. Worth doing when the settled row
  lands **2026-08-04**.
- **The headline selector is still `runs[last]`** — correct with one run, wrong the moment weekly
  scans fire an unsettled re-measurement and hide the standing settled result. Recorded in
  `docs/backlog.md`, tied to the weekly-scans item so the two land together.
