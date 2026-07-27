"""The Optimizer node ``run_optimizer`` (Phase 3, step 2 + 2d re-architecture).

DB-backed (real Postgres ``db`` fixture). A scripted ``OptimizerClient`` stands in for the LLM (no
network); the real grounding guard runs. Step 2d routing (after the 2c write-target spike):

* PRIMARY — a grounded non-body attribute → a **description** fix (all families).
* SECONDARY — a grounded taxonomy-home family whose value maps to a canonical taxonomy value, on a
  product WITH a category → a **taxonomy** metafield fix (``shopify`` namespace). No ``custom.*``.
* NEW — a coffee product without a category → an approval-gated **category** fix.
* absent/ambiguous/hallucinated → merchant to-dos or dropped; ``missing_gtin`` → always a to-do.
"""

import pytest
from sqlalchemy import func, select, text

from app.graph.optimizer import run_optimizer
from app.models import (
    AgentRun,
    AgentRunStatus,
    Audit,
    Fix,
    FixStatus,
    FixType,
    Product,
    Shop,
    ShopStatus,
)
from app.models import QueryPanel as QueryPanelRow
from app.services.optimizer_llm import AttributeCandidate, ExtractedAttributes

# A real Standard-taxonomy category fullName (what ingest stores once a category is assigned).
BEANS_CATEGORY = "Food, Beverages & Tobacco > Beverages > Coffee > Coffee Beans & Ground Coffee"

# The 8 spec families applicable to a non-decaf coffee (decaffeination_method is decaf-only).
_APPLICABLE = (
    "roast_level", "origin", "process", "variety", "tasting_notes", "altitude", "brew_method",
    "coffee_product_form",
)


class ScriptedOptimizerClient:
    """Returns canned candidates; records the (source_fields, targets) it was called with."""

    def __init__(self, candidates: list[AttributeCandidate]):
        self._candidates = candidates
        self.calls: list[tuple[dict, list[str]]] = []

    async def extract(self, source_fields, target_attributes) -> ExtractedAttributes:
        self.calls.append((source_fields, list(target_attributes)))
        return ExtractedAttributes(attributes=self._candidates)


def _gap(code: str, attribute: str | None = None) -> dict:
    return {"code": code, "attribute": attribute, "detail": f"{code} {attribute or ''}".strip()}


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain="optimizer-test.myshopify.com", status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _seed(db, shop_id, *, gaps=None, body="A coffee.", metafields=None, product_type="Coffee",
                variants=None, severity="high", title="Some Coffee", category=None):
    product = Product(
        shop_id=shop_id, shopify_product_id="gid://shopify/Product/1", title=title,
        body=body, variants_json=variants or [], metafields_json=metafields,
        visibility_state="active", product_type=product_type, category=category,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    audit = Audit(
        product_id=product.id, product_class="coffee", gaps_json=gaps or [],
        spec_coverage=0.0, severity=severity,
    )
    db.add(audit)
    await db.commit()
    return product


async def _fixes(db, product_id):
    return (
        await db.execute(select(Fix).where(Fix.product_id == product_id).order_by(Fix.id))
    ).scalars().all()


def _by_type(fixes, fix_type):
    return [f for f in fixes if f.type == fix_type]


# --- SECONDARY: taxonomy metafield fills (the write-target retarget) --------------------------
async def test_grounded_taxonomy_family_with_a_category_becomes_a_taxonomy_metafield(db, shop):
    # roast grounds from the body, product is categorized, "light" maps to a canonical taxonomy
    # value → a shopify-namespace taxonomy metafield carrying the TaxonomyValue GID (spike L1).
    product = await _seed(
        db, shop.id, body="Roast level: light (Agtron 68).", category=BEANS_CATEGORY,
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="light", source_field="body_html",
                            snippet="Roast level: light", ambiguous=False)]
    )

    report = await run_optimizer(db, product.id, client)

    meta = _by_type(await _fixes(db, product.id), FixType.metafield)
    assert len(meta) == 1
    fix = meta[0]
    assert fix.status == FixStatus.proposed
    assert fix.target == "metafield:shopify.coffee-roast"
    assert fix.after_json == {
        "namespace": "shopify", "key": "coffee-roast", "type": "list.metaobject_reference",
        "taxonomy_value_gid": "gid://shopify/TaxonomyValue/19313", "value": "Light",
    }
    assert fix.source_json[0]["source_field"] == "body_html"
    assert report.fillable >= 1


