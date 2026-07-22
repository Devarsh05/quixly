"""Grounding suite for the Optimizer (Phase 3, step 2 — Gate H).

The load-bearing anti-fabrication proof. The Optimizer EXTRACTS AND RESTRUCTURES — it never
generates — so the only thing that keeps a hallucinated claim out of a fix is the grounding guard
(``ground_attribute`` → ``services.matching.is_grounded``). These tests exercise the **guard**, not
the LLM: a candidate only becomes a value if its snippet is literally in the named source field AND
the value is literally in that snippet.

For each of the seven coffee spec families × three cases:
  (a) plainly stated in a source field  → extracted (value returned)
  (b) absent from every source field     → refused (None) — becomes a merchant to-do
  (c) ambiguous / contradictory          → refused (None) — never picks one
plus explicit hallucination cases (value or snippet not in source → refused).
"""

import pytest

from app.graph.optimizer import ground_attribute
from app.services.optimizer_llm import AttributeCandidate

# (family, a plainly-stated value, prose that literally contains it)
FAMILY_CASES = [
    ("roast_level", "light", "Roast level: light (Agtron 68)."),
    ("origin", "Ethiopia", "Single-origin Ethiopia, washed."),
    ("process", "washed", "Process: washed, 36-hour fermentation."),
    ("variety", "Heirloom", "Varietal: Heirloom."),
    ("tasting_notes", "bergamot", "Tasting notes: bergamot, jasmine."),
    ("altitude", "2000 masl", "Grown at 2000 masl."),
    ("brew_method", "pour over", "Brews beautifully as pour over."),
]


@pytest.mark.parametrize("family,value,prose", FAMILY_CASES)
def test_a_plainly_stated_attribute_is_grounded(family, value, prose):
    cand = AttributeCandidate(
        attribute=family, value=value, source_field="body_html", snippet=prose, ambiguous=False
    )
    assert ground_attribute(cand, {"body_html": prose}) == value


@pytest.mark.parametrize("family,value,prose", FAMILY_CASES)
def test_b_absent_attribute_is_refused(family, value, prose):
    # The model correctly returns value=null when the attribute is nowhere in the source.
    cand = AttributeCandidate(
        attribute=family, value=None, source_field=None, snippet=None, ambiguous=False
    )
    assert ground_attribute(cand, {"body_html": "A pleasant everyday coffee."}) is None


@pytest.mark.parametrize("family,value,prose", FAMILY_CASES)
def test_c_ambiguous_attribute_is_refused(family, value, prose):
    # Contradictory sources → the model flags ambiguous and returns null; the node never picks one.
    cand = AttributeCandidate(
        attribute=family, value=None, source_field=None, snippet=None, ambiguous=True
    )
    assert ground_attribute(cand, {"body_html": prose, "metafields": "conflicting"}) is None


def test_hallucinated_value_with_fake_snippet_is_refused():
    # The model asserts a value + a snippet that is NOT in the source field.
    cand = AttributeCandidate(
        attribute="roast_level", value="dark", source_field="body_html",
        snippet="Roast level: dark", ambiguous=False,
    )
    assert ground_attribute(cand, {"body_html": "Roast level: light"}) is None


def test_value_not_inside_a_real_snippet_is_refused():
    # The snippet IS in the source, but the claimed value is not inside the snippet (fabricated).
    cand = AttributeCandidate(
        attribute="origin", value="Kenya", source_field="body_html",
        snippet="Single-origin Ethiopia", ambiguous=False,
    )
    assert ground_attribute(cand, {"body_html": "Single-origin Ethiopia"}) is None


def test_unknown_source_field_is_refused():
    cand = AttributeCandidate(
        attribute="roast_level", value="light", source_field="nonexistent",
        snippet="light", ambiguous=False,
    )
    assert ground_attribute(cand, {"body_html": "Roast level: light"}) is None
