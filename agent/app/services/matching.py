"""Brand normalization + alias matching — the single home for this logic.

Per CLAUDE.md's "one normalizer" convention, brand normalization/matching lives here and
nowhere else. Used by the Extractor's self-mention match (``STORE_ALIASES``) and the
ShareOfModelAggregator's competitor match (``COMPETITOR_ALIASES``).
"""

import re
from collections.abc import Iterable

from pydantic import BaseModel

# Trailing tokens stripped before brand-alias matching, so "Northwind Coffee Roasters",
# "Northwind Coffee", and "Northwind" all collapse to the same key.
_STRIP_SUFFIXES = {"coffee", "roasters", "roaster", "co", "company", "inc"}

_PUNCT = re.compile(r"[^\w\s]")


class BrandMatch(BaseModel):
    """An extracted name matched to a store/competitor alias, keyed by its input position."""

    index: int  # 0-based position in the input name list
    name: str  # the extracted brand name as given
    matched_alias: str  # the canonical alias it matched


def normalize_text(text: str) -> str:
    """Casefold, replace punctuation with spaces, and collapse whitespace."""
    return " ".join(_PUNCT.sub(" ", text).casefold().split())


def _normalize_brand(name: str) -> str:
    """Normalize a brand name and strip trailing generic suffixes (coffee/roasters/co/...)."""
    tokens = normalize_text(name).split()
    while tokens and tokens[-1] in _STRIP_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_and_match(extracted_names: list[str], alias_set: Iterable[str]) -> list[BrandMatch]:
    """Match extracted brand names against an alias set by normalized, suffix-stripped equality.

    Returns one ``BrandMatch`` per matching name, carrying its input ``index`` so the caller can map
    it back to a rank/product. Designed to be reused by passing the competitor alias set instead of
    ``STORE_ALIASES``.
    """
    # Normalized alias -> first original alias string (aliases that collapse to the same key share
    # one canonical label).
    canonical: dict[str, str] = {}
    for alias in alias_set:
        norm = _normalize_brand(alias)
        if norm and norm not in canonical:
            canonical[norm] = alias

    matches: list[BrandMatch] = []
    for index, name in enumerate(extracted_names):
        norm = _normalize_brand(name)
        if norm and norm in canonical:
            matches.append(BrandMatch(index=index, name=name, matched_alias=canonical[norm]))
    return matches
