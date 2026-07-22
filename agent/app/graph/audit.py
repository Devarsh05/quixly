"""Audit node — scores one product with the deterministic rubric and persists the result.

PRD §6 Diagnostician (Phase 3, step 1). Loads a ``products`` row, runs the pure rubric in
``services.audit_rubric`` (catalog-side rule checks only — no LLM, no engine-win evidence, so it
works on a store with zero AI-recommendation wins), and writes one ``audits`` row.

Audits **append** (always INSERT), mirroring ``engine_runs`` — a product's audit history is kept
so Phase 4's Verifier can compare a pre-fix audit to a post-fix one. ``run_id`` scopes the audit
to a scan when supplied, and is NULL for a standalone one-off audit. The session is injected so
tests drive it against the transaction-scoped ``db`` fixture.
"""

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Audit, Product
from app.services.audit_rubric import AuditGap, evaluate_product


class AuditOutcome(BaseModel):
    """The audit node's typed return, held in graph state and serialised by the route."""

    audit_id: int
    product_id: int
    run_id: int | None
    severity: str
    spec_coverage: float
    gaps: list[AuditGap]


async def run_audit(
    session: AsyncSession, product_id: int, *, run_id: int | None = None
) -> AuditOutcome:
    """Audit one product against the rubric and persist an ``audits`` row.

    Raises ``ValueError`` if ``product_id`` does not exist (the route maps this to 404).
    """
    product = await session.get(Product, product_id)
    if product is None:
        raise ValueError(f"product {product_id} not found")

    result = evaluate_product(
        title=product.title,
        body=product.body,
        gtin=product.gtin,
        metafields=product.metafields_json,
        visibility_state=product.visibility_state,
    )

    audit = Audit(
        product_id=product_id,
        run_id=run_id,
        gaps_json=[gap.model_dump() for gap in result.gaps],
        spec_coverage=result.spec_coverage,
        severity=result.severity,
    )
    session.add(audit)
    await session.commit()
    await session.refresh(audit)

    return AuditOutcome(
        audit_id=audit.id,
        product_id=product_id,
        run_id=run_id,
        severity=result.severity,
        spec_coverage=result.spec_coverage,
        gaps=result.gaps,
    )
