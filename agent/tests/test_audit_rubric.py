"""The per-class product-audit rubric (Phase 3, step 1 — Gate G).

Pure, deterministic rule checks. The rubric is **per product class**:

* spec scoring (roast/origin/process/variety/tasting notes/altitude/brew method) applies to
  ``coffee`` only — the vocabulary is anchored to run-75's cited coffee-bean pages. ``equipment``
  has no grounded vocabulary (run 75's panel is coffee-bean queries, so no cited equipment pages),
  so equipment is NOT spec-scored; ``other`` is skipped too.
* ``missing_gtin`` applies only to ``equipment`` (third-party manufactured goods carry a
  manufacturer GTIN); self-roasted coffee is GTIN-not-applicable. GTIN presence is read from the
  variant barcode — the single source of truth shared with the Optimizer.
* not-discoverable products (draft/archived/unlisted) are EXCLUDED from the audit population and
  reported separately — not scored and banded.
* ``missing_metafields`` is a store-level finding, NOT a per-product gap, so it no longer inflates
  per-product severity.

**Step 2b — the three-state model.** Every (product, family) is ``structured`` (in a metafield),
``unstructured`` (in the prose only) or ``absent``. So there are two independent coverage numbers,
and rich prose alone is no longer "optimized": it is a catalog full of *fixable* gaps. Severity is
re-banded on state-weighted scores. Each state and each exclusion branch is seeded distinctly.

Each branch is seeded distinctly.
"""

import pytest

from app.services.audit_rubric import (
    MISSING_DESCRIPTION,
    MISSING_GTIN,
    SPEC_MISSING,
    STATE_ABSENT,
    STATE_UNSTRUCTURED,
    evaluate_product,
    structured_families,
)
from app.services.catalog import classify_product

RICH_BODY = (
    "Single-origin washed Arabica from Ethiopia. Altitude 1,900-2,100 masl. Varietal: Heirloom. "
    "Process: washed, 36-hour fermentation. Roast level: light (Agtron 68). Tasting notes: "
    "bergamot, jasmine, stone fruit. Brews beautifully as pour over or espresso."
)


def _variants(barcode: str | None = "0123456789012") -> list[dict]:
    return [{"id": "gid://shopify/Variant/1", "barcode": barcode}]


def _codes(result) -> set[str]:
    return {gap.code for gap in result.gaps}


def _spec_attrs(result) -> set[str]:
    return {gap.attribute for gap in result.gaps if gap.code == SPEC_MISSING}


def _states(result, state: str) -> set[str]:
    """The families the rubric tagged with ``state`` (its detect-based three-state proxy)."""
    return {g.attribute for g in result.gaps if g.code == SPEC_MISSING and g.state == state}


_ALL_FAMILIES = {
    "roast_level", "origin", "process", "variety", "tasting_notes", "altitude", "brew_method",
}


def _meta(*families: str, value: str = "x") -> list[dict]:
    """Metafields KEYED to each family — the ``structured`` state."""
    return [{"namespace": "custom", "key": f, "value": value} for f in families]


def _coffee(**overrides):
    kwargs = dict(
        title="Ethiopia Yirgacheffe",
        body=RICH_BODY,
        variants=_variants(None),  # coffee GTIN is not applicable, so barcode is irrelevant
        metafields=None,
        visibility_state="active",
        product_class="coffee",
    )
    kwargs.update(overrides)
    return evaluate_product(**kwargs)


def _equipment(**overrides):
    kwargs = dict(
        title="Conical Burr Grinder",
        body="A great grinder.",
        variants=_variants("0123456789012"),
        metafields=None,
        visibility_state="active",
        product_class="equipment",
    )
    kwargs.update(overrides)
    return evaluate_product(**kwargs)