async def test_no_custom_metafield_is_ever_written(db, shop):
    # The whole point of 2d: the retired custom.* channel must never be emitted.
    product = await _seed(
        db, shop.id, body="Roast level: light.", category=BEANS_CATEGORY,
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="light", source_field="body_html",
                            snippet="Roast level: light", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    for fix in await _fixes(db, product.id):
        assert (fix.after_json or {}).get("namespace") != "custom"
        assert "custom." not in (fix.target or "")


async def test_taxonomy_fill_is_gated_on_an_assigned_category(db, shop):
    # Same grounded roast, but NO category → no taxonomy metafield (the dependency chain); the value
    # is still grounded, so it is NOT a to-do — it is legible via the description path.
    product = await _seed(
        db, shop.id, body="<p>A coffee.</p>", category=None,
        variants=[{"title": "Roast: light"}],
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="light", source_field="variants_json",
                            snippet="Roast: light", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert _by_type(fixes, FixType.metafield) == []          # gated: no category yet
    assert len(_by_type(fixes, FixType.description)) == 1     # but legible via description
    assert not any(f.target == "spec:roast_level" for f in fixes)  # not a to-do — it grounded


async def test_grounded_value_not_in_the_taxonomy_falls_to_description_only(db, shop):
    # "Yirgacheffe" grounds+validates as an origin, but it is a REGION, not a taxonomy Country, so
    # it does not map → no taxonomy metafield even with a category; description carries it. Never a
    # fabricated taxonomy value.
    product = await _seed(
        db, shop.id, body="<p>A coffee.</p>", category=BEANS_CATEGORY,
        variants=[{"title": "Origin: Yirgacheffe"}],
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="origin", value="Yirgacheffe", source_field="variants_json",
                            snippet="Origin: Yirgacheffe", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert _by_type(fixes, FixType.metafield) == []
    assert len(_by_type(fixes, FixType.description)) == 1


async def test_country_origin_with_category_becomes_a_taxonomy_metafield(db, shop):
    product = await _seed(
        db, shop.id, body="Single-origin Ethiopia, washed.", category=BEANS_CATEGORY,
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="origin", value="Ethiopia", source_field="body_html",
                            snippet="Single-origin Ethiopia", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    fix = _by_type(await _fixes(db, product.id), FixType.metafield)[0]
    assert fix.target == "metafield:shopify.country"
    assert fix.after_json["taxonomy_value_gid"] == "gid://shopify/TaxonomyValue/8882"
    assert fix.after_json["value"] == "Ethiopia"


# --- NEW: the category fix (approval-gated precondition) --------------------------------------
async def test_uncategorized_coffee_gets_an_approval_gated_category_fix(db, shop):
    product = await _seed(db, shop.id, body="A washed Ethiopian coffee.", category=None)
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    cat = _by_type(await _fixes(db, product.id), FixType.category)
    assert len(cat) == 1
    assert cat[0].status == FixStatus.proposed
    assert cat[0].target == "category"
    assert cat[0].after_json["category"] == "gid://shopify/TaxonomyCategory/fb-1-3-1"
    assert cat[0].before_json == {"category": None}
    assert cat[0].source_json[0]["attribute"] == "category"


async def test_concentrate_maps_to_the_concentrate_category(db, shop):
    product = await _seed(
        db, shop.id, title="Cold Brew Concentrate", body="A coffee concentrate.", category=None,
    )
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    cat = _by_type(await _fixes(db, product.id), FixType.category)[0]
    assert cat.after_json["category"] == "gid://shopify/TaxonomyCategory/fb-1-3-5"


async def test_categorized_coffee_gets_no_category_fix(db, shop):
    product = await _seed(db, shop.id, body="A coffee.", category=BEANS_CATEGORY)
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    assert _by_type(await _fixes(db, product.id), FixType.category) == []


async def test_uncategorized_is_not_a_taxonomy_category():
    from app.graph.optimizer import has_taxonomy_category

    assert has_taxonomy_category(None) is False
    assert has_taxonomy_category("Uncategorized") is False
    assert has_taxonomy_category(BEANS_CATEGORY) is True


# --- to-dos, drops, gtin (guards unchanged) --------------------------------------------------
async def test_absent_spec_becomes_a_merchant_todo(db, shop):
    product = await _seed(db, shop.id, body="A pleasant everyday coffee.", category=BEANS_CATEGORY)
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="altitude", value=None, source_field=None, snippet=None,
                            ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    todos = [f for f in await _fixes(db, product.id) if f.target == "spec:altitude"]
    assert len(todos) == 1
    assert todos[0].type == FixType.merchant_todo
    assert todos[0].after_json is None
    assert todos[0].reason


async def test_hallucinated_candidate_is_dropped_not_emitted(db, shop):
    product = await _seed(db, shop.id, body="Roast level: light", category=BEANS_CATEGORY)
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="dark", source_field="body_html",
                            snippet="Roast level: dark", ambiguous=False)]
    )

    report = await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert not any((f.after_json or {}).get("value") == "dark" for f in fixes)
    assert report.dropped and report.dropped[0].value == "dark"
    assert report.dropped[0].reason == "fabrication"


async def test_mis_assigned_value_is_dropped_and_gap_still_becomes_a_todo(db, shop):
    body = "Single-origin washed Arabica. Process: washed."
    product = await _seed(db, shop.id, body=body, category=BEANS_CATEGORY)
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="brew_method", value="washed", source_field="body_html",
                            snippet="Process: washed", ambiguous=False)]
    )

    report = await run_optimizer(db, product.id, client)

    todo = [f for f in await _fixes(db, product.id) if f.target == "spec:brew_method"][0]
    assert todo.type == FixType.merchant_todo
    assert report.dropped and report.dropped[0].reason == "mis_assignment"
    assert "did not validate as brew method" in todo.reason
    assert todo.source_json[0]["drop_reason"] == "mis_assignment"
    assert todo.source_json[0]["rejected_value"] == "washed"


