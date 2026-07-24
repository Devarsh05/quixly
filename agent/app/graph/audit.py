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
from app.services.catalog import classify_product


class AuditOutcome(BaseModel):
    """The audit node's typed return, held in graph state and serialised by the route."""

    audit_id: int
    product_id: int
    run_id: int | None
    audited: bool
    product_class: str
    severity: str
    # Two coverage numbers, deliberately both: prose vs structured (see models.audit).
    spec_coverage: float | None
    structured_coverage: float | None
    gaps: list[AuditGap]
    excluded_reason: str | None = None


async def run_audit(
    session: AsyncSession, product_id: int, *, run_id: int | None = None
) -> AuditOutcome:
    """Audit one product against the per-class rubric and persist an ``audits`` row.

    The product class is derived from merchant fields (``classify_product``); not-visible products
    are excluded (persisted as ``not_audited`` with no gaps). Raises ``ValueError`` if
    ``product_id`` does not exist (the route maps this to 404).
    """
    product = await session.get(Product, product_id)
    if product is None:
        raise ValueError(f"product {product_id} not found")

    product_class = classify_product(product.product_type, product.category)
    result = evaluate_product(
        title=product.title,
        body=product.body,
        variants=product.variants_json,
        metafields=product.metafields_json,
        visibility_state=product.visibility_state,
        product_class=product_class,
    )

    audit = Audit(
        product_id=product_id,
        run_id=run_id,
        product_class=result.product_class,
        gaps_json=[gap.model_dump() for gap in result.gaps],
        spec_coverage=result.spec_coverage,
        structured_coverage=result.structured_coverage,
        severity=result.severity,
    )
    session.add(audit)
    await session.commit()
    await session.refresh(audit)

    return AuditOutcome(
        audit_id=audit.id,
        product_id=product_id,
        run_id=run_id,
        audited=result.audited,
        product_class=result.product_class,
        severity=result.severity,
        spec_coverage=result.spec_coverage,
        structured_coverage=result.structured_coverage,
        gaps=result.gaps,
        excluded_reason=result.excluded_reason,
    )
