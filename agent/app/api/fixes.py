"""The approval gate (PRD §6, §13) — list proposed fixes, approve or reject them.

**This module makes NO Shopify calls and writes exactly ONE column: ``fixes.status``.** Approval
is a status transition (``proposed → approved | rejected``); the Step-4 Publisher is what later
acts on ``approved`` rows. Nothing here reaches a merchant's store.

Three routes, all mirroring ``api.scan``'s shape (``by-domain`` keying, shared-secret dependency,
202-then-poll for the async job):

* ``POST /shops/by-domain/{shop}/fixes/run`` — enqueue an audit+optimize run. A clone of
  ``start_scan``: commit the panel + ``running`` agent_run BEFORE enqueueing, so a crash at task
  start is visible as a stuck ``running`` run rather than a missing one.
* ``GET  /shops/by-domain/{shop}/fixes`` — the render payload, RUN-SCOPED exactly like
  ``get_report``. A still-running run has no fix rows yet, so it naturally returns
  ``status=running`` with ``products: []`` — no special-casing.
* ``POST .../fixes/{fix_id}/approve|reject`` — the gate itself.

**The agent computes the render payload; the app shell derives nothing** (CLAUDE.md: keep the
shell thin). Diff extraction, approvability and the citation list are decided here so the shell
renders lists rather than re-implementing rules.

**Approvability is enforced HERE, not only in the UI.** A ``metafield`` (taxonomy) fix is grounded
and correct but its write path is blocked on the ``read_metaobject_definitions`` scope
(``docs/decisions/2c-write-target.md`` L6), and a ``merchant_todo`` has ``after_json = NULL`` and
is not publishable at all. Both are refused with 409. The UI is not a security boundary, and the
invariant is: **no approvable path to a write that cannot execute.**
"""

import re
from datetime import datetime
from typing import Annotated

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db import get_db
from app.graph.interrogator import build_query_panel
from app.models import AgentRun, AgentRunStatus, Audit, Fix, FixStatus, FixType, Product, Shop
from app.services.panels import upsert_panel
from app.services.runs import create_agent_run
from app.settings import get_settings

