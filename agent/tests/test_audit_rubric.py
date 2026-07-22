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

Each branch is seeded distinctly.
"""

from app.services.audit_rubric import (
    MISSING_DESCRIPTION,
    MISSING_GTIN,
    SPEC_MISSING,
    evaluate_product,
)

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


# --- coffee ---------------------------------------------------------------------------------
def test_fully_optimized_coffee_has_no_gaps():
    result = _coffee()
    assert result.audited is True
    assert result.gaps == []
    assert result.severity == "none"
    assert result.spec_coverage == 1.0


def test_coffee_missing_one_spec_family_is_flagged_precisely():
    body = (
        "Single-origin washed Arabica from Ethiopia. Altitude 2,000 masl. Varietal: Heirloom. "
        "Process: washed. Roast level: light. Tasting notes: bergamot."
    )  # no brew-method language
    result = _coffee(body=body)
    assert _spec_attrs(result) == {"brew_method"}
    assert result.spec_coverage == 6 / 7


def test_coffee_blank_body_flags_missing_description():
    assert MISSING_DESCRIPTION in _codes(_coffee(body="<p><br></p>"))


def test_coffee_never_gets_missing_gtin_even_without_a_barcode():
    # Self-roasted coffee is GTIN-not-applicable — no barcode is not a gap.
    result = _coffee(variants=_variants(None))
    assert MISSING_GTIN not in _codes(result)


def test_spec_attribute_can_be_satisfied_by_a_metafield():
    result = _coffee(
        body="A lovely coffee.",
        metafields=[
            {"key": "roast", "value": "medium roast"},
            {"key": "origin", "value": "single-origin Ethiopia"},
            {"key": "process", "value": "washed"},
            {"key": "variety", "value": "Heirloom varietal"},
            {"key": "notes", "value": "tasting notes: bergamot"},
            {"key": "altitude", "value": "2000 masl"},
            {"key": "brew", "value": "pour over and espresso"},
        ],
    )
    assert _spec_attrs(result) == set()


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


# --- discoverability (population gate) -------------------------------------------------------
def test_draft_archived_unlisted_are_excluded_not_scored():
    for state in ("draft", "archived", "unlisted"):
        result = _coffee(visibility_state=state, body=None, variants=_variants(None))
        assert result.audited is False, state
        assert result.excluded_reason == "not_visible"
        assert result.severity == "not_audited"
        assert result.gaps == []
        assert result.spec_coverage is None


def test_active_and_null_visibility_are_audited():
    for state in ("active", None):
        assert _coffee(visibility_state=state).audited is True


# --- metafields are store-level, never a per-product gap ------------------------------------
def test_empty_metafields_never_produce_a_per_product_gap():
    result = _coffee(metafields=None)
    assert "missing_metafields" not in _codes(result)
    result2 = _equipment(metafields=[])
    assert "missing_metafields" not in _codes(result2)


# --- severity banding -----------------------------------------------------------------------
def test_empty_coffee_is_high():
    # A spec-neutral title so nothing (not even origin from "Ethiopia") is picked up.
    result = _coffee(title="Mystery Beans", body=None)
    # missing_description (2) + 7 spec_missing (7) = 9 -> high
    assert result.severity == "high"
    assert result.spec_coverage == 0.0


def test_coffee_with_five_missing_specs_is_medium():
    # 2 families present, 5 missing -> score 5 -> medium.
    body = "Medium roast, single-origin Ethiopia."
    result = _coffee(body=body)
    assert result.spec_coverage == 2 / 7
    assert result.severity == "medium"


def test_coffee_with_one_missing_spec_is_low():
    body = (
        "Single-origin washed Arabica from Ethiopia. Altitude 2,000 masl. Varietal: Heirloom. "
        "Process: washed. Roast level: light. Tasting notes: bergamot."
    )
    assert _coffee(body=body).severity == "low"


def test_equipment_missing_gtin_is_medium():
    # missing_gtin weight is 3, so a manufactured good with no GTIN lands at medium.
    assert _equipment(variants=_variants(None)).severity == "medium"


def test_equipment_complete_is_none():
    assert _equipment(variants=_variants("0123456789012")).severity == "none"


def test_result_is_deterministic():
    a = _coffee(body="Medium roast from Colombia.")
    b = _coffee(body="Medium roast from Colombia.")
    assert a.model_dump() == b.model_dump()
