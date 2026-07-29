# 2026-07-29 — Phase 4 Step 1: Verifier dev-store acceptance run

Branch `phase-4-verifier` at `1c1ea9a`; **no code changed this session** — an acceptance run only.
**The Verifier measurement core, run live against a real engine for the first time.**
The 2026-07-28 log closed with "no live dev-store run" as the outstanding item; this closes it.

Protocol was **predict-then-run**: the expected row was written out and the preconditions pasted
*before* the POST, then checked against the prediction rather than read back post-hoc. Cost: one
24-query Perplexity fan-out. No Shopify call of any kind (the measurement core is Shopify-free by
exclusion), no schema change, no code change.

## Preconditions (shop 92, `quixly-ljymkoyb.myshopify.com`)

Baseline **run 137** — `completed` 2026-07-21 00:18:04.692109Z, panel 316, one `share_of_model`
row (`perplexity`, 0 mentions / 24 queries, `our_rate = 0`).

Measured set — the two `verified` fixes:

| fix | product | type | target | `published_at` |
|---|---|---|---|---|
| 9710 | 115 (Kenya AA) | `category` | `category` | 2026-07-28 14:51:27.129221Z |
| 9702 | 114 (Colombia Huila) | `description` | `body_html` | 2026-07-28 14:52:09.500127Z |

The session's opening brief had these two timestamps assigned to the wrong fix ids; the DB read
won and the prediction was corrected before the run. `published_at_max` therefore belongs to
**9702**, and the manifest's first element is **9710** (ordered by `published_at`).

**The baseline-selection trap fired exactly as designed.** All 8 runs on shop 92 are `completed`
and all 8 sit on panel 316; only 75 and 137 satisfy `EXISTS (share_of_model WHERE run_id = …)`.
"Latest completed run" would have picked 1576, a publish run. Tier one of the two-tier anchor
applied — latest scan predating `min(published_at) = 14:51:27` → **137** — so the `after` filter
excluded nothing and M was the full set, not a subset.

Panel 316's fingerprint was **recomputed offline before spending anything**, since a mismatch
aborts the run: `_fingerprint` reproduced the stored
`1c4539c5bfdf6f9c9340d9fda79376d422a2ba4b5b311d9771a97956ba4e5b6e` exactly.

## Two operational preconditions worth recording

- **`arq` does not hot-reload, and the running worker predated the Verifier commit.** The four
  worker PIDs started 2026-07-28 10:43 local; `ba04dd1` (which added `jobs/verify.py` and its
  registration) landed 19:10. Posting first would have returned 202, created an `agent_runs` row,
  and left it wedged `running` forever on an unknown function — no engine spend, but a polluted
  table. The worker was restarted (only the four arq PIDs — never `python.exe /F`, which takes
  uvicorn with it) and its startup line confirmed before the POST:
  `Starting worker for 5 functions: … run_verify_task`. uvicorn's `--reload` *had* picked up the
  routes; both appear in the live `openapi.json`.
- **Zero orphaned runs before starting** (`max(id) = 1576`, all 8 shop-92 runs `completed`), so any
  wedged row afterwards would be attributable to this run.

## The run

`POST /shops/by-domain/quixly-ljymkoyb.myshopify.com/verify?force=true` → **202**
`{"run_id":2787,"baseline_run_id":137,"status":"running","measured_fix_count":2,"settle_satisfied":false}`

Run 2787 `completed` in 35.31s. Worker logged the label independently:
`Verification run 2787 recorded UNSETTLED (12.6h < 168h required)`.

## Prediction vs. result

| predicted | actual |
|---|---|
| `baseline_run_id = 137` | 137 |
| `pre_rate = 0.0`, byte-equal to run 137's persisted `our_rate` | `0` |
| `delta = post_rate − pre_rate` (relation, not a number) | holds — arithmetic below |
| manifest = exactly {9710, 9702} in `published_at` order | 9710 then 9702, nothing else |
| `measured_fix_count = 2` | 2 |
| `published_at_max = 14:52:09.500127Z` | matches |
| `panel_fingerprint` = panel 316's | `1c4539c5…ba4e5b6e` |
| `settle_hours` 12.5–12.7 | **12.60675095388889** |
| `settle_satisfied = false` | `f` |
| `post_rate` — deliberately **not** predicted | `0` over 24 queries |

Exactly one row for run 2787 (`count(*) OVER () = 1`), engine `perplexity`.

