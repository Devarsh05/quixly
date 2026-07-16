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