async def test_absent_todo_has_sql_null_source_and_after_json(db, shop):
    product = await _seed(db, shop.id, body="A coffee.", category=BEANS_CATEGORY)
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="altitude", value=None, source_field=None, snippet=None,
                            ambiguous=False)]
    )
    await run_optimizer(db, product.id, client)
    todo = [f for f in await _fixes(db, product.id) if f.target == "spec:altitude"][0]
    assert "No altitude stated in any source field" in todo.reason

    row = (
        await db.execute(
            text(
                "SELECT (source_json IS NULL) AS src_null, (after_json IS NULL) AS after_null, "
                "(after_json = 'null'::jsonb) AS after_jsonnull FROM fixes WHERE id = :id"
            ),
            {"id": todo.id},
        )
    ).one()
    assert row.src_null is True
    assert row.after_null is True
    assert row.after_jsonnull is None


async def test_missing_gtin_is_always_a_todo_and_never_carries_a_barcode(db, shop):
    product = await _seed(db, shop.id, gaps=[_gap("missing_gtin")], body="A coffee.",
                          category=BEANS_CATEGORY)
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    gtin_fixes = [f for f in await _fixes(db, product.id) if f.target == "gtin"]
    assert len(gtin_fixes) == 1
    assert gtin_fixes[0].type == FixType.merchant_todo
    assert gtin_fixes[0].after_json is None


# --- PRIMARY: the description path (mechanism unchanged, non-body gated) ----------------------
async def test_body_resident_family_with_no_taxonomy_fill_is_surfaced_in_description(db, shop):
    # THE 2d ROUTING-HOLE REGRESSION. roast grounds from the BODY with no category, so no taxonomy
    # fill fires — but it has no structured pair, so it must still be surfaced as a labeled Details
    # line (a labeled pair beats a spec buried in prose). Before this fix it produced nothing.
    product = await _seed(db, shop.id, body="Roast: light.", category=None)
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="light", source_field="body_html",
                            snippet="Roast: light", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert _by_type(fixes, FixType.metafield) == []            # no category → no taxonomy fill
    desc = _by_type(fixes, FixType.description)
    assert len(desc) == 1
    assert "Roast Level: light" in desc[0].after_json["body_html"]
    assert not any(f.target == "spec:roast_level" for f in fixes)  # grounded → not a to-do


async def test_taxonomy_filled_family_is_excluded_from_the_description(db, shop):
    # De-dup: a family that got its structured taxonomy metafield must NOT also be re-appended as a
    # Details line — the metafield IS its structured pair.
    product = await _seed(db, shop.id, body="Roast level: light.", category=BEANS_CATEGORY)
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="light", source_field="body_html",
                            snippet="Roast level: light", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert len(_by_type(fixes, FixType.metafield)) == 1
    assert _by_type(fixes, FixType.description) == []  # roast is the only grounded family → no desc