router = APIRouter(prefix="/shops", tags=["fixes"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# Fix types a merchant may act on. ``metafield`` (taxonomy) is deliberately absent — see the module
# docstring. ``merchant_todo`` is informational and has no ``after_json`` to publish.
APPROVABLE_TYPES = frozenset({FixType.description, FixType.category})

# Everything past the gate. ``approved`` is the Publisher's work set; the rest are its results.
POST_APPROVAL_STATUSES = (
    FixStatus.approved,
    FixStatus.published,
    FixStatus.verified,
    FixStatus.publish_failed,
)
# Terminal publish outcomes the merchant is shown as "what happened". ``stale`` joins these at
# render time only when the Publisher left a ``publish_error`` on it (see ``list_fixes``).
SETTLED_STATUSES = frozenset(
    {FixStatus.published, FixStatus.verified, FixStatus.publish_failed, FixStatus.stale}
)

# Why a non-approvable row is non-approvable, shown to the merchant verbatim.
BLOCK_REASONS: dict[str, str] = {
    FixType.metafield: (
        "Publishing a taxonomy attribute needs an additional Shopify permission this app has not "
        "been granted yet. The value is identified and ready — it just can't be written."
    ),
    FixType.merchant_todo: "This one needs information only you have — we can't derive it.",
}

_LIST_ITEM = re.compile(r"<li>(.*?)</li>", re.S)
_HTML_TAG = re.compile(r"<[^>]+>")


class Citation(BaseModel):
    """Where a proposed value literally came from — the trust mechanism, never optional chrome."""

    attribute: str | None = None
    source_field: str | None = None
    snippet: str | None = None


class AddedLine(BaseModel):
    label: str
    value: str


class FixView(BaseModel):
    """One fix, pre-rendered for the shell."""

    id: int
    type: str
    target: str
    status: str
    reason: str | None
    diff: str | None
    citations: list[Citation]
    approvable: bool
    block_reason: str | None
    # Publish audit trail (step 4). ``publish_error`` is why a publish did not land — a stale
    # refusal or a failed write — and the shell shows it verbatim, so a failure never reads as a
    # dead end. ``published_at`` is set only on a confirmed re-read.
    publish_error: str | None = None
    published_at: datetime | None = None
    # Type-specific render payload; exactly one is populated.
    added_lines: list[AddedLine] = []
    category_from: str | None = None
    category_to: str | None = None
    metafield_value: str | None = None
    metafield_key: str | None = None


class ProductFixes(BaseModel):
    product_id: int
    title: str | None
    severity: str | None
    approvable: list[FixView]
    not_publishable: list[FixView]
    needs_input: list[FixView]
    # Step 4. ``ready`` = approved, awaiting publish — exactly the Publisher's work set, so the
    # merchant never sees fewer rows than we would write. ``settled`` = the outcome of a publish
    # run (published / verified / publish_failed, and any row the staleness gate refused).
    ready: list[FixView] = []
    settled: list[FixView] = []


class FixListResponse(BaseModel):
    run_id: int | None
    status: AgentRunStatus | None
    products: list[ProductFixes]
    # Set only when the caller passes ``publish_run_id`` — see ``list_fixes``.
    publish_run_id: int | None = None
    publish_status: AgentRunStatus | None = None


class FixRunResponse(BaseModel):
    run_id: int
    status: AgentRunStatus


class FixDecisionResponse(BaseModel):
    fix_id: int
    status: FixStatus


async def _enqueue(run_id: int) -> None:
    await _enqueue_job("run_fix_task", run_id)


async def _enqueue_publish(run_id: int) -> None:
    await _enqueue_job("run_publish_task", run_id)


async def _enqueue_job(name: str, run_id: int) -> None:
    pool: ArqRedis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job(name, run_id)
    finally:
        await pool.aclose()


def _strip(text: str) -> str:
    return " ".join(_HTML_TAG.sub(" ", text).split())


def _added_lines(
    before_json: dict | list | None, after_json: dict | list | None
) -> list[AddedLine]:
    """The description lines a fix ADDS, as label/value pairs — never raw HTML.

    ``_compose_description`` appends to the original body byte-for-byte, so the addition is the
    suffix. The ``startswith`` check is a guard, not an assumption: if the shape ever changes we
    fall back to reading the whole ``after`` block rather than silently showing a wrong diff.
    """
    if not isinstance(after_json, dict):
        return []
    after = after_json.get("body_html") or ""
    before = (before_json or {}).get("body_html") or "" if isinstance(before_json, dict) else ""
    added = after[len(before) :] if after.startswith(before) else after

    lines: list[AddedLine] = []
    for item in _LIST_ITEM.findall(added):
        text = _strip(item)
        label, separator, value = text.partition(":")
        # A line without a "Label: value" shape is still shown, rather than dropped.
        lines.append(
            AddedLine(label=label.strip(), value=value.strip())
            if separator
            else AddedLine(label="", value=text)
        )
    return lines


def _to_view(fix: Fix) -> FixView:
    """Pre-render one fix. All approvability and diff logic lives here, not in the shell."""
    approvable = fix.type in APPROVABLE_TYPES
    after = fix.after_json if isinstance(fix.after_json, dict) else {}
    before = fix.before_json if isinstance(fix.before_json, dict) else {}

    view = FixView(
        id=fix.id,
        type=fix.type,
        target=fix.target,
        status=fix.status,
        reason=fix.reason,
        diff=fix.diff,
        citations=[Citation(**c) for c in (fix.source_json or []) if isinstance(c, dict)],
        approvable=approvable,
        block_reason=None if approvable else BLOCK_REASONS.get(fix.type),
        publish_error=fix.publish_error,
        published_at=fix.published_at,
    )

    if fix.type == FixType.description:
        view.added_lines = _added_lines(fix.before_json, fix.after_json)
    elif fix.type == FixType.category:
        view.category_from = before.get("category") or "Uncategorized"
        view.category_to = after.get("fullName")
    elif fix.type == FixType.metafield:
        view.metafield_key = f"{after.get('namespace')}.{after.get('key')}"
        view.metafield_value = after.get("value")

    return view


async def _resolve_shop(db: AsyncSession, shop_domain: str) -> Shop:
    shop = (
        await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    ).scalar_one_or_none()
    if shop is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shop not found.")
    return shop


@router.post(
    "/by-domain/{shop_domain}/fixes/run",
    response_model=FixRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_internal_api_key)],
)
async def start_fix_run(shop_domain: str, db: DbSession) -> FixRunResponse:
    """Enqueue an audit+optimize run for the shop and return 202 with its ``run_id``.

    A clone of ``api.scan.start_scan``. The panel is upserted only because ``agent_runs.panel_id``
    is NOT NULL — a fix run has no query panel of its own (logged in ``docs/backlog.md``); reusing
    the shop's deterministic panel keeps this migration-free.
    """
    shop = await _resolve_shop(db, shop_domain)

    panel_id = await upsert_panel(db, build_query_panel(), shop.id)
    run = await create_agent_run(db, shop.id, panel_id)
    await db.commit()  # load-bearing: the run must exist before the job is enqueued

    await _enqueue(run.id)

    return FixRunResponse(run_id=run.id, status=run.status)


