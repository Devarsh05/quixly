"""Catalog normalisation helpers shared by the ingest job and the products/update webhook.

`normalize_visibility_state` is the single write-path normalizer: GraphQL ingest yields the
ProductStatus enum UPPERCASE, the REST/webhook payload yields it lowercase, and both must land
on the same lowercase canonical.
"""

import pytest

from app.services.catalog import normalize_visibility_state


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
