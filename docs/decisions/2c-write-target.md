# Phase 3 · Step 2c — write-target verification spike

**Status:** decision doc, no code changes. **Date:** 2026-07-24. **API version pinned:** 2026-07.
**Taxonomy releases consulted:** published `v2026-05` (matches our pin window) cross-checked against
`main`/`2026-08-unstable`; the coffee attribute set and values below are **identical** across both.

> **The question.** The Optimizer writes `custom.roast_level` (`optimizer.py:255`, namespace
> `custom`, type `single_line_text_field`). Is that key readable by the surface we optimize for
> (Shopify Catalog → agentic storefronts)? Every finding below cites a doc URL or a live data file;
> anything I could not verify is named **UNRESOLVED**, not guessed.

---

## TL;DR

- The machine-readable channel Shopify itself feeds to agentic surfaces is the **taxonomy category
  metafield** in the reserved **`shopify`** namespace — **not** `custom.*`. Our current
  `custom.roast_level` is a valid, writable metafield, but it is **not the canonical taxonomy
  channel**, and it is **mis-targeted** for the AI-legibility goal.
- **3 of our 7 families have a native taxonomy attribute** (`roast_level`, `origin`, partially
  `tasting_notes`); **4 do not** (`process`, `variety`, `altitude`, `brew_method`).
- **Every taxonomy write is blocked by an unmet precondition: a product category must be assigned
  first, and 0 of our 13 coffee products have one.** So **"assign product category" must become a
  new fix type** — it is the gate on the entire taxonomy path.
- **Two items are genuinely UNRESOLVED and need a live API call** (our offline token is expired —
  see §4): (a) whether writing a category metafield takes the canonical `TaxonomyValue` GID or a
  per-shop metaobject-entry GID; (b) whether arbitrary `custom.*` metafields reach Shopify Catalog
  at all. **Do not model past these without resolving them.**

---

## 1. Taxonomy coverage — the category and its attributes