@router.post(
    "/by-domain/{shop_domain}/fixes/publish",
    response_model=FixRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_internal_api_key)],
)
async def start_publish(shop_domain: str, db: DbSession) -> FixRunResponse:
    """Enqueue a publish run — THE ONLY ROUTE IN THIS SERVICE THAT LEADS TO A MERCHANT WRITE.

    It writes no ``fixes`` row itself and takes no fix ids: the work set is exactly the shop's
    ``approved`` rows, so a crafted request cannot widen what gets published beyond what the
    merchant individually approved. A shop with nothing approved gets a 409 rather than an empty
    run, so "publish" never silently does nothing.

    The staged-rollout allowlist is enforced in the job, not here — the guard belongs next to the
    write, where no other caller can route around it.
    """
    shop = await _resolve_shop(db, shop_domain)

    approved = await db.scalar(
        select(func.count())
        .select_from(Fix)
        .join(Product, Product.id == Fix.product_id)
        .where(Product.shop_id == shop.id, Fix.status == FixStatus.approved)
    )
    if not approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Nothing is approved for this shop, so there is nothing to publish.",
        )

    panel_id = await upsert_panel(db, build_query_panel(), shop.id)
    run = await create_agent_run(db, shop.id, panel_id)
    await db.commit()  # load-bearing: the run must exist before the job is enqueued

    await _enqueue_publish(run.id)

    return FixRunResponse(run_id=run.id, status=run.status)