async def test_every_spec_target_is_accounted_for_by_exactly_one_route(db, shop):
    # The gap->row invariant the routing hole violated: taxonomy-home resolvable -> metafield;
    # taxonomy-less grounded -> description; absent -> to-do; and the three sets PARTITION the
    # targets (union == targets, pairwise disjoint). The node's own assertion also guards this.
    product = await _seed(
        db, shop.id, body="Roast level: light. Process: washed.", category=BEANS_CATEGORY,
    )
    client = ScriptedOptimizerClient([
        AttributeCandidate(attribute="roast_level", value="light", source_field="body_html",
                           snippet="Roast level: light", ambiguous=False),
        AttributeCandidate(attribute="process", value="washed", source_field="body_html",
                           snippet="Process: washed", ambiguous=False),
    ])

    await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    _, targets = client.calls[0]

    metafield_fams = {"roast_level"}  # the only taxonomy-home resolvable candidate
    meta_targets = {f.target for f in _by_type(fixes, FixType.metafield)}
    assert meta_targets == {"metafield:shopify.coffee-roast"}
    desc = _by_type(fixes, FixType.description)
    assert len(desc) == 1
    desc_fams = {c["attribute"] for c in desc[0].source_json}
    assert desc_fams == {"process"}  # taxonomy-less grounded → description; roast excluded (filled)
    todo_fams = {
        f.target.removeprefix("spec:")
        for f in fixes
        if f.type == FixType.merchant_todo and f.target.startswith("spec:")
    }

    # Partition over the Optimizer's own targets.
    assert metafield_fams | desc_fams | todo_fams == set(targets)
    assert metafield_fams.isdisjoint(desc_fams)
    assert metafield_fams.isdisjoint(todo_fams)
    assert desc_fams.isdisjoint(todo_fams)


RICH_HTML = (
    "<h2>Our Signature Roast</h2>"
    '<p>Crafted with <strong>care</strong>. Read our '
    '<a href="https://example.test/story">story</a>.</p>'
    "<ul><li>Small batch</li><li>Fair trade</li></ul>"
)


