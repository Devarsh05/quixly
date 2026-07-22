"""Catalog normalisation helpers shared by the ingest job and the products/update webhook.

`normalize_visibility_state` is the single write-path normalizer: GraphQL ingest yields the
ProductStatus enum UPPERCASE, the REST/webhook payload yields it lowercase, and both must land
on the same lowercase canonical.
"""

import pytest

from app.services.catalog import classify_product, normalize_visibility_state


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ACTIVE", "active"),
        ("active", "active"),
        ("DRAFT", "draft"),
        ("draft", "draft"),
        ("ARCHIVED", "archived"),
        ("archived", "archived"),
        ("UNLISTED", "unlisted"),
        ("unlisted", "unlisted"),
    ],
)
def test_normalizes_both_cases_for_every_status(raw, expected):
    assert normalize_visibility_state(raw) == expected


@pytest.mark.parametrize("raw", ["SOMETHING_NEW", "", None])
def test_unknown_or_missing_value_raises(raw):
    with pytest.raises(ValueError):
        normalize_visibility_state(raw)


class TestClassifyProduct:
    """`classify_product` maps merchant productType/category → an internal product class.

    Deterministic lookup only (no inference). The dev store labels beans 'Coffee' and equipment
    'Brewing Gear'; `category` is 'Uncategorized'/None there, so productType drives the class.
    """

    @pytest.mark.parametrize("product_type", ["Coffee", "coffee", "COFFEE", "Coffee Beans"])
    def test_coffee_types_map_to_coffee(self, product_type):
        assert classify_product(product_type, None) == "coffee"

    @pytest.mark.parametrize(
        "product_type", ["Brewing Gear", "brewing gear", "Equipment", "Conical Burr Grinder"]
    )
    def test_equipment_types_map_to_equipment(self, product_type):
        assert classify_product(product_type, None) == "equipment"

    def test_equipment_keyword_wins_over_coffee_substring(self):
        # A "Coffee Grinder" is equipment, not coffee — equipment signal takes precedence.
        assert classify_product("Coffee Grinder", None) == "equipment"

    @pytest.mark.parametrize("product_type", [None, "", "Merchandise", "Gift Card"])
    def test_unmapped_or_missing_type_is_other(self, product_type):
        assert classify_product(product_type, None) == "other"

    def test_uncategorized_category_is_ignored_and_falls_back_to_type(self):
        assert classify_product("Coffee", "Uncategorized") == "coffee"

    def test_category_used_only_when_type_is_empty(self):
        assert classify_product(None, "Food, Beverages & Tobacco > Beverages > Coffee") == "coffee"
