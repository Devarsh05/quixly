"""The fix job's reusable core, ``jobs.fix.propose_fixes_for_shop`` (Phase 3, step 2b).

DB-backed. A scripted ``OptimizerClient`` stands in for the LLM. This is the entrypoint the
step-3 route/task and the Gate L evidence script both call, so it is tested directly rather than
through either of them.

**Every query here is scoped to the test's own products.** The ``db`` fixture rolls back what the
test writes, but it does NOT hide rows already committed in the dev database — an unscoped
``select(Audit)`` reads the real catalog's audits and the assertions become meaningless.
"""

from sqlalchemy import select

from app.jobs.fix import propose_fixes_for_shop
from app.models import (
    AgentRun,
    AgentRunStatus,
    Audit,
    Fix,
    Product,
    Shop,
    ShopStatus,
)
from app.models import QueryPanel as QueryPanelRow
from app.services.optimizer_llm import AttributeCandidate, ExtractedAttributes

RICH_BODY = "Roast level: light (Agtron 68). Single-origin Ethiopia."


class ScriptedOptimizerClient:
    def __init__(self, candidates=None):
        self._candidates = candidates or []
        self.calls = []

    async def extract(self, source_fields, target_attributes) -> ExtractedAttributes:
        self.calls.append((source_fields, list(target_attributes)))
        return ExtractedAttributes(
            attributes=[c for c in self._candidates if c.attribute in target_attributes]
        )


async def _shop(db) -> Shop:
    shop = Shop(shop_domain="fix-job-test.myshopify.com", status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _product(db, shop_id: int, n: int, **overrides) -> Product:
    defaults = dict(
        shopify_product_id=f"gid://shopify/Product/{n}",
        title="Some Coffee",
        body=RICH_BODY,
        variants_json=[],
        metafields_json=None,
        visibility_state="active",
        product_type="Coffee",
    )
    defaults.update(overrides)
    product = Product(shop_id=shop_id, **defaults)
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


async def _run(db, shop_id: int) -> AgentRun:
    panel = QueryPanelRow(
        shop_id=shop_id, category="coffee", queries_json=[{"text": "q"}], fingerprint="fp"
    )
    db.add(panel)
    await db.commit()
    await db.refresh(panel)
    run = AgentRun(shop_id=shop_id, panel_id=panel.id, status=AgentRunStatus.running)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def test_audits_every_product_and_optimizes_the_discoverable_ones(db):
    shop = await _shop(db)
    live = await _product(db, shop.id, 1)
    draft = await _product(db, shop.id, 2, visibility_state="draft")

    reports = await propose_fixes_for_shop(db, shop.id, ScriptedOptimizerClient())

    # Both products are AUDITED — that is how a draft is recorded as not_audited...
    ours = [live.id, draft.id]
    audits = (
        await db.execute(
            select(Audit).where(Audit.product_id.in_(ours)).order_by(Audit.product_id)
        )
    ).scalars().all()
    assert {a.product_id for a in audits} == {live.id, draft.id}
    assert next(a for a in audits if a.product_id == draft.id).severity == "not_audited"
    # ...but only the discoverable one is optimized.
    assert [r.product_id for r in reports] == [live.id]
    fixes = (
        await db.execute(select(Fix).where(Fix.product_id.in_(ours)))
    ).scalars().all()
    assert {f.product_id for f in fixes} == {live.id}


async def test_stamps_run_id_on_every_persisted_row(db):
    """Standing convention: an evidence run must be separable and clearable by run_id alone."""
    shop = await _shop(db)
    product = await _product(db, shop.id, 1)
    run = await _run(db, shop.id)

    await propose_fixes_for_shop(db, shop.id, ScriptedOptimizerClient(), run_id=run.id)

    audits = (
        await db.execute(select(Audit).where(Audit.product_id == product.id))
    ).scalars().all()
    fixes = (
        await db.execute(select(Fix).where(Fix.product_id == product.id))
    ).scalars().all()
    assert audits and all(a.run_id == run.id for a in audits)
    assert fixes and all(f.run_id == run.id for f in fixes)


async def test_a_prose_only_spec_is_filled_across_the_population(db):
    """End-to-end shape of the step: no metafields anywhere, so every family is targeted, and the
    one that grounds+validates from the prose becomes a metafield fix — no injection involved."""
    shop = await _shop(db)
    product = await _product(db, shop.id, 1)
    client = ScriptedOptimizerClient(
        [AttributeCandidate(
            attribute="roast_level", value="light", source_field="body_html",
            snippet="Roast level: light", ambiguous=False,
        )]
    )

    reports = await propose_fixes_for_shop(db, shop.id, client)

    _, targets = client.calls[0]
    assert len(targets) == 7  # nothing structured yet
    assert reports[0].fillable >= 1
    fills = (
        await db.execute(
            select(Fix).where(
                Fix.product_id == product.id,
                Fix.target == "metafield:custom.roast_level",
            )
        )
    ).scalars().all()
    assert len(fills) == 1
    assert fills[0].after_json["value"] == "light"
    assert fills[0].product_id == product.id


async def test_no_products_is_a_no_op(db):
    shop = await _shop(db)
    assert await propose_fixes_for_shop(db, shop.id, ScriptedOptimizerClient()) == []
