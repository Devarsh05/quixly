"""TEMPORARY dev fixture — seeds one real non-to-do Optimizer fix for Step 3's approval UI.

**Obsolete once Part B (three-state targeting) lands.** It works only by exploiting the same
detection gap Part B removes: the audit reads title/body/metafields but NOT ``variants_json``, so a
spec injected into a variant stays an audit gap yet is a live source the Optimizer extracts from —
which is exactly how it yields both a metafield fix and a (non-body) description fix. A real
merchant edit + re-ingest would produce equivalent rows; there is no store-write path until Step 4.

Idempotent: clears the seed product's prior audits/fixes, (re)injects one seed variant, and runs
audit → optimize with a DETERMINISTIC scripted candidate (a committed seed must not depend on LLM
nondeterminism; the persisted ``fixes`` rows are real, which is what the UI needs). Run from the
agent dir:  ``python scripts/seed_optimizer_demo.py``
"""

import asyncio

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.graph.audit import run_audit
from app.graph.optimizer import run_optimizer
from app.models import Audit, Fix, FixType, Product
from app.services.optimizer_llm import AttributeCandidate, ExtractedAttributes

SEED_PRODUCT_ID = 114  # a coffee product whose roast_level is an audit gap
SEED_VARIANT_ID = "gid://shopify/Variant/SEED-OPTIMIZER-DEMO"
SEED_SNIPPET = "Roast: Medium-Light"


class SeedClient:
    """Deterministic stand-in returning two candidates, both grounded in the injected variant:

    - roast_level="Medium-Light" → valid for the family → a metafield + description fix.
    - brew_method="Medium-Light" → literally present but NOT a brew method → a mis_assignment DROP,
      so a persisted, queryable mis_assignment to-do exists (A1) alongside the real fills.
    """

    async def extract(self, source_fields, target_attributes) -> ExtractedAttributes:
        wanted = {
            "roast_level": AttributeCandidate(
                attribute="roast_level", value="Medium-Light",
                source_field="variants_json", snippet=SEED_SNIPPET, ambiguous=False,
            ),
            "brew_method": AttributeCandidate(
                attribute="brew_method", value="Medium-Light",
                source_field="variants_json", snippet=SEED_SNIPPET, ambiguous=False,
            ),
        }
        return ExtractedAttributes(
            attributes=[c for a, c in wanted.items() if a in target_attributes]
        )


async def main() -> None:
    async with SessionLocal() as s:
        product = await s.get(Product, SEED_PRODUCT_ID)
        if product is None:
            raise SystemExit(f"product {SEED_PRODUCT_ID} not found — re-ingest the dev store first")

        # Idempotent reset of just this product's rows.
        await s.execute(delete(Fix).where(Fix.product_id == SEED_PRODUCT_ID))
        await s.execute(delete(Audit).where(Audit.product_id == SEED_PRODUCT_ID))

        # Inject one seed variant carrying the roast (dedup so re-runs don't stack).
        variants = [
            v for v in (product.variants_json or []) if v.get("id") != SEED_VARIANT_ID
        ]
        variants.append(
            {"id": SEED_VARIANT_ID, "title": SEED_SNIPPET, "sku": "SEED", "barcode": None}
        )
        product.variants_json = variants
        await s.commit()

        await run_audit(s, SEED_PRODUCT_ID)
        report = await run_optimizer(s, SEED_PRODUCT_ID, SeedClient())

        fills = (
            await s.execute(
                select(Fix)
                .where(
                    Fix.product_id == SEED_PRODUCT_ID,
                    Fix.type.in_([FixType.metafield, FixType.description]),
                )
                .order_by(Fix.id)
            )
        ).scalars().all()

    print(f"seeded product {SEED_PRODUCT_ID}: fillable={report.fillable} todos={report.todos} "
          f"dropped={[(d.attribute, d.reason) for d in report.dropped]}")
    for f in fills:
        print(f"  {f.type}  target={f.target}")
        print(f"    before_json={f.before_json}")
        print(f"    after_json ={f.after_json}")
        print(f"    source_json={f.source_json}")
        print(f"    diff       ={f.diff}")
    print("ROWS LEFT IN PLACE.")


if __name__ == "__main__":
    asyncio.run(main())
