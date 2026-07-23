"""The shared grounding primitive `is_grounded` (services/matching.py).

The single anti-fabrication check reused by BOTH the Extractor (brand literally present in an
engine answer) and the Optimizer (an attribute value / snippet literally present in a source
field). Normalized-substring: casefolded, punctuation→space, whitespace-collapsed.
"""

from app.services.matching import is_grounded


def test_literal_presence_is_grounded():
    assert is_grounded("light roast", "This is a light roast coffee.") is True


def test_case_and_punctuation_insensitive():
    # normalize_text casefolds and turns punctuation into spaces.
    assert is_grounded("Light Roast", "ROAST LEVEL: Light-Roast!") is True


def test_absent_needle_is_not_grounded():
    assert is_grounded("dark roast", "This is a light roast coffee.") is False


def test_empty_needle_never_grounds():
    assert is_grounded("", "anything at all") is False
    assert is_grounded("   ", "anything at all") is False


def test_needle_spanning_normalized_whitespace():
    # Collapsed whitespace means multi-space / newline gaps still match.
    assert is_grounded("single origin", "A  single\norigin  Ethiopian coffee") is True