# --- coffee: the three-state model ----------------------------------------------------------
def test_fully_structured_coffee_has_no_gaps():
    """AI-legible means MACHINE-READABLE: every family in a metafield. This is the only state that
    yields no gaps — prose alone no longer qualifies (see the next test)."""
    result = _coffee(metafields=_meta(*_ALL_FAMILIES))
    assert result.audited is True
    assert result.gaps == []
    assert result.severity == "none"
    assert result.structured_coverage == 1.0


def test_rich_prose_without_metafields_is_entirely_unstructured():
    """The step-2b inversion: RICH_BODY states all seven families, which used to score a perfect
    1.0 and zero gaps. It is now seven UNSTRUCTURED gaps — the addressable set, not a clean bill."""
    result = _coffee()  # metafields=None
    assert result.spec_coverage == 1.0        # prose: everything is stated
    assert result.structured_coverage == 0.0  # structured: nothing is machine-readable
    assert _states(result, STATE_UNSTRUCTURED) == _ALL_FAMILIES
    assert _states(result, STATE_ABSENT) == set()


def test_three_state_split_separates_unstructured_from_absent():
    body = (
        "Single-origin washed Arabica from Ethiopia. Altitude 2,000 masl. Varietal: Heirloom. "
        "Process: washed. Roast level: light. Tasting notes: bergamot."
    )  # six families in prose; no brew-method language anywhere
    result = _coffee(body=body, metafields=_meta("origin"))
    # origin is in a metafield -> structured -> no gap at all.
    assert "origin" not in _spec_attrs(result)
    # Stated in prose but not structured -> fixable.
    assert _states(result, STATE_UNSTRUCTURED) == {
        "roast_level", "process", "variety", "tasting_notes", "altitude",
    }
    # Stated nowhere -> a merchant must supply it.
    assert _states(result, STATE_ABSENT) == {"brew_method"}
    assert result.spec_coverage == 6 / 7
    assert result.structured_coverage == 1 / 7


def test_the_difference_between_the_two_numbers_is_the_addressable_set():
    """The finding this step exists to produce: high prose coverage, zero structured coverage."""
    result = _coffee()
    addressable = _states(result, STATE_UNSTRUCTURED)
    assert result.spec_coverage - result.structured_coverage == len(addressable) / 7


def test_coffee_blank_body_flags_missing_description():
    assert MISSING_DESCRIPTION in _codes(_coffee(body="<p><br></p>"))


def test_coffee_never_gets_missing_gtin_even_without_a_barcode():
    # Self-roasted coffee is GTIN-not-applicable — no barcode is not a gap.
    result = _coffee(variants=_variants(None))
    assert MISSING_GTIN not in _codes(result)


# --- structured_families: the single classifier both the rubric and the Optimizer read ---------
def test_structured_family_is_keyed_by_the_metafield_key_not_its_value():
    """A family is structured because a metafield NAMES it, not because prose happens to appear in
    some metafield's value. A roast value filed under an unrelated key is not machine-readable."""
    assert structured_families([{"key": "roast_level", "value": "Light"}]) == {"roast_level"}
    assert structured_families([{"key": "internal_note", "value": "light roast"}]) == set()


def test_structured_family_accepts_merchant_key_aliases_and_ignores_namespace():
    # The Optimizer writes custom.<family>; merchants use their own namespace and wording.
    assert structured_families([{"namespace": "custom", "key": "roast", "value": "Light"}]) == {
        "roast_level"
    }
    assert structured_families([{"namespace": "my_fields", "key": "varietal", "value": "Gesha"}]) \
        == {"variety"}


def test_empty_metafield_value_is_not_structured():
    """A key with no value is not machine-readable. Counting it would BOTH inflate the headline
    score and silently drop a real gap from the Optimizer's targets."""
    assert structured_families([{"key": "roast_level", "value": "   "}]) == set()
    assert structured_families([{"key": "roast_level", "value": None}]) == set()
    assert structured_families([{"key": "roast_level"}]) == set()
    # ...and the rubric agrees: the family is still a gap.
    result = _coffee(body="A lovely coffee.", metafields=[{"key": "roast_level", "value": ""}])
    assert "roast_level" in _spec_attrs(result)


