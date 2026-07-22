"""Catalog normalisation shared by the ingest job and the products/update webhook."""

import re
from typing import Any


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
