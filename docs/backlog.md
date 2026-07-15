# Backlog — known issues & deferred work

Durable ticket notes that outlive a single session. Not task lists (those live in the
PR/issue) and deliberately **not** in `CLAUDE.md`. Each entry says what, why it matters, and
the phase by which it should be revisited.

## Ingest

- **No prune path for deleted products.** Catalog ingest only inserts/updates (upsert on
  `uq_products_shop_shopify_id`); it never deletes. A product removed on the Shopify store
  leaves a stale `products` row behind indefinitely. Decide the reconciliation strategy
  (e.g. mark-and-sweep against the set of IDs seen in a full run, or handle `products/delete`
  webhooks) before catalog freshness matters downstream.
  _Raised: 2026-07-15 (Phase 1 closeout)._

- **`products.gtin` picks the first barcoded variant, not necessarily the primary one.**
  `extract_gtin` (`agent/app/services/catalog.py`) scans `variants_json` in order and returns
  the first variant that carries a barcode. So a product-level GTIN can be sourced from a
  **secondary** variant when the primary/default variant has no barcode — which may not be the
  product's canonical GTIN. (Observed: 14 barcoded variants across the catalog → 11 products
  with at least one, so 11 product-level GTINs; 9 barcode-free.) Decide the intended rule —
  primary/default variant, or first-barcoded as today — before **Phase 3 (Optimizer)**, which
  relies on product-level GTIN for grounding. Every variant's barcode is already preserved in
  `variants_json`, so this is a selection-rule change, not new data.
  _Raised: 2026-07-15 (Phase 1 closeout)._
