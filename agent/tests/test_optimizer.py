"""The Optimizer node ``run_optimizer`` (Phase 3, step 2).

DB-backed (real Postgres ``db`` fixture). A scripted ``OptimizerClient`` stands in for the LLM (no
network); the real grounding guard runs. Asserts the emitted ``fixes`` rows: grounded attributes →
metafield fixes with a source citation; absent/ambiguous/hallucinated → merchant to-dos or dropped;
``missing_gtin`` → always a to-do carrying no barcode.
"""

import pytest
from sqlalchemy import func, select

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
    product = await _seed(db, shop.id, gaps=[_gap("spec_missing", "roast_level")], body=body)
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
        db, shop.id, gaps=[_gap("spec_missing", "altitude")], body="A pleasant everyday coffee."
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
        db, shop.id, gaps=[_gap("spec_missing", "roast_level")], body="Roast level: light"
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


async def test_description_gap_with_grounded_attrs_yields_a_grounded_rewrite(db, shop):
    body = "Roast level: light. Single-origin Ethiopia."
    product = await _seed(
        db, shop.id,
        gaps=[_gap("missing_description"), _gap("spec_missing", "roast_level")],
        body=body,
    )
    client = ScriptedOptimizerClient(
        [AttributeCandidate(attribute="roast_level", value="light", source_field="body_html",
                            snippet="Roast level: light", ambiguous=False)]
    )

    await run_optimizer(db, product.id, client)

    fixes = await _fixes(db, product.id)
    desc = [f for f in fixes if f.type == FixType.description]
    assert len(desc) == 1
    # Every attribute value surfaced in the rewrite is grounded (present in source).
    assert "light" in desc[0].after_json["body_html"]


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
