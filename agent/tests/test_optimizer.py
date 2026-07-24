"""The Optimizer node ``run_optimizer`` (Phase 3, step 2).

DB-backed (real Postgres ``db`` fixture). A scripted ``OptimizerClient`` stands in for the LLM (no
network); the real grounding guard runs. Asserts the emitted ``fixes`` rows: grounded attributes →
metafield fixes with a source citation; absent/ambiguous/hallucinated → merchant to-dos or dropped;
``missing_gtin`` → always a to-do carrying no barcode.
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


_ALL_FAMILIES = (
    "roast_level", "origin", "process", "variety", "tasting_notes", "altitude", "brew_method",
)


def _only_target(family: str) -> list[dict]:
    """Metafields for every family EXCEPT ``family``.

    Targeting is structural (step 2b): the Optimizer asks for every family the product does not
    already carry as a metafield. Marking the other six structured is how a test isolates one
    target — and it exercises the real targeting path rather than working around it.
    """
    return [
        {"namespace": "custom", "key": f, "value": "already structured"}
        for f in _ALL_FAMILIES
        if f != family
    ]


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain="optimizer-test.myshopify.com", status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _seed(db, shop_id, *, gaps, body="A coffee.", metafields=None, product_type="Coffee",
                variants=None, severity="high"):
    product = Product(
        shop_id=shop_id, shopify_product_id="gid://shopify/Product/1", title="Some Coffee",
        body=body, variants_json=variants or [], metafields_json=metafields,
        visibility_state="active", product_type=product_type,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    audit = Audit(
        product_id=product.id, product_class="coffee", gaps_json=gaps,
        spec_coverage=0.0, severity=severity,
    )
    db.add(audit)
    await db.commit()
    return product


async def _fixes(db, product_id):
    return (
        await db.execute(select(Fix).where(Fix.product_id == product_id).order_by(Fix.id))
    ).scalars().all()


async def test_grounded_spec_becomes_a_metafield_fix_with_citation(db, shop):
    body = "Roast level: light (Agtron 68)."
    product = await _seed(
        db, shop.id, gaps=[_gap("spec_missing", "roast_level")], body=body,
        metafields=_only_target("roast_level"),
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(
            attribute="roast_level", value="light", source_field="body_html",
            snippet="Roast level: light", ambiguous=False,
        )]
    )

    report = await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert len(fixes) == 1
    fix = fixes[0]
    assert fix.type == FixType.metafield
    assert fix.status == FixStatus.proposed
    assert fix.after_json["value"] == "light"
    assert fix.after_json["key"] == "roast_level"
    assert fix.source_json[0]["source_field"] == "body_html"
    assert "light" in fix.source_json[0]["snippet"]
    assert fix.base_source_hash
    assert report.fillable == 1 and report.todos == 0


async def test_absent_spec_becomes_a_merchant_todo(db, shop):
    product = await _seed(
        db, shop.id, gaps=[_gap("spec_missing", "altitude")], body="A pleasant everyday coffee.",
        metafields=_only_target("altitude"),
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="altitude", value=None, source_field=None, snippet=None,
                            ambiguous=False)]
    )

    report = await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert len(fixes) == 1
    assert fixes[0].type == FixType.merchant_todo
    assert fixes[0].after_json is None
    assert fixes[0].reason
    assert report.fillable == 0 and report.todos == 1


async def test_hallucinated_candidate_is_dropped_not_emitted(db, shop):
    # Model claims "dark" from a snippet that isn't in the source → guard drops it → to-do, and
    # the drop is recorded for observability.
    product = await _seed(
        db, shop.id, gaps=[_gap("spec_missing", "roast_level")], body="Roast level: light",
        metafields=_only_target("roast_level"),
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="dark", source_field="body_html",
                            snippet="Roast level: dark", ambiguous=False)]
    )

    report = await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert all(f.type == FixType.merchant_todo for f in fixes)
    assert not any((f.after_json or {}).get("value") == "dark" for f in fixes)
    assert report.dropped and report.dropped[0].value == "dark"
    assert report.dropped[0].reason == "fabrication"


async def test_mis_assigned_value_is_dropped_and_gap_still_becomes_a_todo(db, shop):
    # The observed defect: "washed" is literally in the body (a process term) but the model
    # grounds it onto brew_method. Present in source, invalid for the family → mis-assignment.
    body = "Single-origin washed Arabica. Process: washed."
    product = await _seed(
        db, shop.id, gaps=[_gap("spec_missing", "brew_method")], body=body,
        metafields=_only_target("brew_method"),
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="brew_method", value="washed", source_field="body_html",
                            snippet="Process: washed", ambiguous=False)]
    )

    report = await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    # No metafield fix; the gap did NOT vanish — it is a merchant to-do.
    assert [f.type for f in fixes] == [FixType.merchant_todo]
    assert fixes[0].target == "spec:brew_method"
    assert report.fillable == 0 and report.todos == 1
    assert report.dropped and report.dropped[0].reason == "mis_assignment"
    # The distinction is PERSISTED, not only in the in-memory report — and truthful (not "not
    # stated"): the value was found, it just didn't validate.
    todo = fixes[0]
    assert "did not validate as brew method" in todo.reason
    assert "not stated" not in todo.reason
    assert todo.source_json[0]["drop_reason"] == "mis_assignment"
    assert todo.source_json[0]["rejected_value"] == "washed"
    assert todo.source_json[0]["source_field"] == "body_html"


async def test_absent_todo_has_sql_null_source_and_after_json(db, shop):
    product = await _seed(
        db, shop.id, gaps=[_gap("spec_missing", "altitude")], body="A coffee.",
        metafields=_only_target("altitude"),
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="altitude", value=None, source_field=None, snippet=None,
                            ambiguous=False)]
    )
    await run_optimizer(db, product.id, client)
    todo = (await _fixes(db, product.id))[0]
    assert "No altitude stated in any source field" in todo.reason

    # SQL NULL, not JSONB 'null' — so `WHERE ... IS NOT NULL` EXCLUDES to-dos. The after_json
    # check is the load-bearing publish-safety invariant (step-4 filters after_json IS NOT NULL).
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
    assert row.after_jsonnull is None  # JSONB-'null' comparison yields SQL NULL → it's not 'null'


async def test_missing_gtin_is_always_a_todo_and_never_carries_a_barcode(db, shop):
    product = await _seed(db, shop.id, gaps=[_gap("missing_gtin")], body="A coffee.")
    client = ScriptedOptimizerClient([])  # no spec targets

    await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    gtin_fixes = [f for f in fixes if f.target == "gtin"]
    assert len(gtin_fixes) == 1
    assert gtin_fixes[0].type == FixType.merchant_todo
    assert gtin_fixes[0].after_json is None
    # No fix anywhere proposes a barcode/GTIN value.
    assert all(f.type == FixType.merchant_todo for f in fixes)


async def test_body_only_grounded_yields_metafield_but_no_description(db, shop):
    # The value was extracted FROM the body — appending it back into the body is redundant.
    body = "Roast: light."
    product = await _seed(db, shop.id, gaps=[_gap("spec_missing", "roast_level")], body=body)
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="light", source_field="body_html",
                            snippet="Roast: light", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    types = [f.type for f in await _fixes(db, product.id)]
    assert FixType.metafield in types      # the valuable half still fires
    assert FixType.description not in types  # no redundant re-append


RICH_HTML = (
    "<h2>Our Signature Roast</h2>"
    '<p>Crafted with <strong>care</strong>. Read our '
    '<a href="https://example.test/story">story</a>.</p>'
    "<ul><li>Small batch</li><li>Fair trade</li></ul>"
)


async def test_non_body_grounded_yields_metafield_and_description(db, shop):
    # roast is a gap; the value lives in variants_json (a non-body source) — so surfacing it into
    # the description adds information the prose lacks.
    product = await _seed(
        db, shop.id, gaps=[_gap("spec_missing", "roast_level")],
        body="<p>A nice coffee.</p>", variants=[{"title": "Roast: Medium-Light"}],
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="Medium-Light",
                            source_field="variants_json", snippet="Roast: Medium-Light",
                            ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    assert len([f for f in fixes if f.type == FixType.metafield]) == 1
    desc = [f for f in fixes if f.type == FixType.description]
    assert len(desc) == 1
    # against the ORIGINAL body_html, byte-for-byte — impossible to satisfy with stripped text.
    assert desc[0].after_json["body_html"].startswith(desc[0].before_json["body_html"])
    assert "Medium-Light" in desc[0].after_json["body_html"]


async def test_description_fix_preserves_html_verbatim(db, shop):
    product = await _seed(
        db, shop.id, gaps=[_gap("spec_missing", "roast_level")],
        body=RICH_HTML, variants=[{"title": "Roast: Medium-Light"}],
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="Medium-Light",
                            source_field="variants_json", snippet="Roast: Medium-Light",
                            ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    desc = [f for f in await _fixes(db, product.id) if f.type == FixType.description][0]
    after = desc.after_json["body_html"]
    assert desc.before_json["body_html"] == RICH_HTML
    assert after.startswith(RICH_HTML)  # merchant's markup preserved byte-for-byte
    # every original tag survives verbatim
    for tag in ("<h2>", "</h2>", "<strong>", "</strong>",
                '<a href="https://example.test/story">', "</a>", "<ul>", "<li>Small batch</li>"):
        assert tag in after
    # the appended block is valid HTML with no bare newline
    appended = after[len(RICH_HTML):]
    assert appended == "<p><strong>Details</strong></p><ul><li>Roast Level: Medium-Light</li></ul>"
    assert "\n" not in appended


async def test_run_id_is_stamped(db, shop):
    product = await _seed(db, shop.id, gaps=[_gap("spec_missing", "altitude")])
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
    assert all(f.run_id == run.id for f in fixes)


async def test_no_audit_raises(db, shop):
    product = Product(shop_id=shop.id, shopify_product_id="gid://shopify/Product/9",
                      title="X", visibility_state="active", product_type="Coffee")
    db.add(product)
    await db.commit()
    await db.refresh(product)

    with pytest.raises(ValueError):
        await run_optimizer(db, product.id, ScriptedOptimizerClient([]))


async def test_excluded_product_produces_no_fixes(db, shop):
    product = await _seed(db, shop.id, gaps=[], severity="not_audited")
    report = await run_optimizer(db, product.id, ScriptedOptimizerClient([]))

    count = await db.scalar(
        select(func.count()).select_from(Fix).where(Fix.product_id == product.id)
    )
    assert count == 0
    assert report.fillable == 0 and report.todos == 0


async def test_unknown_product_raises(db):
    with pytest.raises(ValueError):
        await run_optimizer(db, 999999, ScriptedOptimizerClient([]))


# --- step 2b: structural targeting (the decoupling) -------------------------------------------
async def test_targeting_ignores_the_audits_gap_list_entirely(db, shop):
    """THE decoupling test. The audit found no spec gaps at all, yet every family the product does
    not carry as a metafield is still targeted.

    This is what breaks the old inverse coupling between detection quality and fill capability:
    when targets came from gaps, a fill could only happen where ``detect`` had FAILED to notice a
    spec in the prose, so improving detection destroyed the fill path. Targets are now structural,
    so refining ``detect`` cannot add or remove a single fill.
    """
    product = await _seed(db, shop.id, gaps=[], body="Roast level: light.")
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    _, targets = client.calls[0]
    assert sorted(targets) == sorted(_ALL_FAMILIES)


async def test_a_structured_family_is_not_targeted(db, shop):
    """Already machine-readable → nothing to do. This is also what makes the node self-limiting."""
    product = await _seed(
        db, shop.id, gaps=[_gap("spec_missing", "roast_level")],
        metafields=[{"namespace": "custom", "key": "roast_level", "value": "Light"}],
    )
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    _, targets = client.calls[0]
    assert "roast_level" not in targets
    assert len(targets) == len(_ALL_FAMILIES) - 1


async def test_an_empty_valued_metafield_is_still_targeted(db, shop):
    """A key with no value is not machine-readable, so the gap is real and must stay targeted —
    the conservative direction, since a false ``structured`` would silently drop it forever."""
    product = await _seed(
        db, shop.id, gaps=[],
        metafields=[{"namespace": "custom", "key": "roast_level", "value": ""}],
    )
    client = ScriptedOptimizerClient([])

    await run_optimizer(db, product.id, client)

    _, targets = client.calls[0]
    assert "roast_level" in targets


async def test_non_coffee_class_is_never_asked_for_coffee_families(db, shop):
    """Targeting is gated on the persisted product_class. Without that gate, dropping the gap list
    would ask an espresso machine for its roast level and tasting notes."""
    product = await _seed(db, shop.id, gaps=[_gap("missing_gtin")], product_type="Brewing Gear")
    audit = (
        await db.execute(select(Audit).where(Audit.product_id == product.id))
    ).scalar_one()
    audit.product_class = "equipment"
    await db.commit()

    client = ScriptedOptimizerClient([])
    report = await run_optimizer(db, product.id, client)

    assert client.calls == []  # no extraction call at all
    # The GTIN gap is still honoured — a barcode can never be derived.
    fixes = await _fixes(db, product.id)
    assert [f.target for f in fixes] == ["gtin"]
    assert report.fillable == 0


async def test_optimizer_converges_once_a_family_is_structured(db, shop):
    """Idempotence/convergence: publishing a metafield (step 4) makes the family structured, which
    removes it from targets — so a re-run proposes nothing new for it rather than looping forever.
    """
    body = "Roast level: light (Agtron 68)."
    product = await _seed(
        db, shop.id, gaps=[_gap("spec_missing", "roast_level")], body=body,
        metafields=_only_target("roast_level"),
    )
    candidate = AttributeCandidate(
        attribute="roast_level", value="light", source_field="body_html",
        snippet="Roast level: light", ambiguous=False,
    )

    first = await run_optimizer(db, product.id, ScriptedOptimizerClient([candidate]))
    assert first.fillable == 1

    # Simulate the publish: the proposed metafield now exists on the product.
    product.metafields_json = [
        *(product.metafields_json or []),
        {"namespace": "custom", "key": "roast_level", "value": "light"},
    ]
    await db.commit()

    client = ScriptedOptimizerClient([candidate])
    second = await run_optimizer(db, product.id, client)

    assert client.calls == []       # nothing left to target
    assert second.fillable == 0 and second.todos == 0
