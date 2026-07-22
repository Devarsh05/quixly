"""Product-level routes (PRD §9) — the deterministic audit today; fix generation lands here later.

Internal-only (``INTERNAL_API_KEY`` shared secret), like every other agent route. Keyed on the
agent-internal product PK: the audit is invoked by orchestration and the internal API, which hold
the PK — not directly from the storefront.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db import get_db
from app.graph.audit import AuditOutcome, run_audit

router = APIRouter(prefix="/products", tags=["products"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


class AuditRequest(BaseModel):
    """Optional body: scope the audit to a scan run. Absent/empty body → a standalone audit."""

    run_id: int | None = None


@router.post(
    "/{product_id}/audit",
    response_model=AuditOutcome,
    dependencies=[Depends(require_internal_api_key)],
)
async def audit_product(
    product_id: int,
    db: DbSession,
    body: AuditRequest | None = None,
) -> AuditOutcome:
    """Audit one product against the deterministic rubric and persist the result."""
    run_id = body.run_id if body else None
    try:
        return await run_audit(db, product_id, run_id=run_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