**Category for whole-bean / ground coffee:** `Food, Beverages & Tobacco > Beverages > Coffee >
Coffee Beans & Ground Coffee` — `gid://shopify/TaxonomyCategory/fb-1-3-1`.
(Coffee concentrates = `fb-1-3-5`; that is product 119 "Cold Brew Concentrate".)
Source: [`dist/en/categories.txt`](https://raw.githubusercontent.com/Shopify/product-taxonomy/main/dist/en/categories.txt).

**The 12 category metafields (product attributes) `fb-1-3-1` unlocks**, from
[`dist/en/taxonomy.json`](https://raw.githubusercontent.com/Shopify/product-taxonomy/main/dist/en/taxonomy.json)
(all 12 confirmed present in published `v2026-05`
[`dist/en/attributes.txt`](https://raw.githubusercontent.com/Shopify/product-taxonomy/v2026-05/dist/en/attributes.txt)):

| Attribute | handle | id | kind | # values |
|---|---|---|---|---|
| Coffee bean species | `coffee-bean-species` | 7698 | choice list | 6 (Arabica, Blend, Excelsa, Liberica, Robusta, Other) |
| **Coffee product form** | `coffee-product-form` | 1977 | choice list | 3 (Ground, Whole bean, Other) |
| **Coffee roast** | `coffee-roast` | 1477 | choice list | 6 (Light, Medium, Dark, Medium-light, Medium-dark, Other) |
| **Country** | `country` | 2364 | choice list | 238 (all ISO countries) |
| Decaffeination method | `decaffeination-method` | 7731 | choice list | 6 (Swiss Water, Carbon Dioxide, Ethyl Acetate, Methylene Chloride, Mountain Water, Other) |
| Flavor | `flavor` | 1458 | choice list | 30 (Almond, Caramel, Chocolate, … Vanilla, Other) |
| Grind size | `grind-size` | 3416 | choice list | 8 (Coarse … Extra fine, Other) |
| Caffeine content | `caffeine-content` | — | — | — |
| Allergen information | `allergen-information` | — | — | — |
| Dietary preferences | `dietary-preferences` | — | — | — |
| Dietary supplements | `dietary-supplements` | — | — | — |
| Food product form | `food-product-form` | — | — | — |

### The 7-family × target-mechanism mapping

| Our `SPEC_FAMILY` | Native taxonomy attribute? | Fit | Evidence |
|---|---|---|---|
| **roast_level** | **YES — `coffee-roast`** | **Exact.** Our detected `"Medium-Light"` = value `gid://shopify/TaxonomyValue/7459` ("Medium-light"). All our roast terms are members. | attribute id 1477; value 7459 confirmed in `v2026-05` `attribute_values.txt` |
| **origin** | **YES — `country`** | **Good, with a caveat.** All 10 of our origins (Ethiopia, Colombia, Kenya, Guatemala, Brazil, Costa Rica, Panama, Indonesia, Peru, Rwanda) are valid `Country` values. Caveat: taxonomy models **country**, not coffee *region/washing-station* — "Yirgacheffe"/"Huila" collapse to Ethiopia/Colombia. | attribute id 2364; 10/10 membership checked against the 238-value list |
| **tasting_notes** | **PARTIAL — `flavor`** | **Poor fit.** `Flavor` is a **closed** 30-value list of generic flavors (Almond, Caramel, Chocolate, Vanilla…). Our specialty notes — bergamot, jasmine, stone fruit, blackcurrant — are **not members.** "Chocolate"/"Caramel"/"Cherry" would map; the rest would not. | attribute id 1458; full value list pulled |
| **process** | **NO** | washed / natural / honey / anaerobic have **no** taxonomy attribute. | absent from the 12 |
| **variety** | **NO** | Gesha / Bourbon / Typica / SL28 have no attribute. `coffee-bean-species` is the wrong granularity (Arabica vs Robusta, not cultivar). | absent from the 12 |
| **altitude** | **NO** | no elevation/altitude attribute exists. | absent from the 12 |
| **brew_method** | **NO** | pour over / espresso / cold brew has no attribute. `grind-size` is adjacent but semantically different (grind ≠ brew). | absent from the 12 |

**Also addressable but not currently targeted:** `coffee-product-form` (Ground / Whole bean — a
strong, universally-applicable signal) and `decaffeination-method` (product 117 is "Decaf Swiss
Water Process" → `Swiss Water` = a native value). These are latent wins the current 7-family set
misses.

---

## 2. Write mechanics

**Namespace / key / type.** Category metafields live in the **reserved `shopify` namespace**, keyed
by the attribute handle (e.g. `shopify.coffee-roast`), and choice-list attributes are stored as
**`list.metaobject_reference`** — the value is a JSON array of metaobject-entry GIDs, e.g.
`["gid://shopify/Metaobject/123"]`.
Sources: [category metafields](https://help.shopify.com/en/manual/custom-data/metafields/category-metafields);
[list of data types](https://shopify.dev/docs/apps/build/metafields/list-of-data-types);
[standard metaobject definitions](https://shopify.dev/docs/apps/build/custom-data/metaobjects/list-of-standard-definitions)
("The Shopify standard taxonomy is a suite of standard metaobject definitions … used to store and
manage product taxonomy categories and attributes.").

**How an app obtains the entry GID for "Medium-Light".** The taxonomy publishes a **canonical
`TaxonomyValue` GID** per value — `Medium-light` = `gid://shopify/TaxonomyValue/7459`
([`v2026-05` `attribute_values.txt`](https://raw.githubusercontent.com/Shopify/product-taxonomy/v2026-05/dist/en/attribute_values.txt)).
The category metafield, however, stores a **metaobject-entry** reference (`shopify--…` standard
definition), whose GID is what `metafieldsSet`/`productSet` actually persists.
**UNRESOLVED (needs a live call):** whether the Admin API accepts the canonical `TaxonomyValue` GID
directly, or whether the app must resolve a **per-shop metaobject-entry GID** from the `shopify--`
definition first. Shopify docs confirm the standard-taxonomy metaobjects exist and are Shopify-seeded,
but I could **not** find a doc stating which GID form `metafieldsSet` takes for a category metafield,
and I could not test it live (token expired, §4). Recorded as unresolved, not inferred.

**Scope / mutation.** `metafieldsSet` "requires the same access level needed to mutate the owner
resource" — for a product metafield that is product-write, i.e. our **`write_products`** scope
([metafieldsSet](https://shopify.dev/docs/api/admin-graphql/latest/mutations/metafieldsSet)). No
doc names an extra scope for the `shopify` namespace, and standard-definition values are documented
as "readable and writable across apps." So writing a category metafield is **in-scope at 2026-07**,
**conditional** on the GID-form question above.

**Documented limitation — confirmed.** "Custom attributes are currently not supported" for the
Standard Product Taxonomy
([add category metafields](https://help.shopify.com/en/manual/custom-data/metafields/category-metafields/add-category-metafields)).
Nuance that matters for us: you **can** add custom *entries* (a new value like a specialty roast)
to an existing attribute via "Add new entry," but you **cannot** invent a new *attribute* (so there
will never be a taxonomy `process`/`variety`/`altitude`/`brew_method` we can create ourselves).

---

## 3. What Shopify Catalog actually consumes

**Shopify Catalog Mapping exposes exactly three mappable fields: product title, product
description, product category** — each sourceable "from product attributes, product metafields, or
metaobject references."
Source: [Mapping your product data sources for Shopify Catalog](https://help.shopify.com/en/manual/promoting-marketing/seo/shopify-catalog/default-listing).

Catalog syndicates products "with their title, description, options, images, price, availability,
and **other key attributes**"
([products for agentic storefronts](https://help.shopify.com/en/manual/online-sales-channels/agentic-storefronts/products)).
"Other key attributes" is not enumerated.

**UNRESOLVED (do not infer):** whether arbitrary `custom.*`-namespace metafields reach Shopify
Catalog. What the docs **do** establish:
1. `custom.roast_level` is **not** one of the three mapped fields (title/description/category), so it
   is not surfaced *through Catalog Mapping*.
2. Category (taxonomy) attributes are the structured product data Shopify's own taxonomy exists to
   feed to marketplaces and discovery surfaces.
The docs neither confirm nor deny that a stray `custom.*` metafield is otherwise ingested. Marked
**UNRESOLVED** — this is the single most important open question, because it decides whether the 23
already-persisted `custom.*` fills have *any* AI-legibility value or none.

---

## 4. Live store check — BLOCKED, and why

**The dev store's offline token is expired.** `shopify.Session` row
`offline_quixly-ljymkoyb.myshopify.com`: **expired 2026-07-22 23:22 UTC — ~1 day 17 h ago**, scope
`write_products`. Per CLAUDE.md token custody, `app/lib/admin-token.server.ts` is the **single
refresh authority** and refresh is serialized by a per-shop advisory lock; the agent stores no token
and **must not** open a second refresh path. The app shell is not running (ports 3000/3001/8000 all
closed). So a live Admin API query for the product's assigned category, its `shopify`-namespace
metafields, and channel availability **could not be run without standing up the app shell and
completing an interactive re-auth**, which is out of scope for a read-only spike.

**What I could establish without the live API:**
- **Category state** comes from *our own ingest*, which already queries the taxonomy field
  (`shopify_admin.py:33` — `category { name fullName }`). See §5.
- **Agentic / Shopify Catalog availability on this plan: UNRESOLVED.** `shops.plan` is **NULL**
  (never populated), so I cannot confirm the plan from stored data. Shopify's own docs state agentic
  storefronts (Copilot, Google AI Mode, Gemini) are **early access and "not yet available for all
  stores"**
  ([requirements](https://help.shopify.com/en/manual/online-sales-channels/agentic-storefronts/requirements)),
  and ChatGPT requires Catalog eligibility + US buyers. **It is entirely possible the Agentic /
  Catalog channel does not exist on our dev store at all** — which, stated plainly, **changes how
  Phase 4 can verify uplift**: if the channel isn't provisioned, there is no first-party Catalog
  surface to read our writes back from, and Phase 4's uplift claim must fall back to the
  engine-query panel (Perplexity et al.), not a Shopify-Catalog round-trip.

> **Action to close §4:** when the app shell is next running with a fresh token, run one Admin
> GraphQL query for product 113: `category { id fullName }`, `metafields(namespace:"shopify")`, and
> the available `publications`/channels — plus one `metafieldsSet` write of `shopify.coffee-roast`
> with the `TaxonomyValue/7459` GID to settle the §2 GID-form question empirically.

---

## 5. Our products' category state

From the live DB (populated by our own taxonomy-aware ingest):

**0 of 13 coffee products have a real Standard Product Taxonomy category.** 5 are the literal
`"Uncategorized"`; 8 are `NULL`.

`"Uncategorized"` is itself a taxonomy node — `gid://shopify/TaxonomyCategory/na` — and it unlocks
**0 attributes** (verified in `taxonomy.json`: `level 0`, `attributes: []`). So it is functionally
identical to NULL for our purposes: **no category metafield can exist on any of these products.**

| category value | count |
|---|---|
| `NULL` | 8 |
| `Uncategorized` (`…/na`, 0 attributes) | 5 |
| a real coffee category (`fb-1-3-1`) | **0** |

**Consequence:** "assign the category" is a **precondition** the merchant/app must satisfy before a
single category metafield is writable — and it is a fix type we do not currently have.

---

## Decision — recommended write target per family

| Family | RECOMMENDED write target | Why |
|---|---|---|
| **roast_level** | **Taxonomy attribute `shopify.coffee-roast`** (after category assigned) | Exact closed-list match incl. Medium-light; this is the AI-legible channel. |
| **origin** | **Taxonomy attribute `shopify.country`** (after category assigned) | All our origins are members. Keep a `custom.*` region/lot field in parallel for the sub-country detail taxonomy drops. |
| **tasting_notes** | **Description restructure** (+ `shopify.flavor` only for the ~handful that match) | `Flavor` is a closed 30-value list that excludes specialty notes; forcing it would fabricate/flatten. Prose is the honest home; map the rare exact hits (chocolate, caramel) as a bonus. |
| **process** | **Custom metafield** (`custom.process`) + description | No taxonomy attribute exists and none can be created. Custom is the only structured home. |
| **variety** | **Custom metafield** (`custom.variety`) + description | Same — no taxonomy attribute; `coffee-bean-species` is the wrong granularity. |
| **altitude** | **Custom metafield** (`custom.altitude`) + description | No taxonomy attribute. |
| **brew_method** | **Custom metafield** (`custom.brew_method`) + description | No taxonomy attribute; `grind-size` is not equivalent. |
| _(new)_ **coffee_product_form** | **Taxonomy attribute `shopify.coffee-product-form`** | Ground/Whole bean — strong universal signal we don't yet capture. Consider adding to `SPEC_FAMILIES`. |
| _(new)_ **decaffeination_method** | **Taxonomy attribute `shopify.decaffeination-method`** | Product 117 is a native `Swiss Water` value. |

**Is the current `custom.roast_level` target correct, wrong, or unresolved?**
**Mis-targeted (effectively wrong for the AI-legibility goal), with one dependency UNRESOLVED.**
It writes successfully and is not harmful, but the canonical machine-readable channel for roast is
the `shopify.coffee-roast` category metafield, and `custom.roast_level` is not that. Whether the
`custom.*` write has *residual* legibility value hinges on the UNRESOLVED §3 question (does `custom.*`
reach Catalog at all). Either way, roast should be written to the taxonomy attribute, and `custom`
reserved for the four families that have no taxonomy home.

**Size of the change if we re-target:**
- **`optimizer.py`:** **not a one-liner.** `_metafield_object` (lines ~134–140) hardcodes
  `namespace="custom"`, `key=attribute`, `type="single_line_text_field"`. Taxonomy families need
  `namespace="shopify"`, `key=<handle>`, `type="list.metaobject_reference"`, and a **new
  value→GID resolver** step (the §2 unresolved GID-form work). Realistically: a per-family
  target-mechanism table (taxonomy vs custom vs description), a GID-resolution helper, and gating on
  "category assigned." Medium change, and it **should wait** until §2 and §3 are resolved live.
- **Already-persisted fix rows:** **23 `custom.*` metafield fixes across 4 products**, all
  `status = proposed`, **never published** (no Publisher until Step 4). These are evidence/demo rows
  under a `run_id` — **regenerate, don't migrate.** Trivial cost: `DELETE … WHERE run_id = …` and
  re-run once the target logic lands. No production write was ever made.

**Does "assign product category" need to become a new fix type?** **YES.** It is the hard
precondition for every taxonomy write, 0/13 products satisfy it, and it is neither a metafield fix
nor a description fix nor a to-do in today's model. It is a first-class, app-writable action
(`productUpdate`/`productSet` with `category`) that must gate the taxonomy fixes behind it.

---

## UNRESOLVED — named, not guessed

1. **GID form for a category-metafield write (§2).** Does `metafieldsSet`/`productSet` accept the
   canonical `TaxonomyValue` GID (`…/7459`), or must the app resolve a per-shop metaobject-entry GID
   from the `shopify--` standard definition? **Blocks** the Publisher for taxonomy families. Resolve
   with one live write.
2. **Does `custom.*` reach Shopify Catalog at all (§3)?** Docs cover only title/description/category
   mapping and an un-enumerated "other key attributes." **Blocks** the claim that our 23 existing
   `custom.*` fills have any legibility value. Resolve via docs escalation or a live Catalog read.
3. **Is the Agentic / Shopify Catalog channel provisioned on our dev store, and on what plan (§4)?**
   `shops.plan` is NULL; channel is early-access. **Blocks/relocates** Phase 4's uplift verification
   method. Resolve with a live channel/publications query once the app shell is up.
4. **Live category + `shopify`-namespace metafield state of product 113 (§4).** Could not be read —
   token expired, and re-auth must go through the app shell's single refresh authority.

**No code was changed in this step.** `optimizer.py` is untouched.

---

# Step 2c-live — RESOLVED / STILL-UNRESOLVED (2026-07-24)

Live spike against `quixly-ljymkoyb.myshopify.com`, Admin API **2026-07**, granted scopes
**`write_products` + `read_products`** (see §L0). No code changed. Two live calls run; raw
responses below.

## L0. Token custody — the chain is HEALTHY (not reauth_required)

§4 recorded the offline token as expired and the live check as BLOCKED. Re-checked live: the
access token is expired (normal — offline tokens expire ~60 min) but the **refresh chain is
alive** (`refreshToken` present, `refreshTokenExpires = 2026-10-20`). Minted a fresh token
through the single refresh authority (app shell `POST /internal/shops/:shop/admin-token`, no
second refresh path):

```
HTTP 200
{"access_token":"shpua_…","expires_at":"2026-07-24T23:54:32.443Z"}
```

So §4's "BLOCKED — token expired" is superseded: the block was only that the app shell wasn't
running. **Not a reauth_required finding.**

**Granted scopes (load-bearing for everything below):**
```
{"data":{"currentAppInstallation":{"accessScopes":[
  {"handle":"write_products"},{"handle":"read_products"}]}}}
```
No `read_publications`, no `read_metaobjects`. This single fact explains every gap below.

## L1. GID form for a category-metafield write — **RESOLVED**

Answers UNRESOLVED #1 (§2). **The canonical `TaxonomyValue` GID is REJECTED; a per-shop
metaobject-entry GID is required.**

**Category assign succeeded** (`productUpdate`, modern `product:` arg + scalar
`TaxonomyCategory` GID — no metaobject involved for the category itself):
```
INPUT:  mutation { productUpdate(product: {
          id: "gid://shopify/Product/15436808192371",
          category: "gid://shopify/TaxonomyCategory/fb-1-3-1" }) {
          product { id category { id fullName } } userErrors { field message } } }
OK:     {"productUpdate":{"product":{"category":{
          "id":"gid://shopify/TaxonomyCategory/fb-1-3-1",
          "fullName":"Food, Beverages & Tobacco > Beverages > Coffee > Coffee Beans & Ground Coffee"}},
          "userErrors":[]}}
```

**Roast metafield with the canonical `TaxonomyValue` GID FAILED** — this settles the GID form:
```
INPUT:  metafieldsSet metafields:[{ ownerId:"gid://shopify/Product/15436808192371",
          namespace:"shopify", key:"coffee-roast", type:"list.metaobject_reference",
          value:"[\"gid://shopify/TaxonomyValue/7459\"]" }]
FAIL:   {"metafieldsSet":{"metafields":[],"userErrors":[{
          "field":["metafields","0","value"],
          "message":"Value require that you select a metaobject.","code":"INVALID_VALUE"}]}}
```
The server enforces `list.metaobject_reference` and demands a **Metaobject** GID, not the
`TaxonomyValue/7459` GID the taxonomy files publish. **So the Publisher must resolve a per-shop
metaobject-entry GID before writing a taxonomy attribute.**

## L2. …but resolving that metaobject GID is BLOCKED under current scope — **NEW BLOCKER**

A successful roast write could **not** be completed, because every path to the metaobject-entry
GID returns empty/null with `write_products`+`read_products` only (no `read_metaobjects`):

```
metaobjectDefinitions(first:100)                    -> {"edges":[]}
metaobjects(type:"shopify--coffee-roast", first:10) -> {"edges":[]}   (also "coffee-roast",
                                                        "shopify--2024-07--coffee-roast": [])
metaobjectDefinitionByType("shopify--coffee-roast") -> null
metafieldDefinitions(ownerType:PRODUCT,ns:"shopify")-> {"edges":[]}
```
No `ACCESS_DENIED` was returned — the reads are silently filtered to the (zero) definitions this
scope can see. The standard-taxonomy metaobject definitions exist server-side (the L1 error
proves it) but are not exposed to us. **Consequence for the Publisher:** taxonomy-family writes
need (a) an added scope — `read_metaobjects` — and (b) a value→metaobject-entry-GID resolver
step, before a single `shopify.coffee-roast` (or `country`, etc.) write is possible. This is a
concrete addition to the "medium change" estimate in the Decision section.

## L3. Product 113 catalog state — **RESOLVED (read)**

Baseline read **before** the L1 category assign:
```
{"product":{"id":"gid://shopify/Product/15436808192371",
  "title":"Ethiopia Yirgacheffe 340 g","status":"ACTIVE",
  "category":{"id":"gid://shopify/TaxonomyCategory/na","fullName":"Uncategorized"},
  "metafields":{"edges":[]}}}
```
- **Assigned category:** was `Uncategorized` (`…/na`); now `fb-1-3-1` after L1 (the spike left it
  assigned — it is the correct category for this product; §5's count is now 1/13, not 0/13).
- **All `shopify`-namespace metafields:** **none.** Zero metafields of any namespace —
  confirms the 23 `custom.*` optimizer fills were **never published** (nothing on the product).

## L4. Channel / publications reality — **CANNOT be read (scope), NOT proof of absence**

Answers UNRESOLVED #3 only partially, and reframes it. **We cannot enumerate channels at all**
under current scope:
```
publications(first:40)          -> errors: ACCESS_DENIED "Required access: `read_publications`"
product{ resourcePublications } -> errors: ACCESS_DENIED "Required access: `read_publications`"
product{ resourcePublicationsCount } -> ACCESS_DENIED (same)
```
So whether a **Shopify Catalog / Agentic sales channel exists on this dev store is UNKNOWN** —
we lack `read_publications` to see any publication. `shops.plan` stays NULL and is
**not resolvable via this scope.** This is a scope gap, not evidence the channel is absent.

> **LOUD FLAG — plan-level consequence for Phase 4.** To learn whether a first-party Catalog
> round-trip is even possible (read our writes back from the channel Shopify feeds to agentic
> surfaces), the app must **add `read_publications`** and re-query, and the channel must be
> provisioned on the store. Until then, **Phase 4 uplift cannot assume a Shopify-Catalog
> round-trip** and must be able to fall back to the engine-query panel (Perplexity et al.).
> Do not design Phase 4 verification around a Catalog read that our scopes can't perform.

## L5. Does `custom.*` reach Shopify Catalog? — **STILL-UNRESOLVED (not inferable live)**

UNRESOLVED #2 is **not** answerable from Call 2's data. We cannot even enumerate the channel
(L4), let alone observe what it ingests. Determining whether `custom.*` (vs. only
`shopify`-namespace + the three mapped fields title/description/category) is consumed requires a
**live agentic Catalog query** against a provisioned channel — which this store's scope does not
permit us to reach. **Per the task: this cannot be determined without a live agentic query — so
it is left UNRESOLVED, not inferred.**

## Net for re-planning

- **GID form: RESOLVED** — metaobject-entry GID, not `TaxonomyValue`. (UNRESOLVED #1 closed.)
- **But** taxonomy writes are **gated on `read_metaobjects` + a metaobject resolver** the app
  doesn't yet have (L2) — a real add to the Optimizer/Publisher scope of work.
- **Channel existence: UNKNOWN** — blocked by missing `read_publications`, not proven absent
  (L4). Re-plan Phase 4 to not depend on a Catalog round-trip until scope + provisioning are
  confirmed.
- **`custom.*`→Catalog: STILL-UNRESOLVED** (L5), and now known to need a live agentic query, not
  a doc/API read.
- **Scope decision needed before the Optimizer re-plan:** the app currently requests only
  `write_products`. Taxonomy legibility (the whole point of 2c) needs at minimum
  `read_metaobjects`; verifying it needs `read_publications`. Both are re-consent (merchant
  re-auth) events — decide the final scope set **before** building the taxonomy write path.

**No code was changed in this step (2c-live).** Live writes were made to the dev store only:
product 113 category set to `fb-1-3-1` (intended by the spike); one rejected `metafieldsSet`
attempt wrote nothing.

---

# Step 2c-live-followup — does categorizing expose the roast metaobjects? (2026-07-24)

**Question (L2 follow-up):** product 113 is now on `fb-1-3-1`. Does having the category assigned
cause Shopify to expose the roast metaobject entries that were empty pre-category — making the
resolver a plain `read_products` lookup rather than needing `read_metaobjects`? Read-only, same
single refresh authority, no write, no scope change.

**Answer: NO — still empty/null across every path. `read_metaobjects` is confirmed-required.**
Raw responses, all against the now-categorized product:

```
metaobjectDefinitions(first:250)                     -> {"edges":[]}
metaobjectDefinitionByType("shopify--coffee-roast")  -> null
metaobjects(type:"shopify--coffee-roast", first:20)  -> {"edges":[]}
metafieldDefinitions(ownerType:PRODUCT, ns:"shopify")-> {"edges":[]}
product(113){ category }                             -> fb-1-3-1  (confirmed still assigned)
product(113){ metafields(namespace:"shopify") }      -> {"edges":[]}
product(113){ metafield(ns:"shopify", key:"coffee-roast") } -> null
```

**Conclusion.** Category assignment does **not** surface the standard-taxonomy metaobject
definitions or entries under `write_products`+`read_products`. The value→metaobject-entry
resolver is therefore **gated on adding `read_metaobjects`** — it is not a post-category lookup
available at the current scope. This closes the L2 open question: the taxonomy write path needs
the `read_metaobjects` scope (a re-consent/merchant re-auth event), plus the resolver step, in
addition to the `metafieldsSet` mechanics already settled in L1.

**No code, no writes in this follow-up.** Reads only.

---

# Step 4-preamble — scope granted, resolver probed, channel resolved (2026-07-27)

Scope change shipped (`app/shopify.app.toml`: `write_products` → `write_products,
read_metaobjects, read_publications`), merchant re-consent completed on
`quixly-ljymkoyb.myshopify.com`, then a **read-only throwaway probe** (uncommitted, deleted after
the run) via `ShopifyAdminClient.execute()` pulling its token through the app-shell single refresh
authority. Admin API **2026-07**. **No writes of any kind.**

## L6-gate. The new scopes ARE on the live token — **CONFIRMED**

```
{ currentAppInstallation { accessScopes { handle } } }
->
{"currentAppInstallation":{"accessScopes":[
  {"handle":"read_metaobjects"},{"handle":"read_publications"},
  {"handle":"write_products"},{"handle":"read_products"}]}}
```
The toml declaration reached the token, not just the config. (`read_products` is implied by
`write_products` and is not declared.)

## L6. Resolver contract — **STILL BLOCKED. The settled scope set is WRONG.**

**The `read_metaobjects` grant did NOT unblock the taxonomy metaobject surface.** Every path is
still empty *with the scope granted* — identical to the pre-grant L2/followup results:

```
metaobjectDefinitions(first:250)                        -> {"edges":[]}
metaobjectDefinitionByType(<any of 8 spellings>)         -> null
metaobjects(type:<any of 8 spellings>, first:5)          -> []
metaobjectByHandle(type:…, handle:"light"/"Light")       -> null
metafieldDefinitions(ownerType:PRODUCT, ns:"shopify")    -> {"nodes":[]}
```
Spellings tried: `shopify--coffee-roast`, `coffee-roast`, `shopify--2024-07--coffee-roast`,
`shopify--2026-05--coffee-roast`, `shopify--CoffeeRoast`, `shopify.coffee-roast`, `coffee_roast`,
`shopify--coffee_roast`, plus the `product_taxonomy_*` convention (`product_taxonomy_coffee_roast`,
`product_taxonomy_country`, `product_taxonomy_coffee_product_form`,
`product_taxonomy_decaffeination_method`, `product_taxonomy_material`, `product_taxonomy_flavor`)
— **all 0 entries.** No `ACCESS_DENIED`; the reads are silently filtered, exactly as at L2.

**ROOT CAUSE — a scope we did not know about.** `metaobjectDefinitions` requires
**`read_metaobject_definitions`**, which is a **distinct scope from `read_metaobjects`**:
> `read_metaobject_definitions` / `write_metaobject_definitions` grant access to
> `MetaobjectDefinition`; `read_metaobjects` / `write_metaobjects` grant access to `Metaobject`.
> — [access scopes](https://shopify.dev/docs/api/usage/access-scopes)

We granted only the **instance-level** scope. The **schema-level** scope is what exposes the
definitions — and without enumerable definitions we cannot even *discover* the correct `type`
string, so the `metaobjects` query (which we DO have scope for) cannot be aimed. The two failures
compound: no definitions → no type name → no entries.

Also note `standardMetaobjectDefinitionTemplates` **does not exist on `QueryRoot` at 2026-07**
(`undefinedField`), despite appearing in current docs — so the documented
"enable a standard definition" route is not available at our pinned version.

**Consequence:** the §5 resolver contract is **NOT confirmed live, and NOT refuted** — it remains
untested, now blocked on a *second, previously-unidentified* scope. **The "settled scope set" in
CLAUDE.md is incomplete.**

### What IS readable — the canonical layer, and it validates our static map

The `taxonomy` root query works fully at current scope and confirms **every GID baked into
`agent/app/services/taxonomy_map.py`**:

```graphql
{ taxonomy { categories(first:1, search:"Coffee Beans & Ground Coffee") { nodes {
    id fullName
    attributes(first:30) { nodes { __typename
      ... on TaxonomyChoiceListAttribute { id name values(first:250) { nodes { id name } } } } } } } } }
```

| Attribute (`TaxonomyAttribute`) | value | canonical `TaxonomyValue` GID | in our map? |
|---|---|---|---|
| Coffee roast (1477) | Light | `…/19313` | ✓ (113's roast) |
| Coffee roast (1477) | Medium-light | `…/7459` | ✓ (spike L1's reference) |
| Coffee roast (1477) | Medium / Medium-dark / Dark / Other | `…/19314` / `…/9422` / `…/19315` / `…/26642` | ✓ |
| Country (2364, 250 values) | Ethiopia | `…/8882` | ✓ |
| Coffee product form (1977) | Whole bean | `…/8285` | — |
| Decaffeination method (7731) | Swiss Water | `…/53270` | — |

So the **canonical value→GID half is live-validated**; only the canonical→**metaobject-entry**
hop remains unproven. Product 113 still has **zero** `shopify`-namespace metafields (no writes).

## L7. Channel reality — **RESOLVED: the agentic channel EXISTS and 113 is published to it**

Answers UNRESOLVED #3, and reverses L4's "UNKNOWN".

```
shop { plan } -> {"displayName":"Basic App Development",
                  "partnerDevelopment":true,"shopifyPlus":false}
```
`shops.plan` is **reported only, deliberately not persisted** (the column stays NULL; its owner is
the connect path — see `docs/backlog.md`).

**Product 113 is published to Microsoft Copilot — an agentic surface:**
```
product(113) { resourcePublications } ->
  Online Store       (Publication/350800085363)  isPublished:true
  Microsoft Copilot  (Publication/350800118131)  isPublished:true
  Point of Sale      (Publication/350800183667)  isPublished:true
  Shop               (Publication/350800150899)  isPublished:true

publication(id:"gid://shopify/Publication/350800118131") ->
  {"name":"Microsoft Copilot","autoPublish":true,
   "catalog":{"id":"gid://shopify/AppCatalog/185356321139","status":"ACTIVE",
              "__typename":"AppCatalog"}}
```

> **TRAP — the agentic channel is NOT enumerable.** `publications(first:40)` returns **3** (Online
> Store, Shop, Point of Sale) and **omits Microsoft Copilot**, under *every* `catalogType`
> (`APP`/`NONE`/`MARKET`/`COMPANY_LOCATION`). `publicationsCount` = **3**; `catalogs(first:40)`
> likewise omits the Copilot `AppCatalog` while `catalogsCount` = **4**. The publication resolves
> perfectly by **direct ID** and appears in **`product.resourcePublications`** — it is simply
> excluded from the collection queries. **Any code that discovers channels by enumerating
> `publications` will conclude the agentic channel does not exist.** Go through
> `product.resourcePublications`.
>
> (`resourcePublicationsCount` reported **3** while `resourcePublications` returned **4** nodes —
> the count field is also not counting the agentic publication. Do not gate on that count.)

**Plan-level consequence for Phase 4 — the L4 flag is now resolved in the POSITIVE direction:**
a first-party Catalog/agentic round-trip **is** possible on this store — product 113 is live on
Microsoft Copilot with `autoPublish: true`. Phase 4 uplift verification **may** use a
publication-status round-trip, and is no longer forced onto the engine-query panel alone. It must
still read that state per-product via `resourcePublications`, never by enumerating publications.

## L8. Does `custom.*` reach Catalog? — **STILL UNRESOLVED; needs a live agentic query**

Enumerating publications yields **no signal** about which product fields the channel ingests: the
publication/catalog objects expose identity and publish-state only, not field mapping. Confirming
whether a non-mapped `custom.*` metafield reaches Catalog still requires **a live agentic query
against the provisioned channel** — it is not inferable from the Admin API. Left **UNRESOLVED, not
inferred**, exactly as at L5. Largely moot: `custom.*` is retired as a write target.

## Verdict — what this means for Step 4's Publisher

**The §5 resolver contract is NOT confirmed live. It needs adjustment before Step 4 can build on
it**, and the blocker is a scope, not a query shape:

1. **The settled scope set is incomplete.** Add **`read_metaobject_definitions`** —
   `read_metaobjects` alone is provably insufficient (this run). That is a **second merchant
   re-consent**, and it is the precondition for re-probing the resolver.
2. Only after that can the canonical→metaobject-entry hop be proven or refuted. If it still comes
   back empty, the fallback question becomes whether the standard-taxonomy metaobjects must be
   *enabled* on the shop first — and `standardMetaobjectDefinitionTemplates` is absent at 2026-07,
   so that route would need an API-version decision.
3. **Unblocked regardless:** the canonical value→`TaxonomyValue` GID map in `taxonomy_map.py` is
   live-validated, and the `category` fix type (`productUpdate` with a scalar `TaxonomyCategory`
   GID) was already proven writable at L1 — it needs no metaobject resolution.

**No code changed. No writes.** The probe script was deleted after this record was written.
