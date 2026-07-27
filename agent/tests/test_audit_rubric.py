"""The per-class product-audit rubric (Phase 3, steps 1/2b — Gate G; 2d re-bake — Gate M).

Pure, deterministic rule checks. The rubric is **per product class**:

* spec scoring (roast/origin/process/variety/tasting notes/altitude/brew method, + step-2d
  coffee_product_form and — decaf-only — decaffeination_method) applies to ``coffee`` only.
  ``equipment`` / ``other`` are not spec-scored.
* ``missing_gtin`` applies only to ``equipment``; ``missing_description`` to any audited product.
* not-discoverable products (draft/archived/unlisted) are EXCLUDED and reported separately.

**Three-state model (unchanged since 2b).** Every (product, family) is ``structured`` /
``unstructured`` / ``absent``. **Step 2d** changed only WHICH write makes a family ``structured``:
its **taxonomy attribute** in the reserved ``shopify`` namespace (``shopify.coffee-roast`` etc.) —
``custom.*`` no longer counts — and only the 4 taxonomy-home families can ever be structured. Two
channel-specific coverage numbers, per-product denominators over APPLICABLE families:
``spec_coverage`` = prose / applicable spec families (8, or 9 for decaf); ``taxonomy_coverage`` =
taxonomy-written / applicable taxonomy-home families (3, or 4 for decaf). They are NOT blended.
Each state and each exclusion branch is seeded distinctly.
"""

import pytest

from app.services.audit_rubric import (
    MISSING_DESCRIPTION,
    MISSING_GTIN,
    SPEC_MISSING,
    STATE_ABSENT,
    STATE_UNSTRUCTURED,
    applicable_families,
    evaluate_product,
    structured_families,
)
from app.services.catalog import classify_product

RICH_BODY = (
    "Single-origin washed Arabica from Ethiopia. Altitude 1,900-2,100 masl. Varietal: Heirloom. "
    "Process: washed, 36-hour fermentation. Roast level: light (Agtron 68). Tasting notes: "
    "bergamot, jasmine, stone fruit. Brews beautifully as pour over or espresso."
)

# The 8 spec families APPLICABLE to a (non-decaf) coffee. decaffeination_method is decaf-only.
_APPLICABLE = {
    "roast_level", "origin", "process", "variety", "tasting_notes", "altitude", "brew_method",
    "coffee_product_form",
}
# Families RICH_BODY states in prose — everything except coffee_product_form (no whole-bean/ground).
_IN_PROSE = _APPLICABLE - {"coffee_product_form"}
# Applicable taxonomy-home families (non-decaf): the taxonomy_coverage denominator = 3.
_HOME = {"roast_level", "origin", "coffee_product_form"}

# The reserved ``shopify`` taxonomy attribute handles — the ONLY way to make a family structured.
_HANDLE = {
    "roast_level": "coffee-roast",
    "origin": "country",
    "coffee_product_form": "coffee-product-form",
    "decaffeination_method": "decaffeination-method",
}


def _variants(barcode: str | None = "0123456789012") -> list[dict]:
    return [{"id": "gid://shopify/Variant/1", "barcode": barcode}]


def _codes(result) -> set[str]:
    return {gap.code for gap in result.gaps}


def _spec_attrs(result) -> set[str]:
    return {gap.attribute for gap in result.gaps if gap.code == SPEC_MISSING}


def _states(result, state: str) -> set[str]:
    return {g.attribute for g in result.gaps if g.code == SPEC_MISSING and g.state == state}


def _struct(*families: str, value: str = "Light") -> list[dict]:
    """Metafields in the ``shopify`` namespace keyed to each family's TAXONOMY handle — the only
    ``structured`` state in step 2d. Only taxonomy-home families have a handle."""
    return [{"namespace": "shopify", "key": _HANDLE[f], "value": value} for f in families]


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


# --- applicability: decaffeination_method is decaf-only --------------------------------------
def test_decaf_method_applies_only_to_decaf_products():
    assert "decaffeination_method" not in applicable_families("A washed Ethiopian coffee.")
    assert "decaffeination_method" in applicable_families("Decaf, Swiss Water process.")
    # the always-applicable new win is present regardless
    assert "coffee_product_form" in applicable_families("A washed Ethiopian coffee.")


def test_a_non_decaf_coffee_is_never_dinged_for_a_decaffeination_method():
    result = _coffee()  # RICH_BODY, not decaf
    assert "decaffeination_method" not in _spec_attrs(result)


def test_a_decaf_coffee_scores_the_decaffeination_method_family():
    # "decaf" makes the family applicable; Swiss Water is stated → unstructured (in prose).
    result = _coffee(title="Decaf Colombia", body="Decaf, Swiss Water process. Medium roast.")
    assert "decaffeination_method" in _states(result, STATE_UNSTRUCTURED)