**Delta, by hand from the two `share_of_model` rows:** pre = 0/24 = 0.0 · post = 0/24 = 0.0 ·
`delta = 0.0 − 0.0 = 0.0`, matching the persisted `delta = 0`.

**`settle_hours`, by hand:** post run `started_at` 2026-07-29 03:28:33.803561Z −
`published_at_max` 2026-07-28 14:52:09.500127Z = 12h 36m 24.303434s =
12 + 0.6 + 0.00675095389 = **12.60675095389 h**, matching the persisted value to the last digit.
Anchoring on `started_at` rather than `completed_at` is the deliberate lower bound.

**`fixes.published_at` unchanged — the measurement-sacred check.** Snapshotted by md5 before the
run and re-read after; both byte-identical: 9702 `97b56bbd8a1970e798024e3e8ccb2e90`, 9710
`f65e6ff6c6d7be9fc9bd07bfc761807b`. The Publisher remains the column's only writer.

## The competitor split is what proves the Verifier read its own aggregates

`our_rate` is `0.0` on **both** sides, so that column alone cannot distinguish "read run 2787's
row" from "read run 137's row twice" — the acceptance criterion would have been unfalsifiable on
the headline number. The competitor rates settle it. Run 2787 wrote its own `share_of_model` row
(`created_at 2026-07-29 03:29:09.494766Z`) with materially different values, and the persisted
`pre_rate`s track 137 while the `post_rate`s track 2787:

| competitor | 137 → `pre_rate` | 2787 → `post_rate` | persisted `delta` |
|---|---|---|---|
| Stumptown | 0.125 | 0.25 | +0.125 |
| Blue Bottle | 0.0 | 0.041666666666666664 | +0.041666666666666664 |
| Intelligentsia | 0.16666666666666666 | 0.125 | −0.04166666666666666 |
| Counter Culture | 0.20833333333333334 | 0.25 | +0.04166666666666666 |
| Onyx Coffee Lab | 0.08333333333333333 | 0.08333333333333333 | 0.0 |

Generalises: **when the headline metric is identical on both sides, a secondary field that moved
is the only available proof of provenance.** Worth reaching for whenever an acceptance check risks
being satisfied by the wrong data.

## Two readings that matter

- **`post_rate = 0.0` is a real zero, not a no-data NULL.** `post_total_queries = 24`, so
  `_side_rates`' `has_data` branch passed the literal rate through. This run exercised live exactly
  the case the Gate-2 guard exists to *permit* — a genuine 0.0 over a non-zero denominator. The
  guard's NULL branch (0.0 over 0 queries → fabricated regression) remains **test-only**; no live
  flake has produced it.
- **`delta = 0.0` means no measured uplift, and that is the expected outcome, not a failure.**
  12.6h of the required 168h have elapsed and engines have not re-crawled. The row is permanently
  labelled `settle_satisfied = false` with the true `settle_hours` — which is precisely the
  labelling this run was meant to confirm. **A settled re-measurement is available 2026-08-04**
  and should be run without `force`.

`GET .../verification` read the row back with the manifest, both engine sides and all five
competitor deltas intact.

## Negatives

- **Re-POST without `force` → 409**, with the real elapsed figure in the detail:
  `"Only 12.6h since the last publish; 168h are required…"`.
- **Verify on a shop with an empty measured set → NOT run live.** `shops` contains exactly one row
  (shop 92, 2 verified fixes), so exercising this branch would have required inserting a throwaway
  shop — a write outside what the session authorized. Left **test-covered, not live**:
  `tests/test_verify_route.py:151` asserts the `"nothing to measure"` 409; the three covering route
  tests were re-run green. The adjacent shop-resolution branch *was* probed live (unknown domain →
  **404** `"Shop not found."`, zero writes). Cheap to close whenever a second shop exists.

## Integrity sweep after the run

One `verifications` row total; zero shop-92 runs in any state but `completed`; `max(agent_runs.id)`
still 2787, so the negative probes created no runs. `PUBLISH_ALLOWED_SHOPS` untouched, no other
shop touched.

## Deliberately not done

- **No settled measurement** — blocked on the clock until 2026-08-04, by design.
- **No UI.** The uplift chart is the next Phase 4 item; nothing in `app/` changed.
- **No Browserbase simulation, no scheduled scans, no new scope.**
- **No agentic-channel readback.** If a later step adds one it must use
  `product.resourcePublications` and must not gate on `resourcePublicationsCount`.
