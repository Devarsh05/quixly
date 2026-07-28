"""Catalog normalisation shared by the ingest job, the products/update webhook and the Publisher."""

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_HTML_TAG = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str:
    """Replace every tag with a space. EXTRACTION/HASHING INPUT ONLY — never a written value."""
    return _HTML_TAG.sub(" ", text or "")


# Variant keys present in BOTH writer shapes — see ``stable_source_hash``.
_STABLE_VARIANT_KEYS = ("title", "sku", "barcode", "price")


def _field(source: Any, name: str) -> Any:
    """Read a column off either a ``Product`` row or a ``product_row_from_node`` dict."""
    return source.get(name) if isinstance(source, Mapping) else getattr(source, name, None)


def stable_source_hash(source: Any) -> str:
    """Hash a product's grounding sources through a WRITER-STABLE projection.

    **Why a projection and not the raw columns.** ``products`` has two writers that store the same
    column in different shapes: the GraphQL ingest job stores variants as
    ``{id,title,sku,barcode,price,inventoryQuantity}``, while the ``products/update`` webhook
    overwrites the column with the full REST variant object. Hashing the raw JSONB therefore
    produces a different digest for the same product depending on which writer touched it last —
    and every product the Publisher writes to gets touched by that webhook moments later, by our
    own hand. A live re-read could never reproduce a stored digest, so the staleness gate would
    refuse every write.

    So the projection keeps only what both writers spell identically: the scalar text columns, a
    per-variant ``title/sku/barcode/price`` subset, and ``namespace.key=value`` metafield strings.
    Variants and metafields are SORTED — collection order is not a source change.

    ``body`` is stripped of markup before hashing, for the same reason: Shopify may normalise the
    HTML it echoes back, and a whitespace-only reformat is not a change to what the Optimizer read.

    ``category`` is deliberately NOT hashed: assigning one is itself a fix, so including it would
    make a category publish stale the very sibling fixes queued behind it on the same product.

    Accepts a ``Product`` row or a ``product_row_from_node`` dict — the Optimizer hashes the stored
    row at propose time, the Publisher hashes a live re-read, and the two must agree.
    """
    variants = _field(source, "variants_json") or []
    metafields = _field(source, "metafields_json") or []

    projection = {
        "title": _field(source, "title") or "",
        "body": " ".join(strip_html(_field(source, "body")).split()),
        "product_type": _field(source, "product_type") or "",
        "variants": sorted(
            [str(v.get(key) or "") for key in _STABLE_VARIANT_KEYS]
            for v in variants
            if isinstance(v, dict)
        ),
        "metafields": sorted(
            f"{m.get('namespace')}.{m.get('key')}={m.get('value')}"
            for m in metafields
            if isinstance(m, dict)
        ),
    }
    canonical = json.dumps(projection, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_gtin(variants: list[dict[str, Any]]) -> str | None:
    """The first variant barcode, if any.

    GTIN is a *variant* field in Shopify; PRD §8 models it on the product. Every
    variant's barcode is retained in ``variants_json`` — this is a convenience column,
    not the source of truth. Works for both the GraphQL and REST-webhook variant shapes,
    which both spell it ``barcode``.
    """
    for variant in variants:
        if barcode := variant.get("barcode"):
            return barcode
    return None


# Canonical lowercase vocabulary. Shopify's GraphQL ProductStatus is UPPERCASE (ACTIVE/DRAFT/
# ARCHIVED/UNLISTED); the REST/webhook payload is lowercase. Upper-case the raw value before
# lookup so both spellings map here. ``unlisted`` is kept as its own state (NOT collapsed to
# active) — "not in search/collections/recommendations" is exactly the discoverability signal
# this product cares about.
_VISIBILITY_STATES = {
    "ACTIVE": "active",
    "DRAFT": "draft",
    "ARCHIVED": "archived",
    "UNLISTED": "unlisted",
}


def normalize_visibility_state(raw: str | None) -> str:
    """Map a Shopify ProductStatus (any case) to the lowercase canonical.

    Raises ValueError on an unrecognized value; callers decide how to react.
    """
    try:
        return _VISIBILITY_STATES[(raw or "").upper()]
    except KeyError:
        raise ValueError(f"Unmapped Shopify product status {raw!r}") from None


# Product class is derived from MERCHANT DATA ONLY — Shopify's productType (and, as a fallback,
# the Standard-taxonomy category) — never inferred by a model. The audit rubric is per-class, so
# this classification is deterministic (a keyword lookup, a data change to extend) and load-bearing
# for Gate G's determinism. The dev store labels beans "Coffee" and equipment "Brewing Gear";
# `category` is "Uncategorized"/None there, so productType is the signal.
#
# Equipment keywords are checked BEFORE coffee so "Coffee Grinder" classifies as equipment, not
# coffee. Matching is normalized-substring, so "Coffee Beans" → coffee and "Brewing Gear" → gear.
_EQUIPMENT_KEYWORDS = (
    "brewing gear", "gear", "equipment", "grinder", "kettle", "filter", "dripper", "brewer",
    "press", "scale", "machine", "accessor",
)
_COFFEE_KEYWORDS = ("coffee", "beans", "espresso", "roast", "decaf")

PRODUCT_CLASS_OTHER = "other"


def classify_product(product_type: str | None, category: str | None) -> str:
    """Classify a product as ``coffee`` / ``equipment`` / ``other`` from merchant fields.

    Deterministic keyword lookup over ``product_type`` (preferred) then ``category``. Returns
    ``other`` when neither carries a known signal — the rubric then skips spec scoring rather than
    guessing a vocabulary. ``Uncategorized`` and empty values carry no signal.
    """
    for source in (product_type, category):
        text = re.sub(r"[^\w\s]", " ", (source or "").casefold())
        if not text.strip() or "uncategorized" in text:
            continue
        if any(keyword in text for keyword in _EQUIPMENT_KEYWORDS):
            return "equipment"
        if any(keyword in text for keyword in _COFFEE_KEYWORDS):
            return "coffee"
    return PRODUCT_CLASS_OTHER


def product_row_from_node(node: dict[str, Any]) -> dict[str, Any]:
    """Map one Admin GraphQL product node to ``products`` column values.

    The SINGLE node→row mapping, shared by the ingest job (which adds ``shop_id`` / ``updated_at``
    and upserts) and the Publisher (which re-reads one product and needs the same column shape to
    hash and to refresh the row). Two copies of this mapping would drift, and the Publisher's
    staleness gate compares against exactly what ingest stored.

    ``visibility_state`` is deliberately left to the caller: ingest RAISES on an unmapped status
    while the webhook keeps the prior value, and that divergence is intentional.
    """
    variants = (node.get("variants") or {}).get("nodes") or []
    metafields = (node.get("metafields") or {}).get("nodes") or []
    category = node.get("category") or {}
    return {
        "shopify_product_id": node["id"],
        "title": node.get("title"),
        "body": node.get("descriptionHtml"),
        "variants_json": variants,
        "gtin": extract_gtin(variants),
        "metafields_json": metafields,
        "product_type": node.get("productType") or None,
        "category": category.get("fullName"),
    }