# --- coffee: the three-state model ----------------------------------------------------------
def test_rich_prose_without_metafields_is_entirely_unstructured():
    """RICH_BODY states 7 of the 8 applicable families in prose; none is in a taxonomy attribute.
    So it is 7 UNSTRUCTURED gaps + 1 ABSENT (coffee_product_form, not stated) — the addressable
    set, not a clean bill. taxonomy_coverage is 0.0 (the headline channel is empty)."""
    result = _coffee()  # metafields=None
    assert result.spec_coverage == len(_IN_PROSE) / len(_APPLICABLE)  # 7/8 prose
    assert result.taxonomy_coverage == 0.0                            # nothing in the taxonomy
    assert _states(result, STATE_UNSTRUCTURED) == _IN_PROSE
    assert _states(result, STATE_ABSENT) == {"coffee_product_form"}


def test_taxonomy_home_families_structured_lifts_only_taxonomy_coverage():
    """A coffee can never reach ``none`` — the 5 non-taxonomy families have no structured channel —
    but writing the 3 applicable taxonomy-home attributes drives taxonomy_coverage to 1.0 and drops
    those families from the gaps entirely."""
    result = _coffee(metafields=_struct("roast_level", "origin", "coffee_product_form"))
    assert result.taxonomy_coverage == 1.0
    assert _spec_attrs(result) & _HOME == set()          # the 3 taxonomy-home families: no gap
    # the 5 non-taxonomy families remain (unstructured, since RICH_BODY states them all)
    assert _states(result, STATE_UNSTRUCTURED) == {
        "process", "variety", "tasting_notes", "altitude", "brew_method"
    }
    assert result.severity == "medium"  # 5 unstructured * 1 = 5


def test_three_state_split_separates_unstructured_from_absent():
    body = (
        "Single-origin washed Arabica from Ethiopia. Altitude 2,000 masl. Varietal: Heirloom. "
        "Process: washed. Roast level: light. Tasting notes: bergamot."
    )  # 6 families in prose; no brew-method / product-form language anywhere
    result = _coffee(body=body, metafields=_struct("origin", value="Ethiopia"))
    # origin is in its taxonomy attribute -> structured -> no gap at all.
    assert "origin" not in _spec_attrs(result)
    assert _states(result, STATE_UNSTRUCTURED) == {
        "roast_level", "process", "variety", "tasting_notes", "altitude",
    }
    # Stated nowhere -> a merchant must supply it.
    assert _states(result, STATE_ABSENT) == {"brew_method", "coffee_product_form"}
    assert result.spec_coverage == 6 / 8         # 6 of 8 applicable stated in prose
    assert result.taxonomy_coverage == 1 / 3     # origin of the 3 taxonomy-home families


def test_custom_namespace_metafields_are_not_structured():
    """The step-2d flip: a ``custom.*`` metafield — including the ones the pre-2d Optimizer wrote —
    is NOT the AI-legible channel, so it does not count as structured and does not lift the headline
    number. Only the reserved ``shopify`` taxonomy attribute does."""
    custom = [{"namespace": "custom", "key": "roast_level", "value": "Light"}]
    result = _coffee(body="A lovely coffee.", metafields=custom)
    assert "roast_level" in _spec_attrs(result)   # still a gap
    assert result.taxonomy_coverage == 0.0


def test_coffee_blank_body_flags_missing_description():
    assert MISSING_DESCRIPTION in _codes(_coffee(body="<p><br></p>"))


def test_coffee_never_gets_missing_gtin_even_without_a_barcode():
    result = _coffee(variants=_variants(None))
    assert MISSING_GTIN not in _codes(result)


# --- structured_families: the single classifier both the rubric and the Optimizer read ---------
def test_structured_family_is_keyed_by_the_shopify_taxonomy_handle():
    assert structured_families(
        [{"namespace": "shopify", "key": "coffee-roast", "value": "Light"}]
    ) == {"roast_level"}
    assert structured_families(
        [{"namespace": "shopify", "key": "country", "value": "Ethiopia"}]
    ) == {"origin"}
    assert structured_families(
        [{"namespace": "shopify", "key": "decaffeination-method", "value": "Swiss Water"}]
    ) == {"decaffeination_method"}


def test_only_the_shopify_namespace_counts_as_structured():
    # The taxonomy channel is the RESERVED shopify namespace. A merchant's own namespace does not
    # make a family machine-legible, even keyed to the handle.
    assert structured_families([{"namespace": "custom", "key": "coffee-roast", "value": "Light"}]) \
        == set()
    assert structured_families([{"namespace": "custom", "key": "roast_level", "value": "Light"}]) \
        == set()
    assert structured_families([{"namespace": "my_fields", "key": "country", "value": "Peru"}]) \
        == set()