@router.get(
    "/by-domain/{shop_domain}/fixes",
    response_model=FixListResponse,
    dependencies=[Depends(require_internal_api_key)],
)
async def list_fixes(
    shop_domain: str,
    db: DbSession,
    run_id: int | None = None,
    publish_run_id: int | None = None,
) -> FixListResponse:
    """Fixes for a run, grouped by product and pre-rendered.

    **Two different scopes, deliberately.** ``proposed`` rows are RUN-SCOPED (mirrors
    ``get_report``), so superseded proposals from earlier runs are never shown — no writes needed
    to hide them. Everything past approval is SHOP-SCOPED, because the Publisher publishes every
    approved row for the shop regardless of which run proposed it. Run-scoping that half would show
    the merchant fewer rows than we would actually write, which is exactly the surprise the
    approval gate exists to prevent.

    With no ``run_id``, resolves the latest run that actually PRODUCED fixes. ``agent_runs`` has no
    run-kind discriminator (backlog), so "the latest fix run" is derived from the fixes themselves,
    which is exact for this purpose. A shop that has never proposed a fix gets an empty payload,
    not a 404 — "nothing to approve yet" is a normal state the page renders.

    ``publish_run_id`` is echoed back with its status so the shell can poll a publish in flight
    without needing a run-kind discriminator: the 202 from ``start_publish`` hands the shell the
    id, and the shell hands it back here.
    """
    shop = await _resolve_shop(db, shop_domain)

    publish_status = None
    if publish_run_id is not None:
        publish_status = await db.scalar(
            select(AgentRun.status).where(
                AgentRun.id == publish_run_id, AgentRun.shop_id == shop.id
            )
        )

    if run_id is None:
        run_id = await db.scalar(
            select(func.max(Fix.run_id))
            .select_from(Fix)
            .join(Product, Product.id == Fix.product_id)
            .where(Product.shop_id == shop.id)
        )
    if run_id is None:
        return FixListResponse(
            run_id=None,
            status=None,
            products=[],
            publish_run_id=publish_run_id,
            publish_status=publish_status,
        )

    run = (
        await db.execute(
            select(AgentRun).where(AgentRun.id == run_id, AgentRun.shop_id == shop.id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")

    rows = (
        await db.execute(
            select(Fix, Product)
            .join(Product, Product.id == Fix.product_id)
            .where(
                Product.shop_id == shop.id,
                or_(
                    # The gate: this run's undecided proposals.
                    and_(Fix.run_id == run_id, Fix.status == FixStatus.proposed),
                    # The Publisher's work set and its results, across every run.
                    Fix.status.in_(POST_APPROVAL_STATUSES),
                    # A row the staleness gate REFUSED. ``stale`` is overloaded — Step 3's
                    # supersede uses it too — so it is shown only when the Publisher left a
                    # cause on it. A supersede-stale row carries NULL and stays hidden.
                    and_(Fix.status == FixStatus.stale, Fix.publish_error.is_not(None)),
                ),
            )
            .order_by(Product.id, Fix.id)
        )
    ).all()

    # Severity comes from each product's latest audit — the same run where possible.
    severities: dict[int, str] = {}
    product_ids = {product.id for _, product in rows}
    if product_ids:
        audit_rows = (
            await db.execute(
                select(Audit.product_id, Audit.severity, Audit.id)
                .where(Audit.product_id.in_(product_ids))
                .order_by(Audit.product_id, Audit.id.desc())
            )
        ).all()
        for product_id, severity, _ in audit_rows:
            severities.setdefault(product_id, severity)

    grouped: dict[int, ProductFixes] = {}
    for fix, product in rows:
        entry = grouped.get(product.id)
        if entry is None:
            entry = ProductFixes(
                product_id=product.id,
                title=product.title,
                severity=severities.get(product.id),
                approvable=[],
                not_publishable=[],
                needs_input=[],
            )
            grouped[product.id] = entry

        view = _to_view(fix)
        if fix.status == FixStatus.approved:
            entry.ready.append(view)
        elif fix.status in SETTLED_STATUSES:
            entry.settled.append(view)
        elif view.approvable:
            entry.approvable.append(view)
        elif fix.type == FixType.merchant_todo:
            entry.needs_input.append(view)
        else:
            entry.not_publishable.append(view)

    return FixListResponse(
        run_id=run_id,
        status=run.status,
        products=list(grouped.values()),
        publish_run_id=publish_run_id,
        publish_status=publish_status,
    )


async def _load_fix_for_shop(db: AsyncSession, shop_domain: str, fix_id: int) -> Fix:
    """Load a fix, proving it belongs to ``shop_domain``.

    **The authorization check.** Without the join a shop could act on another shop's fixes by
    guessing an integer id. A mismatch returns 404 rather than 403 so ids cannot be probed for
    existence.
    """
    fix = (
        await db.execute(
            select(Fix)
            .join(Product, Product.id == Fix.product_id)
            .join(Shop, Shop.id == Product.shop_id)
            .where(Fix.id == fix_id, Shop.shop_domain == shop_domain)
        )
    ).scalar_one_or_none()
    if fix is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fix not found.")
    return fix


async def _decide(
    db: AsyncSession, shop_domain: str, fix_id: int, target_status: FixStatus
) -> FixDecisionResponse:
    """Shared approve/reject transition. Writes ``fixes.status`` and nothing else."""
    fix = await _load_fix_for_shop(db, shop_domain, fix_id)

    if fix.type not in APPROVABLE_TYPES:
        # Server-side enforcement of the non-approvable rule (see module docstring).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {fix.type} fix cannot be approved or rejected.",
        )

    if fix.status == target_status:
        return FixDecisionResponse(fix_id=fix.id, status=FixStatus(fix.status))  # idempotent

    if fix.status != FixStatus.proposed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Fix {fix.id} is {fix.status}; only a proposed fix can be {target_status}.",
        )

    fix.status = target_status

    if target_status == FixStatus.approved:
        # SUPERSEDE: at most ONE approved fix per (product, target). Step 4's description composer
        # appends, so two approved rows for one body would append the Details block twice.
        await db.execute(
            update(Fix)
            .where(
                Fix.product_id == fix.product_id,
                Fix.target == fix.target,
                Fix.id != fix.id,
                Fix.status.in_([FixStatus.proposed, FixStatus.approved]),
            )
            .values(status=FixStatus.stale)
        )

    await db.commit()
    return FixDecisionResponse(fix_id=fix.id, status=target_status)


@router.post(
    "/by-domain/{shop_domain}/fixes/{fix_id}/approve",
    response_model=FixDecisionResponse,
    dependencies=[Depends(require_internal_api_key)],
)
async def approve_fix(shop_domain: str, fix_id: int, db: DbSession) -> FixDecisionResponse:
    """Approve a fix: ``proposed → approved``. NO Shopify write happens here."""
    return await _decide(db, shop_domain, fix_id, FixStatus.approved)


@router.post(
    "/by-domain/{shop_domain}/fixes/{fix_id}/reject",
    response_model=FixDecisionResponse,
    dependencies=[Depends(require_internal_api_key)],
)
async def reject_fix(shop_domain: str, fix_id: int, db: DbSession) -> FixDecisionResponse:
    """Reject a fix: ``proposed → rejected``. The decision is remembered, never re-proposed over."""
    return await _decide(db, shop_domain, fix_id, FixStatus.rejected)
