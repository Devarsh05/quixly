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
from app.services.audit_rubric import validate_spec_value
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


# --- Positive validation: a grounded value must be valid FOR the target family ---------------
# At a ~1.6% fill rate, over-strictness is invisible in aggregate — so assert that LEGITIMATE
# values (including compound / variant phrasings) still pass.
LEGITIMATE = [
    ("roast_level", "light", ""),
    ("roast_level", "medium-light", ""),
    ("roast_level", "Medium-Light", ""),
    ("roast_level", "dark", ""),
    ("process", "washed", ""),
    ("process", "natural", ""),
    ("process", "honey", ""),
    ("brew_method", "pour over", ""),
    ("brew_method", "espresso", ""),
    ("brew_method", "cold brew", ""),
    ("variety", "Heirloom", ""),
    ("variety", "Gesha", ""),
    ("origin", "Ethiopia", ""),
    ("origin", "Ethiopia, Yirgacheffe", ""),
    ("origin", "Costa Rica", ""),
    ("altitude", "1,800 masl", ""),
    ("altitude", "1800m", ""),
    ("altitude", "2000 metres", ""),
    ("tasting_notes", "bergamot", "Tasting notes: bergamot, jasmine"),
]


@pytest.mark.parametrize("family,value,snippet", LEGITIMATE)
def test_legitimate_values_validate(family, value, snippet):
    assert validate_spec_value(family, value, snippet) is True


# --- Mis-assignment: a real source token grounded onto the WRONG family must be refused --------
MIS_ASSIGNED = [
    ("brew_method", "Washed", "Process: Washed"),   # the observed defect (process → brew_method)
    ("altitude", "340", "Ethiopia Yirgacheffe 340 g"),  # a weight → altitude
    ("altitude", "340 g", "340 g net weight"),
    ("altitude", "500", "500 reviews"),
    ("roast_level", "washed", "washed"),
    ("process", "pour over", "pour over"),
    ("variety", "washed", "washed"),
    ("origin", "washed", "washed"),
]


@pytest.mark.parametrize("family,value,snippet", MIS_ASSIGNED)
def test_mis_assigned_values_are_refused(family, value, snippet):
    assert validate_spec_value(family, value, snippet) is False


def test_open_family_refuses_a_competing_label_snippet():
    # A tasting-notes candidate whose snippet is actually a process statement (competing label).
    assert validate_spec_value("tasting_notes", "washed", "Process: washed") is False
