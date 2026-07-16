"""Catalog normalisation shared by the ingest job and the products/update webhook."""

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
