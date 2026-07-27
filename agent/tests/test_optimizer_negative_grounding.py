"""Negative-grounding suite for the Optimizer (Phase 3, step 2e).

The mirror of ``test_optimizer_grounding.py``. That suite proves a POSITIVE claim (a fill) cannot
be made without literal presence; this one proves a NEGATIVE claim (an "absent" merchant to-do)
cannot be made without literal ABSENCE.

The bug it locks down (run 839, product 113): the Optimizer emitted ``spec:origin`` *"No origin
stated in any source field"* on a product titled **"Ethiopia Yirgacheffe 340 g"** whose audit had
classified origin ``unstructured``. Extraction had flakily missed it — and extraction returning
nothing is not evidence of absence. A false to-do survives the approval gate because it reads as
advice rather than a diff, so it must never reach a merchant.

Two independent defences, tested separately AND together:
  * the deterministic AUDIT state is the authority for absence (``unstructured`` forbids the claim);
  * the LITERAL guard blocks it whenever a ``SPEC_VOCABULARY`` token is in any source field.
Plus deterministic RECOVERY, which turns a missed-but-present family back into a real fix rather
than letting it vanish — so the gap→row accounting invariant is unchanged.
"""

import pytest
from sqlalchemy import select

from app.graph.optimizer import run_optimizer
from app.models import Audit, Fix, FixType, Product, Shop, ShopStatus
from app.services.audit_rubric import SPEC_MISSING, STATE_ABSENT, STATE_UNSTRUCTURED
from app.services.optimizer_llm import AttributeCandidate, ExtractedAttributes

BEANS_CATEGORY = "Food, Beverages & Tobacco > Beverages > Coffee > Coffee Beans & Ground Coffee"

# Product 113's real title — the origin is literally in it, which is what made the to-do false.
TITLE_113 = "Ethiopia Yirgacheffe 340 g"


class ScriptedOptimizerClient:
    """Returns canned candidates; records the (source_fields, targets) it was called with."""

    def __init__(self, candidates: list[AttributeCandidate] | None = None):
        self._candidates = candidates or []
        self.calls: list[tuple[dict, list[str]]] = []

    async def extract(self, source_fields, target_attributes) -> ExtractedAttributes:
        self.calls.append((source_fields, list(target_attributes)))
        return ExtractedAttributes(attributes=self._candidates)


