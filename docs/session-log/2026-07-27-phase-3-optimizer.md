# 2026-07-27 — Phase 3: Optimizer close-out (steps 2b/2c/2d + routing fix)

**Landed as `9953069` on `phase-3-retarget`** (2d re-architecture + routing fix, one commit; not
yet PR'd to `main`); this log is docs-only.
**Goal:** make the grounded Optimizer target the channels AI shopping engines actually read, prove
it on the real catalog with the demo seed gone, and record why `custom.*` was abandoned.

---

## Step 2b — three-state model + structural targeting (the decoupling)

Step 2 shipped an Optimizer that produced ~all to-dos and zero natural fills; the only fills came
from a seed that *injected* a spec into `variants_json` (a field the audit doesn't read). Root
cause: targeting followed the audit's gaps, but a gap means the rubric's `detect` found nothing —
so a fill could only happen where `detect` had FAILED. Detection quality and fill capability were
inversely coupled.

2b severed that. Every (product, family) is now `structured` (in a metafield) / `unstructured` (in
prose only) / `absent` (nowhere). The Optimizer's targets became a purely structural set —
`spec_families − structured_families(metafields)` — never `audit.gaps`, so refining `detect` moves
only the audit's numbers, never a fill. Two coverage numbers replaced one, and Gate G was re-baked
and re-approved.

## Step 2c — the write-target spike (three live sessions)

Before writing more fills, we asked *which channel an AI engine reads*. Three live sessions against
the dev store (recorded in full in `docs/decisions/2c-write-target.md`) resolved it:

- **`custom.*` is not the channel.** Shopify feeds agentic surfaces from the reserved `shopify`
  taxonomy-attribute metafields and the mapped description/title/category — not arbitrary `custom.*`
  fields. The 23 already-persisted `custom.*` fills were mis-targeted (never published).
- **GID form (L1).** A category attribute (`shopify.coffee-roast`) is `list.metaobject_reference`
  and **rejects the canonical `TaxonomyValue` GID** (`INVALID_VALUE` — "Value require that you
  select a metaobject"). It needs a **per-shop metaobject-entry GID**.
- **Two scope walls.** The metaobject surface is invisible without `read_metaobjects` (L2 — even
  after assigning the category); channel/publication reads are denied without `read_publications`
  (L4), so whether the Catalog/Agentic channel exists on the store and `shops.plan` stay unknown
  under the current `write_products`+`read_products`.
- **0 of 13 coffee products carried a real category** — the precondition for any taxonomy write —
  and two "free native wins" surfaced: `coffee-product-form` (Ground/Whole) and
  `decaffeination-method` (product 117 = Swiss Water).

## Step 2d — the fix-type re-architecture (Gate M)

Redesigned routing to the channels the spike validated. Four panel decisions were taken up front:
the hardened append-list composer is kept (not a prose rewrite); GID resolution is **publish-time**
(the Optimizer stays Shopify-free, carrying the canonical `TaxonomyValue` GID); coverage is **two
channel-specific numbers**, never blended; the three-scope re-consent is **deferred to Step 4**,
where the resolver that needs it is built.

- **PRIMARY — description.** A grounded family with no structured pair gets an HTML-safe labeled
  Details line. Reaches all families, including the four with no taxonomy home.
- **SECONDARY — taxonomy attribute.** The four taxonomy-home families, gated on an assigned category
  AND a value that maps to a canonical taxonomy value (a region like "Yirgacheffe" is not a
  `Country`, so it falls to description).
- **NEW — `category` fix.** Assigns `fb-1-3-1` (or `fb-1-3-5` for concentrates), grounded from
  product data, approval-gated like a publish — the first link of the dependency chain.
- **`custom.*` retired.** New `taxonomy_map` holds the static value→`TaxonomyValue`-GID tables;
  `structured_families` now keys off the `shopify` namespace; `audits.structured_coverage` was
  dropped and `taxonomy_coverage` added (migration `c1f2a3b4d5e6`, drift-checked empty). Two new
  spec families added, `decaffeination-method` applicable only to decaf products.

## The silent-drop bug (run 746) and the Option-B fix

The first 2d acceptance (run 746) produced **zero** description fixes and, on product 113, the four
`unstructured` taxonomy-less families (`process`/`variety`/`altitude`/`tasting_notes`) produced **no
row at all** — not a fix, not a to-do. Trace: they grounded from `body_html`, are taxonomy-less (so
no metafield), and the composer's non-body gate skipped body-resident specs. The gate was a 2b
proxy for "already has a structured pair" that worked only while `custom.*` supplied that pair;
retiring `custom.*` broke the compensation.

Resolved as **Option B** — a labeled attribute-value pair beats a spec buried in prose, and the
composer must surface it. The gate was re-based from "grounded from a non-body source" to "did not
get a taxonomy metafield," so every grounded family without a structured pair is composed
(taxonomy-filled families excluded to avoid duplication). Added the **gap→row accounting invariant**
— an assertion in `run_optimizer` that every `spec_target` lands in exactly one of {taxonomy
metafield, description line, to-do} — the guard that would have caught the hole at the source.

## Evidence

- **Run 746 (buggy):** 113 → 2 taxonomy metafields + 2 absent-to-dos; the four unstructured
  taxonomy-less families dropped silently; 0 description fixes catalog-wide.
- **Run 839 (fixed):** completed → the accounting assertion held across all 18 discoverable
  products. 113 now emits a description fix with `Altitude / Process / Tasting Notes / Variety` as
  labeled lines (roast excluded — its taxonomy metafield is the pair). Description fixes on 5
  rich-prose products (113/117/118/120/130); category proposals on the 12 uncategorized coffees
  (119 → concentrate `fb-1-3-5`); **0 `custom.*`**. Product 113's category was made real honestly by
  re-ingesting shop 92 (its `fb-1-3-1` was assigned live during the spike), not hand-set.
- Tests: 266 passed, 2 skipped; ruff clean; Alembic drift empty.

Run 839 also surfaced a **false "absent" to-do** (`spec:origin` on 113, whose title *is* the
origin) — logged to `docs/backlog.md` → Optimizer as a blocker before Step 4.

## Where Phase 3 stands

- **Done:** per-product audit + three-state model (Gate G/M); write-target spike (2c); grounded
  Optimizer with taxonomy/description/category routing + gap→row invariant, seed retired (Gates
  H/L/M).
- **Remaining:** the false-to-do fix (blocker); Step 3 approval UI; Step 4 Publisher (per-shop
  metaobject-GID resolution + Admin API writes + the settled scope re-consent, dev store first).