def test_non_taxonomy_families_can_never_be_structured():
    # process / variety / altitude / brew_method / tasting_notes have no taxonomy attribute, so no
    # metafield can ever mark them structured — their legible ceiling is the description.
    for key in ("process", "variety", "altitude", "brew-method", "tasting-notes", "flavor"):
        assert structured_families([{"namespace": "shopify", "key": key, "value": "x"}]) == set()


def test_empty_metafield_value_is_not_structured():
    assert structured_families([{"namespace": "shopify", "key": "coffee-roast", "value": "  "}]) \
        == set()
    assert structured_families([{"namespace": "shopify", "key": "coffee-roast", "value": None}]) \
        == set()
    assert structured_families([{"namespace": "shopify", "key": "coffee-roast"}]) == set()
    result = _coffee(
        body="A lovely coffee.",
        metafields=[{"namespace": "shopify", "key": "coffee-roast", "value": ""}],
    )
    assert "roast_level" in _spec_attrs(result)


def test_structured_families_tolerates_malformed_metafields():
    assert structured_families(None) == set()
    assert structured_families([]) == set()
    assert structured_families(["not-a-dict", {"value": "no key"}]) == set()


def test_taxonomy_metafield_removes_the_family_from_the_gaps_entirely():
    result = _coffee(body="A lovely coffee.", metafields=_struct("roast_level"))
    assert "roast_level" not in _spec_attrs(result)
    assert result.taxonomy_coverage == 1 / 3


# --- equipment ------------------------------------------------------------------------------
def test_equipment_is_not_spec_scored():
    result = _equipment(body="")
    assert result.spec_coverage is None
    assert result.taxonomy_coverage is None
    assert not any(g.code == SPEC_MISSING for g in result.gaps)


def test_equipment_without_a_barcode_flags_missing_gtin():
    assert MISSING_GTIN in _codes(_equipment(variants=_variants(None)))


def test_equipment_with_a_barcode_has_no_gtin_gap():
    assert MISSING_GTIN not in _codes(_equipment(variants=_variants("0123456789012")))


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
        assert result.spec_coverage is None
        assert result.taxonomy_coverage is None


def test_active_and_null_visibility_are_audited():
    for state in ("active", None):
        assert _coffee(visibility_state=state).audited is True


# --- metafields are store-level, never a per-product gap ------------------------------------
def test_empty_metafields_never_produce_a_per_product_gap():
    assert "missing_metafields" not in _codes(_coffee(metafields=None))
    assert "missing_metafields" not in _codes(_equipment(metafields=[]))


# --- spec_coverage is PROSE-only -------------------------------------------------------------
def test_metafield_values_do_not_count_toward_prose_coverage():
    result = _coffee(
        title="Mystery Beans",  # spec-neutral, so prose contributes nothing on its own
        body="A lovely coffee.",
        metafields=_struct("roast_level", value="light roast, Agtron 68"),
    )
    assert result.taxonomy_coverage == 1 / 3
    # "light roast" lives only in the metafield, so prose coverage must NOT see it.
    assert result.spec_coverage == 0.0


# --- severity banding (2d re-bake; Gate M) --------------------------------------------------
def test_empty_coffee_is_high():
    result = _coffee(title="Mystery Beans", body=None)
    # missing_description (4) + 8 absent (8*2=16) = 20 -> high
    assert result.severity == "high"
    assert result.spec_coverage == 0.0
    assert result.taxonomy_coverage == 0.0


def test_mostly_absent_coffee_is_high():
    # roast + origin in prose (unstructured, 1 each); 6 absent (2 each) -> 2 + 12 = 14 -> high.
    result = _coffee(body="Medium roast, single-origin Ethiopia.")
    assert result.spec_coverage == 2 / 8
    assert result.severity == "high"


def test_unstructured_scores_strictly_better_than_absent():
    """Prose-stated specs are auto-fixable, so a product whose families are merely unstructured
    must band strictly better than one where the same families are absent. Both have
    taxonomy_coverage 0.0, so only the state weighting separates them."""
    in_prose = _coffee()  # RICH_BODY: 7 stated in prose, 1 absent
    nowhere = _coffee(title="Mystery Beans", body="A coffee.")  # none stated anywhere
    assert in_prose.taxonomy_coverage == nowhere.taxonomy_coverage == 0.0
    bands = ["none", "low", "medium", "high"]
    assert bands.index(in_prose.severity) < bands.index(nowhere.severity)


def test_equipment_missing_gtin_is_still_medium_on_its_own():
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
    """Gate G's core claim, pinned: the three-state split is reached with the detect-based proxy and
    nothing else — no LLM, so severity stays deterministic and reproducible."""
    import inspect

    from app.services import audit_rubric

    source = inspect.getsource(audit_rubric)
    for banned in ("optimizer_llm", "extractor_llm", "httpx", "openai", "await "):
        assert banned not in source, f"the audit path must not reference {banned!r}"
    assert "client" not in inspect.signature(audit_rubric.evaluate_product).parameters