def _spec_gap(attribute: str, state: str) -> dict:
    """An ``audits.gaps_json`` entry exactly as ``run_audit`` persists it (``gap.model_dump()``)."""
    return {
        "code": SPEC_MISSING,
        "attribute": attribute,
        "state": state,
        "detail": f"{attribute} is {state}.",
    }


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain="negative-grounding.myshopify.com", status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _seed(db, shop_id, *, title, body, gaps=None, category=None, metafields=None):
    product = Product(
        shop_id=shop_id, shopify_product_id="gid://shopify/Product/113", title=title, body=body,
        variants_json=[], metafields_json=metafields, visibility_state="active",
        product_type="Coffee", category=category,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    db.add(
        Audit(
            product_id=product.id, product_class="coffee", gaps_json=gaps or [],
            spec_coverage=0.0, severity="high",
        )
    )
    await db.commit()
    return product


async def _fixes(db, product_id):
    return (
        await db.execute(select(Fix).where(Fix.product_id == product_id).order_by(Fix.id))
    ).scalars().all()


def _todo(fixes, attribute):
    matches = [
        f for f in fixes
        if f.type == FixType.merchant_todo and f.target == f"spec:{attribute}"
    ]
    return matches[0] if matches else None


def _description(fixes):
    matches = [f for f in fixes if f.type == FixType.description]
    return matches[0] if matches else None


# --- 1/2. The exact run-839 product-113 regression -------------------------------------------
async def test_113_origin_is_not_a_false_absent_todo_and_routes_to_description(db, shop):
    # audit = unstructured, extraction returns NOTHING for origin. Uncategorized, so no taxonomy
    # channel is available — origin must still surface, as a labeled description line.
    product = await _seed(
        db, shop.id, title=TITLE_113, body="A bright everyday coffee.",
        gaps=[_spec_gap("origin", STATE_UNSTRUCTURED)],
    )
    report = await run_optimizer(db, product.id, ScriptedOptimizerClient())

    fixes = await _fixes(db, product.id)
    assert _todo(fixes, "origin") is None, "the false 'absent' origin to-do must be gone"

    description = _description(fixes)
    assert description is not None
    assert "<li>Origin: Ethiopia</li>" in description.after_json["body_html"]
    # Grounded in the title, with the merchant's own casing — not the lowercase vocabulary entry.
    citation = next(c for c in description.source_json if c["attribute"] == "origin")
    assert citation["source_field"] == "title"
    assert citation["snippet"] == "Ethiopia"

    # Telemetry: recovery rescued a family that would have been a FALSE ABSENCE claim.
    assert [(r.attribute, r.value, r.missed_as) for r in report.recovered] == [
        ("origin", "Ethiopia", "absent")
    ]


async def test_113_origin_routes_to_the_taxonomy_metafield_once_categorized(db, shop):
    # Same miss, but the product has a category — so the recovered value takes the SECONDARY
    # (taxonomy attribute) path and carries the canonical Ethiopia TaxonomyValue GID.
    product = await _seed(
        db, shop.id, title=TITLE_113, body="A bright everyday coffee.",
        gaps=[_spec_gap("origin", STATE_UNSTRUCTURED)], category=BEANS_CATEGORY,
    )
    await run_optimizer(db, product.id, ScriptedOptimizerClient())

    fixes = await _fixes(db, product.id)
    assert _todo(fixes, "origin") is None
    metafield = next(f for f in fixes if f.target == "metafield:shopify.country")
    assert metafield.after_json["taxonomy_value_gid"] == "gid://shopify/TaxonomyValue/8882"
    assert metafield.after_json["value"] == "Ethiopia"
    # De-dup holds: the taxonomy metafield IS origin's structured pair, so no description line.
    description = _description(fixes)
    assert description is None or "Origin" not in description.after_json["body_html"]


# --- 3. The literal guard, with NO audit state available --------------------------------------
async def test_literal_guard_blocks_the_absent_claim_without_any_audit_state(db, shop):
    # gaps_json is EMPTY, so the audit authority is unavailable — the literal guard alone must
    # still refuse to claim absence for a family whose token is sitting in a source field.
    product = await _seed(db, shop.id, title="Colombia Huila", body="A coffee.", gaps=[])
    await run_optimizer(db, product.id, ScriptedOptimizerClient())

    todo = _todo(await _fixes(db, product.id), "origin")
    assert todo is None  # recovered to a description line instead


async def test_literal_guard_downgrades_rather_than_deletes_when_no_value_is_readable(db, shop):
    # "single-origin" is a MENTION of the family but not an origin VALUE. The to-do must survive
    # (the merchant really does need to act) with a truthful claim, not the false absence one.
    product = await _seed(
        db, shop.id, title="House Blend", body="Single-origin Arabica beans.", gaps=[]
    )
    await run_optimizer(db, product.id, ScriptedOptimizerClient())

    todo = _todo(await _fixes(db, product.id), "origin")
    assert todo is not None
    assert "No origin stated in any source field" not in todo.reason
    assert "referred to in body_html" in todo.reason
    # Quoted back as the MERCHANT wrote it, not as the vocabulary spells it ("single origin").
    assert "'Single-origin'" in todo.reason
    evidence = todo.source_json[0]
    assert evidence["drop_reason"] == "mentioned_no_value"
    assert evidence["detected_phrase"] == "Single-origin"
    assert evidence["source_field"] == "body_html"


# --- 4. The audit gate, with NO literal token available ---------------------------------------
async def test_audit_unstructured_blocks_the_absent_claim_even_with_no_live_token(db, shop):
    # A STALE audit: it recorded origin as unstructured, but the product has since been edited and
    # no origin token remains. The audit only ever RAISES the bar — the absence claim stays
    # forbidden (the conservative direction), and the to-do cites the audit instead.
    product = await _seed(
        db, shop.id, title="Some Coffee", body="A coffee.",
        gaps=[_spec_gap("origin", STATE_UNSTRUCTURED)],
    )
    await run_optimizer(db, product.id, ScriptedOptimizerClient())

    todo = _todo(await _fixes(db, product.id), "origin")
    assert todo is not None
    assert "No origin stated in any source field" not in todo.reason
    assert "The audit found origin stated in this product's description" in todo.reason
    evidence = todo.source_json[0]
    assert evidence["drop_reason"] == "mentioned_no_value"
    assert evidence["audit_state"] == STATE_UNSTRUCTURED
    assert evidence["detected_phrase"] is None


# --- 5. Recovery runs for EVERY extraction failure, not just a null return ---------------------
async def test_recovery_also_rescues_a_fabrication_drop(db, shop):
    # The model asserted a roast that is not in source (dropped as fabrication). The old code then
    # claimed "no verified roast level was found in your product data" — on a body that states one.
    product = await _seed(
        db, shop.id, title="Some Coffee", body="Roast level: light.",
        gaps=[_spec_gap("roast_level", STATE_UNSTRUCTURED)], category=BEANS_CATEGORY,
    )
    client = ScriptedOptimizerClient([
        AttributeCandidate(attribute="roast_level", value="dark", source_field="body_html",
                           snippet="Roast level: dark", ambiguous=False)
    ])
    report = await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert _todo(fixes, "roast_level") is None
    # The fabricated "dark" was still dropped by the guard; the recovered "light" is what shipped.
    assert [d.value for d in report.dropped] == ["dark"]
    assert [(r.attribute, r.value, r.missed_as) for r in report.recovered] == [
        ("roast_level", "light", "fabrication")
    ]
    metafield = next(f for f in fixes if f.target == "metafield:shopify.coffee-roast")
    assert metafield.after_json["value"] == "Light"


# --- 6. No over-correction: a genuinely absent family STILL gets its to-do ---------------------
@pytest.mark.parametrize("family", ["brew_method", "coffee_product_form", "altitude", "variety"])
async def test_a_genuinely_absent_family_still_emits_the_absent_todo(db, shop, family):
    # Product 113's own genuinely-absent families. Nothing in any source field, audit says absent →
    # the truthful absence claim is exactly right and must survive the fix.
    product = await _seed(
        db, shop.id, title=TITLE_113, body="A bright everyday coffee.",
        gaps=[_spec_gap(family, STATE_ABSENT)],
    )
    await run_optimizer(db, product.id, ScriptedOptimizerClient())

    todo = _todo(await _fixes(db, product.id), family)
    assert todo is not None
    assert todo.reason == (
        f"No {family.replace('_', ' ')} stated in any source field; a merchant must add it."
    )
    assert todo.source_json is None  # SQL NULL is reserved for a TRUE absence claim
    assert todo.after_json is None


async def test_a_weight_is_never_recovered_as_an_altitude(db, shop):
    # "340 g" is a WEIGHT. Neither the presence gate (no altitude token) nor the format validator
    # would accept it — the family stays a truthful absence.
    product = await _seed(db, shop.id, title=TITLE_113, body="A coffee.", gaps=[])
    await run_optimizer(db, product.id, ScriptedOptimizerClient())

    fixes = await _fixes(db, product.id)
    assert "340" not in (_description(fixes).after_json["body_html"] if _description(fixes) else "")
    assert "No altitude stated in any source field" in _todo(fixes, "altitude").reason


async def test_a_real_elevation_is_recovered_as_a_WHOLE_range(db, shop):
    # The span must cover the range. Recovering only "2,100 masl" would be literally present and
    # would validate, yet it reports the top of a range as *the* altitude — a narrowing of the
    # merchant's own copy, and not safe to publish.
    product = await _seed(
        db, shop.id, title="Some Coffee", body="Grown at 1,900-2,100 masl.", gaps=[]
    )
    await run_optimizer(db, product.id, ScriptedOptimizerClient())

    fixes = await _fixes(db, product.id)
    assert _todo(fixes, "altitude") is None
    assert "<li>Altitude: 1,900-2,100 masl</li>" in _description(fixes).after_json["body_html"]


# --- 7. Cross-family ambiguity: never trade a false negative for a false positive --------------
async def test_an_ambiguous_token_is_never_recovered_for_either_family(db, shop):
    # "espresso" is BOTH a roast level and a brew method. A token-level scan cannot tell which is
    # meant, so recovery refuses it for both — an "Espresso Roast" product must NOT gain a
    # fabricated "Brew Method: espresso" line. Both fall to the truthful mentioned_no_value tier.
    product = await _seed(db, shop.id, title="Espresso Roast", body="A dark, syrupy cup.", gaps=[])
    await run_optimizer(db, product.id, ScriptedOptimizerClient())

    fixes = await _fixes(db, product.id)
    body_html = _description(fixes).after_json["body_html"] if _description(fixes) else ""
    assert "Brew Method: espresso" not in body_html.replace("Espresso", "espresso")

    brew_todo = _todo(fixes, "brew_method")
    assert brew_todo is not None
    assert "No brew method stated in any source field" not in brew_todo.reason
    assert brew_todo.source_json[0]["drop_reason"] == "mentioned_no_value"
    # "dark" is unambiguous, so roast_level DOES recover from the body.
    assert _todo(fixes, "roast_level") is None


# --- 8. Open-kind families are never given an invented boundary -------------------------------
async def test_tasting_notes_is_never_recovered_deterministically(db, shop):
    # tasting_notes has no closed vocabulary — any span picked here would be an invented boundary.
    # It must fall to the truthful "mentioned, no value" tier, never a fabricated line.
    product = await _seed(
        db, shop.id, title="Some Coffee", body="Tasting notes: bergamot and jasmine.", gaps=[]
    )
    await run_optimizer(db, product.id, ScriptedOptimizerClient())

    fixes = await _fixes(db, product.id)
    body_html = _description(fixes).after_json["body_html"] if _description(fixes) else ""
    assert "Tasting Notes:" not in body_html
    todo = _todo(fixes, "tasting_notes")
    assert todo.source_json[0]["drop_reason"] == "mentioned_no_value"
    assert "No tasting notes stated in any source field" not in todo.reason


# --- 9. The gap→row accounting invariant still partitions the targets --------------------------
async def test_accounting_invariant_holds_across_all_three_outcomes(db, shop):
    # One product exercising every route at once: a recovered taxonomy fill (origin), a recovered
    # description line (process), a mentioned_no_value to-do (tasting_notes) and true absences.
    # ``run_optimizer``'s own assertion fires on a routing hole; this asserts the partition too.
    product = await _seed(
        db, shop.id, title=TITLE_113, body="Washed process. Tasting notes: bergamot.",
        gaps=[_spec_gap("origin", STATE_UNSTRUCTURED), _spec_gap("brew_method", STATE_ABSENT)],
        category=BEANS_CATEGORY,
    )
    client = ScriptedOptimizerClient()
    await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    _, targets = client.calls[0]

    meta_fams = {"origin"}
    assert {f.target for f in fixes if f.type == FixType.metafield} == {
        "metafield:shopify.country"
    }
    desc_fams = {c["attribute"] for c in _description(fixes).source_json}
    todo_fams = {
        f.target.removeprefix("spec:")
        for f in fixes
        if f.type == FixType.merchant_todo and f.target.startswith("spec:")
    }

    assert "process" in desc_fams
    assert "tasting_notes" in todo_fams
    assert meta_fams | desc_fams | todo_fams == set(targets)
    assert meta_fams.isdisjoint(desc_fams)
    assert meta_fams.isdisjoint(todo_fams)
    assert desc_fams.isdisjoint(todo_fams)

    # And every spec to-do that remains makes a claim the node can defend.
    for todo in (f for f in fixes if f.type == FixType.merchant_todo):
        if not todo.target.startswith("spec:"):
            continue
        if "stated in any source field" in todo.reason:
            assert todo.source_json is None  # a TRUE absence carries no evidence to cite
