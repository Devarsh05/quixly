"""The deterministic product-audit rubric (Phase 3, step 1 — Gate G).

Pure rule checks over a product's catalog fields; no DB, no LLM. The spec vocabulary is
anchored to attributes the competitor pages cited in run 75 actually carry (roast level,
origin, process, variety, tasting notes, altitude, brew-method suitability) — not invented
thresholds. Each gap branch is seeded distinctly so a regression in one check can't hide
behind another.
"""

from app.services.audit_rubric import (
    MISSING_DESCRIPTION,
    MISSING_GTIN,
    MISSING_METAFIELDS,
    NOT_DISCOVERABLE,
    SPEC_MISSING,
    evaluate_product,
)

# A product that carries every spec family in prose, plus a structured metafield, a GTIN,
# a description, and an active (discoverable) status. The rubric should find nothing.
RICH_BODY = (
    "Single-origin washed Arabica from Ethiopia. Altitude 1,900-2,100 masl. "
    "Varietal: Heirloom. Process: washed, 36-hour fermentation. Roast level: light "
    "(Agtron 68). Tasting notes: bergamot, jasmine, stone fruit. Brews beautifully as "
    "pour over or espresso."
)


def _meta(*pairs: tuple[str, str]) -> list[dict]:
    return [
        {"namespace": "custom", "key": k, "value": v, "type": "single_line_text_field"}
        for k, v in pairs
    ]


def _codes(result) -> set[str]:
    return {gap.code for gap in result.gaps}


def _spec_attrs(result) -> set[str]:
    return {gap.attribute for gap in result.gaps if gap.code == SPEC_MISSING}


def test_fully_optimized_product_has_no_gaps_and_none_severity():
    result = evaluate_product(
        title="Ethiopia Yirgacheffe 340 g",
        body=RICH_BODY,
        gtin="0123456789012",
        metafields=_meta(("roast", "light")),
        visibility_state="active",
    )
    assert result.gaps == []
    assert result.severity == "none"
    assert result.spec_coverage == 1.0


def test_blank_description_flags_missing_description():
    result = evaluate_product(
        title="Kenya AA",
        body="   ",
        gtin="0123456789012",
        metafields=_meta(("roast", "light")),
        visibility_state="active",
    )
    assert MISSING_DESCRIPTION in _codes(result)


def test_html_only_description_counts_as_missing():
    # The ingest stores descriptionHtml; a body that is only markup carries no text.
    result = evaluate_product(
        title="Kenya AA",
        body="<p><br></p>",
        gtin="0123456789012",
        metafields=_meta(("roast", "light")),
        visibility_state="active",
    )
    assert MISSING_DESCRIPTION in _codes(result)


def test_absent_gtin_flags_missing_gtin():
    result = evaluate_product(
        title="Colombia Huila",
        body=RICH_BODY,
        gtin=None,
        metafields=_meta(("roast", "light")),
        visibility_state="active",
    )
    assert MISSING_GTIN in _codes(result)


def test_empty_metafields_flags_missing_metafields():
    for metafields in (None, []):
        result = evaluate_product(
            title="Colombia Huila",
            body=RICH_BODY,
            gtin="0123456789012",
            metafields=metafields,
            visibility_state="active",
        )
        assert MISSING_METAFIELDS in _codes(result)


def test_non_active_visibility_flags_not_discoverable():
    for state in ("draft", "archived", "unlisted"):
        result = evaluate_product(
            title="House Blend",
            body=RICH_BODY,
            gtin="0123456789012",
            metafields=_meta(("roast", "light")),
            visibility_state=state,
        )
        assert NOT_DISCOVERABLE in _codes(result), state


def test_active_and_null_visibility_are_discoverable():
    for state in ("active", None):
        result = evaluate_product(
            title="House Blend",
            body=RICH_BODY,
            gtin="0123456789012",
            metafields=_meta(("roast", "light")),
            visibility_state=state,
        )
        assert NOT_DISCOVERABLE not in _codes(result), state


def test_missing_single_spec_attribute_is_flagged_precisely():
    # RICH_BODY minus any brew-method language: exactly one spec family should be missing.
    body = (
        "Single-origin washed Arabica from Ethiopia. Altitude 1,900-2,100 masl. "
        "Varietal: Heirloom. Process: washed. Roast level: light. "
        "Tasting notes: bergamot, jasmine."
    )
    result = evaluate_product(
        title="Ethiopia Yirgacheffe",
        body=body,
        gtin="0123456789012",
        metafields=_meta(("roast", "light")),
        visibility_state="active",
    )
    assert _spec_attrs(result) == {"brew_method"}


def test_spec_attribute_satisfied_by_metafield_not_only_body():
    # A thin body but the attribute is carried in a metafield value — still counts as present.
    result = evaluate_product(
        title="Ethiopia Yirgacheffe",
        body="A lovely coffee.",
        gtin="0123456789012",
        metafields=_meta(
            ("roast", "medium roast"),
            ("origin", "single-origin Ethiopia"),
            ("process", "washed"),
            ("variety", "Heirloom varietal"),
            ("notes", "tasting notes: bergamot, jasmine"),
            ("altitude", "2000 masl"),
            ("brew", "pour over and espresso"),
        ),
        visibility_state="active",
    )
    assert _spec_attrs(result) == set()


def test_empty_product_is_high_severity_with_all_spec_families_missing():
    result = evaluate_product(
        title="Mystery Beans",
        body=None,
        gtin=None,
        metafields=None,
        visibility_state="active",
    )
    assert result.severity == "high"
    assert MISSING_DESCRIPTION in _codes(result)
    assert MISSING_GTIN in _codes(result)
    assert MISSING_METAFIELDS in _codes(result)
    # All seven spec families are absent from an empty product.
    assert _spec_attrs(result) == {
        "roast_level",
        "origin",
        "process",
        "variety",
        "tasting_notes",
        "altitude",
        "brew_method",
    }
    assert result.spec_coverage == 0.0


def test_not_discoverable_alone_lands_at_least_medium():
    # A near-perfect product that is merely set to draft is still a real problem.
    result = evaluate_product(
        title="House Blend",
        body=RICH_BODY,
        gtin="0123456789012",
        metafields=_meta(("roast", "light")),
        visibility_state="draft",
    )
    assert _codes(result) == {NOT_DISCOVERABLE}
    assert result.severity == "medium"


def test_result_is_deterministic():
    kwargs = dict(
        title="Colombia Huila",
        body="Medium roast from Colombia.",
        gtin=None,
        metafields=None,
        visibility_state="active",
    )
    first = evaluate_product(**kwargs)
    second = evaluate_product(**kwargs)
    assert first.model_dump() == second.model_dump()