async def test_non_body_grounded_yields_a_description_fix(db, shop):
    # process lives in variants_json (non-body) → surfaced into the description. No taxonomy home,
    # so it is description-only regardless of category.
    product = await _seed(
        db, shop.id, body="<p>A nice coffee.</p>", category=BEANS_CATEGORY,
        variants=[{"title": "Process: washed"}],
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="process", value="washed", source_field="variants_json",
                            snippet="Process: washed", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    desc = _by_type(await _fixes(db, product.id), FixType.description)
    assert len(desc) == 1
    assert desc[0].after_json["body_html"].startswith(desc[0].before_json["body_html"])
    assert "washed" in desc[0].after_json["body_html"]


async def test_description_fix_preserves_html_verbatim(db, shop):
    product = await _seed(
        db, shop.id, body=RICH_HTML, category=None, variants=[{"title": "Process: washed"}],
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="process", value="washed", source_field="variants_json",
                            snippet="Process: washed", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    desc = _by_type(await _fixes(db, product.id), FixType.description)[0]
    after = desc.after_json["body_html"]
    assert desc.before_json["body_html"] == RICH_HTML
    assert after.startswith(RICH_HTML)  # merchant's markup preserved byte-for-byte
    for tag in ("<h2>", "</h2>", "<strong>", "</strong>",
                '<a href="https://example.test/story">', "</a>", "<ul>", "<li>Small batch</li>"):
        assert tag in after
    appended = after[len(RICH_HTML):]
    assert appended == "<p><strong>Details</strong></p><ul><li>Process: washed</li></ul>"
    assert "\n" not in appended


async def test_composed_description_carries_only_grounded_values(db, shop):
    # Gate H for the primary path: a hallucinated candidate is dropped before composition, so it
    # never reaches the description block.
    product = await _seed(
        db, shop.id, body="<p>A coffee.</p>", category=None,
        variants=[{"title": "Process: washed"}],
    )
    client = ScriptedOptimizerClient([
        AttributeCandidate(attribute="process", value="washed", source_field="variants_json",
                           snippet="Process: washed", ambiguous=False),
        # hallucinated: "dark" is nowhere in source → dropped, must not appear in the block.
        AttributeCandidate(attribute="roast_level", value="dark", source_field="variants_json",
                           snippet="Roast: dark", ambiguous=False),
    ])

    await run_optimizer(db, product.id, client)

    desc = _by_type(await _fixes(db, product.id), FixType.description)[0]
    assert "washed" in desc.after_json["body_html"]
    assert "dark" not in desc.after_json["body_html"]


# --- targeting: structural, applicability-gated (the decoupling) -----------------------------
async def test_targeting_covers_the_applicable_families(db, shop):
    product = await _seed(db, shop.id, body="Roast level: light.", category=BEANS_CATEGORY)
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    _, targets = client.calls[0]
    assert sorted(targets) == sorted(_APPLICABLE)  # 8, decaffeination_method excluded (not decaf)


async def test_decaf_product_is_targeted_for_its_decaffeination_method(db, shop):
    product = await _seed(db, shop.id, title="Decaf", body="Decaf, Swiss Water process.",
                          category=BEANS_CATEGORY)
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    _, targets = client.calls[0]
    assert "decaffeination_method" in targets


async def test_a_taxonomy_structured_family_is_not_targeted(db, shop):
    product = await _seed(
        db, shop.id, body="A coffee.", category=BEANS_CATEGORY,
        metafields=[{"namespace": "shopify", "key": "coffee-roast", "value": "Light"}],
    )
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    _, targets = client.calls[0]
    assert "roast_level" not in targets


async def test_a_custom_metafield_does_not_remove_a_family_from_targets(db, shop):
    # The step-2d flip: custom.* is not structured, so a family filed under it is STILL targeted
    # (to be re-routed to the taxonomy channel).
    product = await _seed(
        db, shop.id, body="A coffee.", category=BEANS_CATEGORY,
        metafields=[{"namespace": "custom", "key": "roast_level", "value": "Light"}],
    )
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    _, targets = client.calls[0]
    assert "roast_level" in targets


async def test_non_coffee_class_is_never_asked_for_coffee_families(db, shop):
    product = await _seed(db, shop.id, gaps=[_gap("missing_gtin")], product_type="Brewing Gear")
    audit = (
        await db.execute(select(Audit).where(Audit.product_id == product.id))
    ).scalar_one()
    audit.product_class = "equipment"
    await db.commit()

    client = ScriptedOptimizerClient([])
    report = await run_optimizer(db, product.id, client)

    assert client.calls == []  # no extraction call at all
    fixes = await _fixes(db, product.id)
    assert [f.target for f in fixes] == ["gtin"]  # no category fix for non-coffee, either
    assert report.fillable == 0


async def test_optimizer_converges_once_a_taxonomy_metafield_is_published(db, shop):
    body = "Roast level: light (Agtron 68)."
    product = await _seed(db, shop.id, body=body, category=BEANS_CATEGORY)
    candidate = AttributeCandidate(
        attribute="roast_level", value="light", source_field="body_html",
        snippet="Roast level: light", ambiguous=False,
    )

    first = await run_optimizer(db, product.id, ScriptedOptimizerClient([candidate]))
    assert first.fillable >= 1

    # Simulate the Step-4 publish: the taxonomy attribute now exists on the product.
    product.metafields_json = [
        *(product.metafields_json or []),
        {"namespace": "shopify", "key": "coffee-roast", "value": "Light"},
    ]
    await db.commit()

    client = ScriptedOptimizerClient([candidate])
    await run_optimizer(db, product.id, client)

    _, targets = client.calls[0]
    assert "roast_level" not in targets  # structured now → dropped from targets


# --- plumbing (unchanged) --------------------------------------------------------------------
async def test_run_id_is_stamped(db, shop):
    product = await _seed(db, shop.id, body="A coffee.", category=BEANS_CATEGORY)
    panel = QueryPanelRow(shop_id=shop.id, category="coffee", queries_json=[{"text": "q"}],
                          fingerprint="fp")
    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    run = AgentRun(shop_id=shop.id, panel_id=panel.id, status=AgentRunStatus.running)
    db.add(run)
    await db.commit()
    await db.refresh(run)

    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="altitude", value=None, source_field=None, snippet=None,
                            ambiguous=False)]
    )
    await run_optimizer(db, product.id, client, run_id=run.id)

    fixes = await _fixes(db, product.id)
    assert fixes and all(f.run_id == run.id for f in fixes)


async def test_no_audit_raises(db, shop):
    product = Product(shop_id=shop.id, shopify_product_id="gid://shopify/Product/9",
                      title="X", visibility_state="active", product_type="Coffee")
    db.add(product)
    await db.commit()
    await db.refresh(product)

    with pytest.raises(ValueError):
        await run_optimizer(db, product.id, ScriptedOptimizerClient([]))


async def test_excluded_product_produces_no_fixes(db, shop):
    product = await _seed(db, shop.id, severity="not_audited")
    report = await run_optimizer(db, product.id, ScriptedOptimizerClient([]))

    count = await db.scalar(
        select(func.count()).select_from(Fix).where(Fix.product_id == product.id)
    )
    assert count == 0
    assert report.fillable == 0 and report.todos == 0


async def test_unknown_product_raises(db):
    with pytest.raises(ValueError):
        await run_optimizer(db, 999999, ScriptedOptimizerClient([]))