def test_generic_keys_do_not_false_positive_as_a_spec_family():
    """Aliases are curated, not taken from the open-kind ``labels`` (which include generic terms
    like "notes"/"aroma"/"masl"). A false ``structured`` is the dangerous direction."""
    assert structured_families([{"key": "notes", "value": "call the supplier"}]) == set()
    assert structured_families([{"key": "aroma", "value": "nice"}]) == set()


def test_structured_families_tolerates_malformed_metafields():
    assert structured_families(None) == set()
    assert structured_families([]) == set()
    assert structured_families(["not-a-dict", {"value": "no key"}]) == set()


def test_metafields_removes_the_family_from_the_gaps_entirely():
    result = _coffee(body="A lovely coffee.", metafields=_meta("roast_level"))
    assert "roast_level" not in _spec_attrs(result)
    assert result.structured_coverage == 1 / 7


# --- equipment ------------------------------------------------------------------------------
def test_equipment_is_not_spec_scored():
    result = _equipment(body="")  # nothing coffee-ish at all
    assert result.spec_coverage is None
    assert not any(g.code == SPEC_MISSING for g in result.gaps)


def test_equipment_without_a_barcode_flags_missing_gtin():
    result = _equipment(variants=_variants(None))
    assert MISSING_GTIN in _codes(result)


def test_equipment_with_a_barcode_has_no_gtin_gap():
    result = _equipment(variants=_variants("0123456789012"))
    assert MISSING_GTIN not in _codes(result)


# --- other / unset --------------------------------------------------------------------------
def test_other_class_is_not_spec_scored_and_has_no_gtin_gap():
    result = evaluate_product(
        title="Gift Card", body="", variants=_variants(None), metafields=None,
        visibility_state="active", product_class="other",
    )
    assert result.spec_coverage is None
    assert MISSING_GTIN not in _codes(result)
    assert not any(g.code == SPEC_MISSING for g in result.gaps)


@pytest.mark.parametrize("product_type", ["Whole Bean", "Merch", "", None])
def test_unknown_product_type_falls_back_to_unset_not_coffee_vocabulary(product_type):
    """Full chain: an unmapped/empty/NULL productType classifies to the UNSET class ("other") and
    is NOT spec-scored — the classifier never guesses the coffee vocabulary for unknown data."""
    product_class = classify_product(product_type, None)
    assert product_class == "other"

    result = evaluate_product(
        title="Something", body="", variants=_variants(None), metafields=None,
        visibility_state="active", product_class=product_class,
    )
    assert result.spec_coverage is None
    assert not any(g.code == SPEC_MISSING for g in result.gaps)


# --- discoverability (population gate) -------------------------------------------------------
def test_draft_archived_unlisted_are_excluded_not_scored():
    for state in ("draft", "archived", "unlisted"):
        result = _coffee(visibility_state=state, body=None, variants=_variants(None))
        assert result.audited is False, state
        assert result.excluded_reason == "not_visible"
        assert result.severity == "not_audited"
        assert result.gaps == []
        # BOTH coverage numbers are NULL for an excluded product — never a misleading 0.0.
        assert result.spec_coverage is None
        assert result.structured_coverage is None


def test_active_and_null_visibility_are_audited():
    for state in ("active", None):
        assert _coffee(visibility_state=state).audited is True


# --- metafields are store-level, never a per-product gap ------------------------------------
def test_empty_metafields_never_produce_a_per_product_gap():
    result = _coffee(metafields=None)
    assert "missing_metafields" not in _codes(result)
    result2 = _equipment(metafields=[])
    assert "missing_metafields" not in _codes(result2)


# --- spec_coverage is PROSE-only -------------------------------------------------------------
def test_metafield_values_do_not_count_toward_prose_coverage():
    """Metafield values are the STRUCTURED channel. If they also fed prose coverage, a structured
    family would raise both numbers and their difference — the addressable set — would collapse."""
    result = _coffee(
        title="Mystery Beans",  # spec-neutral, so prose contributes nothing on its own
        body="A lovely coffee.",
        metafields=[{"key": "roast_level", "value": "light roast, Agtron 68"}],
    )
    assert result.structured_coverage == 1 / 7
    # "light roast" lives only in the metafield, so prose coverage must NOT see it.
    assert result.spec_coverage == 0.0


