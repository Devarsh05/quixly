# 2026-07-28 — Phase 4 Step 1: the Verifier measurement core

Branch `phase-4-verifier` off `main` (`87c8ffb`, Step 4 merged as PR #20).
**The first code that measures whether any of this worked.**

## What shipped

A post-publish scan on the *pinned* panel plus a per-engine share-of-model delta against a
pre-publish baseline, anchored to `fixes.published_at`. `agent/app/graph/verifier.py` is the
aggregation node, `services/baselines.py` picks the baseline and snapshots the measured set,
`jobs/verify.py` drives the run, and `POST /shops/by-domain/{shop}/verify` +
`GET .../verification` are the routes.

**It is orchestration, not a second pipeline.** `jobs/scan.py`'s three-node core was extracted as
`run_scan_pipeline` (EngineRunner → Extractor → ShareOfModelAggregator) and both jobs call it, so
**a verification run IS a scan run** — it writes `engine_runs` and `share_of_model` under its own
`run_id`, and the existing report route reads it with no special-casing. A forked scan would have
drifted from the one that produced every baseline.

## The grain decision: PRD §8 is superseded

PRD §8 sketches `verifications(id, fix_id, pre_rate, post_rate, delta, ts)`. That grain cannot be
honoured without lying. Pre/post rates are shop/engine-level quantities over a 24-query panel, and
the two fixes on the dev store were published **42 seconds apart** — attribution between them is
unrecoverable in principle. PRD §13 names exactly this ("attribution is messy… sell the clean
leading metric"), so writing one shop-level delta onto N fix rows would manufacture N causal claims
out of a single correlational observation.

Grain is therefore `(run_id, engine)`. Per-fix survives as `measured_fixes_json`, a GIN-indexed
JSONB manifest — **annotation, not attribution**. JSONB rather than a join table because it is a
*snapshot*: a FK'd table would CASCADE rows away and silently rewrite history a merchant was
already shown, and a relational edge invites the very per-fix join the grain exists to prevent.

## Gate 2 — the assumed NULL contract, checked instead of trusted

The plan's NULL-propagation safety rested on an assumption: that `share_of_model.our_rate` is NULL
(not 0.0) when an engine returns nothing usable. **Investigated before writing any delta code**
(`graph/share_of_model.py:126–176`). What it actually does:

- **Two shapes, not one.** If the engine produced ≥1 `engine_runs` row (the normal flake case —
  `run_engine` inserts a row per query even when the call raises), a row **is** written. If it
  produced zero rows, the engine never enters `_latest_per_query` and **no row exists at all**.
- `our_rate` = `None` → SQL NULL. `total_queries` = **0**, written explicitly. `our_mentions` = 0,
  `competitor_rates_json` = `{}`. Existing coverage:
  `test_share_of_model.py::test_fully_degraded_engine_writes_null_rate`.

So the contract **holds today** — and the Verifier still does not inherit it, for three reasons:
`total_queries` and `our_mentions` are **nullable columns**; there is **no CHECK** tying
`our_rate IS NULL` to `total_queries = 0`; and the missing-row shape is a third case the rate
column cannot express at all.

`_side_rates` therefore gates on `total_queries`, not `our_rate`: a missing row, NULL, or 0 is
no-data and the rate goes NULL **regardless of what `our_rate` literally holds**, taking that
side's competitor rates with it. This is what stops the concrete failure — a flaky post-scan
writing `0.0 / 0` and the Verifier computing `0.0 - pre_rate`, showing the merchant a **fabricated
regression**. Three tests cover it, including one seeding a literal `0.0` over 0 queries.

## Gate 1 — the measured set is snapshotted once

`resolve_measured_set` runs at the **route**, before any engine spend, and the list is threaded
through the Arq payload into the job, which consumes it verbatim. The settle gate,
`published_at_max` and the persisted manifest all read that one snapshot; `fixes` is never
re-queried at aggregation time. Otherwise a publish landing in between would let the 409 the
merchant already received disagree with the row that finally lands — both internally consistent,
silently describing different windows.

## The baseline anchor: wrong twice before it was right

It took two corrections, both driven by a test rather than by reasoning, and neither symmetric.

**First `min`, changed to `max`.** `test_verify_route.py` failed with a 409 on a shop that clearly
had a usable baseline: anchoring on the *earliest* publish means a shop that published before its
first scan has nothing old enough to baseline against, and is **permanently unmeasurable** —
forever, including for everything it publishes later.

**Then `max`, caught in review as wrong the other way.** A `max` anchor picks the latest scan
predating the *last* publish, which on staggered publishes is a scan sitting **between** two of
them. That bakes the earlier fix into the pre-rate *and* drops it from M, so the very change being
measured is counted as part of the "before" — a systematic **understatement of uplift**. The
suite could not catch it: every test seeded a single `PUBLISHED_AT`, and with one publish `min`
and `max` are the same value, so the branch was never exercised. The docstring's justification for
`max` described a case that cannot distinguish the two.

**Resolved as one two-tier rule**, not min-with-an-escape-hatch: *the latest scan predating
`min(published_at)`, if one exists; otherwise the latest scan predating `max(published_at)`.*
Tier one keeps M maximal on the normal path; tier two exists solely so the install → publish →
scan pattern is measurable at all. On the fallback tier M is deliberately a **subset** — no
pre-state for those fixes exists anywhere in the data, so the alternative isn't a better number,
it's a fabricated one — and `measured_fixes_json` names only what was actually measured, so a
subset is never reported as the whole.

The anchor now lives **inside `select_baseline_run`** and is no longer a caller-supplied `before`
argument. That is the durable part of the fix: both bugs were the *route* computing an anchor, and
a caller that can compute the anchor is a caller that can get it wrong.

## Live-DB findings that shaped the code (shop 92, via psql)

- **A genuine baseline exists**: runs 75 and 137 both completed 7 days before
  `min(published_at) = 2026-07-28 14:51:27Z`, both on panel 316, both with `share_of_model` rows.
  Both carry `our_rate = 0.0` — a floor, worth stating as a caveat in any report.
- **"Completed" does not identify a scan, and neither does `panel_id`.** All 8 runs sit on panel
  316 (fix/publish runs borrow it because `agent_runs.panel_id` is NOT NULL), and run **623** is
  `completed` with zero `engine_runs` and zero aggregates. "The latest completed run" picks 1576,
  a publish run. Selection keys on `EXISTS (share_of_model WHERE run_id = …)` instead.
- **The panel is already immutable** — `upsert_panel` on conflict touches only `category`, so
  `queries_json` is frozen per fingerprint. The hole was in the **route**: `start_scan` calls
  `build_query_panel()` fresh, so a post-scan after any Interrogator edit would silently bind to a
  different panel. The verify route **binds to the baseline's `panel_id`** and never calls the
  builder; the node re-asserts the binding and recomputes the fingerprint.

## Schema

One migration (`2bf30ca663a2`), `public` only, no Prisma model: `verifications`, unique on
`(run_id, engine)`, GIN index on `measured_fixes_json`. FK asymmetry is deliberate — `run_id` (the
post scan) CASCADEs like `share_of_model.run_id`, while `baseline_run_id` / `panel_id` SET NULL
like `engine_runs.run_id`, because every pre-side value is denormalized onto the row and a
measurement record must survive deletion of old run metadata. `published_at_max` and
`settle_hours` are NOT NULL (an empty measured set is refused before a row can exist).
Drift check **empty**; downgrade round-trips.

## Settle window

`VERIFY_SETTLE_HOURS` defaults to **168h (7 days)**, matching the weekly-scan cadence and honest
about the fact that no engine publishes a re-crawl SLA. Inside the window the route 409s unless
`force=true` — which is a **label, not a bypass**: the row persists the real `settle_hours` and
`settle_satisfied = false`, so an early measurement can never be read back as settled.

## Gates

Agent: ruff clean, **392 passed / 7 skipped** (346 before; 46 new). App: lint, typecheck,
**53 tests**, build all clean. Alembic: `upgrade head` applied, autogenerate diff **empty** while
`shopify.Session` exists, `downgrade` round-trips. The existing scan suite is the refactor's
regression guard and passed **unchanged** — `test_scan_task.py`, `test_scan_route.py` and
`test_share_of_model.py` have a zero-line diff.

## Deliberately not done

- **No live dev-store run.** The measurement core is untested against a real engine; that is the
  next reviewed step and needs `force=true` (the settle window is not met until 2026-08-04).
- **No UI.** The uplift chart is a separate Phase 4 item; nothing in `app/` changed.
- **No `agent_runs.kind` column** — a verification run genuinely is a scan run, so it would have
  been redundant here. The backlog entry stands.
- **No scheduled scans, no Browserbase simulation, no new scope, no Shopify call of any kind.**