# --- severity banding (step 2b re-baseline; Gate G re-approved) -------------------------------
def test_empty_coffee_is_high():
    # A spec-neutral title so nothing (not even origin from "Ethiopia") is picked up.
    result = _coffee(title="Mystery Beans", body=None)
    # missing_description (4) + 7 absent (7*2=14) = 18 -> high
    assert result.severity == "high"
    assert result.spec_coverage == 0.0
    assert result.structured_coverage == 0.0


def test_mostly_absent_coffee_is_high():
    # 2 in prose (unstructured, 1 each), 5 absent (2 each) -> 2 + 10 = 12 -> high.
    result = _coffee(body="Medium roast, single-origin Ethiopia.")
    assert result.spec_coverage == 2 / 7
    assert result.severity == "high"


def test_mostly_unstructured_coffee_is_only_medium():
    """Weighting is state-based: prose-stated specs are auto-fixable, so a rich-prose product is
    materially better off than an empty one even though neither is machine-readable."""
    body = (
        "Single-origin washed Arabica from Ethiopia. Altitude 2,000 masl. Varietal: Heirloom. "
        "Process: washed. Roast level: light. Tasting notes: bergamot."
    )
    # 6 unstructured (6) + 1 absent (2) = 8 -> medium
    assert _coffee(body=body).severity == "medium"


def test_unstructured_scores_strictly_better_than_absent():
    """The ordering that gives the bands their meaning: prose-stated specs are auto-fixable, so a
    product whose families are merely unstructured must band strictly better than one where the
    same families are absent. Both are equally un-machine-readable (structured_coverage 0.0), so
    only the state weighting separates them."""
    in_prose = _coffee()  # RICH_BODY: all seven stated in prose
    nowhere = _coffee(title="Mystery Beans", body="A coffee.")  # none stated anywhere
    assert _states(in_prose, STATE_UNSTRUCTURED) == _ALL_FAMILIES
    assert _states(nowhere, STATE_ABSENT) == _ALL_FAMILIES
    assert in_prose.structured_coverage == nowhere.structured_coverage == 0.0

    bands = ["none", "low", "medium", "high"]
    assert bands.index(in_prose.severity) < bands.index(nowhere.severity)


def test_equipment_missing_gtin_is_still_medium_on_its_own():
    """Pins the step-1 intent the re-baseline preserved: missing_gtin was scaled with the spec
    weights (3->6) so a manufactured good lacking its GTIN still lands at medium by itself. At the
    un-scaled weight it would have silently demoted to low."""
    result = _equipment(variants=_variants(None))
    assert result.gaps and all(g.code == MISSING_GTIN for g in result.gaps)
    assert result.severity == "medium"


def test_equipment_complete_is_none():
    assert _equipment(variants=_variants("0123456789012")).severity == "none"


def test_result_is_deterministic():
    a = _coffee(body="Medium roast from Colombia.")
    b = _coffee(body="Medium roast from Colombia.")
    assert a.model_dump() == b.model_dump()


def test_the_audit_path_makes_no_llm_call():
    """Gate G's core claim, pinned. The three-state split is tempting to compute with the
    Optimizer's extractor — that would make severity nondeterministic and unreproducible. The
    rubric must reach its split with the detect-based proxy and nothing else."""
    import inspect

    from app.services import audit_rubric

    source = inspect.getsource(audit_rubric)
    for banned in ("optimizer_llm", "extractor_llm", "httpx", "openai", "await "):
        assert banned not in source, f"the audit path must not reference {banned!r}"
    # No client can even be passed in.
    assert "client" not in inspect.signature(audit_rubric.evaluate_product).parameters
