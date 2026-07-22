"""Optimizer/Writer node — proposes GROUNDED fixes from a product's own data (PRD §6, §13).

The Optimizer **extracts and restructures — it never generates.** The only grounding sources are
the product's own persisted fields (title, body, variants_json, metafields_json, product_type). An
LLM proposes candidate attribute values; the **grounding guard** (``ground_attribute`` →
``services.matching.is_grounded``, the same primitive the Extractor uses) is what actually decides
what survives: a value is kept only if the model's cited snippet is literally in the named source
field AND the value is literally in that snippet. A hallucinated value is dropped here, not trusted.

Outputs are all deterministic projections of the grounded attribute set:

* **metafield** fix — one per grounded attribute (structured, machine-readable).
* **description** fix — a spec-dense block composed only from grounded attributes, so it is
  grounded by construction ("restructure existing content, no new claims").
* **merchant_todo** — a first-class outcome for every gap that could NOT be grounded (absent or
  ambiguous), and ALWAYS for ``missing_gtin`` (a barcode cannot be derived — proposing one would
  violate PRD §13). To-dos carry ``after_json = NULL`` and are never publishable.

No Shopify writes: this node only persists ``fixes`` rows (``status = proposed``). The LLM client is
injected so tests drive a scripted client against the transaction-scoped ``db`` fixture.
"""

import hashlib
import json
import re

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Audit, Fix, FixStatus, FixType, Product
from app.services.audit_rubric import SEVERITY_NOT_AUDITED
from app.services.matching import is_grounded
from app.services.optimizer_llm import AttributeCandidate, OptimizerClient

_HTML_TAG = re.compile(r"<[^>]+>")
_METAFIELD_NAMESPACE = "custom"


class DroppedCandidate(BaseModel):
    """A candidate the model proposed but the guard dropped (not literally in source)."""

    attribute: str
    value: str
    source_field: str | None


class OptimizerReport(BaseModel):
    """The Optimizer node's typed return."""

    product_id: int
    run_id: int | None
    fillable: int
    todos: int
    dropped: list[DroppedCandidate]


def ground_attribute(candidate: AttributeCandidate, source_fields: dict[str, str]) -> str | None:
    """Return the candidate's value iff it is literally grounded in its cited source field.

    The guard (not the prompt) is the guarantee: the model's ``snippet`` must appear in the named
    ``source_field``, and ``value`` must appear in that ``snippet``. A null/ambiguous candidate, an
    unknown source field, a fabricated snippet, or a value not inside the snippet all refuse.
    """
    if candidate.ambiguous or candidate.value is None or candidate.snippet is None:
        return None
    field_text = source_fields.get(candidate.source_field or "")
    if not field_text:
        return None
    snippet_in_field = is_grounded(candidate.snippet, field_text)
    value_in_snippet = is_grounded(candidate.value, candidate.snippet)
    return candidate.value if snippet_in_field and value_in_snippet else None


def _strip_html(text: str | None) -> str:
    return _HTML_TAG.sub(" ", text or "")


def _variants_text(variants: list | None) -> str:
    parts: list[str] = []
    for variant in variants or []:
        if isinstance(variant, dict):
            parts.extend(str(v) for v in variant.values() if isinstance(v, str | int | float) and v)
    return " ".join(parts)


def _metafields_text(metafields: list | None) -> str:
    parts: list[str] = []
    for field in metafields or []:
        if isinstance(field, dict) and isinstance(field.get("value"), str):
            parts.append(field["value"])
    return " ".join(parts)


def _build_source_fields(product: Product) -> dict[str, str]:
    """The product's own fields as named text blobs — the only grounding sources (tags deferred)."""
    return {
        "title": product.title or "",
        "body_html": _strip_html(product.body).strip(),
        "variants_json": _variants_text(product.variants_json),
        "metafields": _metafields_text(product.metafields_json),
        "product_type": product.product_type or "",
    }


def _source_hash(source_fields: dict[str, str]) -> str:
    canonical = json.dumps(source_fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _metafield_object(attribute: str, value: str) -> dict:
    return {
        "namespace": _METAFIELD_NAMESPACE,
        "key": attribute,
        "value": value,
        "type": "single_line_text_field",
    }


def _citation(attribute: str, candidate: AttributeCandidate) -> dict:
    return {
        "attribute": attribute,
        "source_field": candidate.source_field,
        "snippet": candidate.snippet,
    }


def _compose_description(body: str | None, grounded: dict[str, AttributeCandidate]) -> str:
    """Compose a spec-dense description from grounded attributes only (no new claims)."""
    base = _strip_html(body).strip()
    lines = [f"- {attr.replace('_', ' ').title()}: {cand.value}" for attr, cand in grounded.items()]
    details = "Details:\n" + "\n".join(lines)
    return f"{base}\n\n{details}" if base else details


async def run_optimizer(
    session: AsyncSession,
    product_id: int,
    client: OptimizerClient,
    *,
    run_id: int | None = None,
) -> OptimizerReport:
    """Propose grounded fixes for a product from its latest audit's gaps. Persists ``fixes`` rows.

    Raises ``ValueError`` if the product or its audit is missing. A not-audited (excluded) product
    produces nothing.
    """
    product = await session.get(Product, product_id)
    if product is None:
        raise ValueError(f"product {product_id} not found")

    audit = (
        await session.execute(
            select(Audit).where(Audit.product_id == product_id).order_by(Audit.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if audit is None:
        raise ValueError(f"no audit for product {product_id}; audit must run first")

    if audit.severity == SEVERITY_NOT_AUDITED:
        return OptimizerReport(
            product_id=product_id, run_id=run_id, fillable=0, todos=0, dropped=[]
        )

    gaps = audit.gaps_json or []
    spec_targets = [
        g["attribute"] for g in gaps if g.get("code") == "spec_missing" and g.get("attribute")
    ]
    has_description_gap = any(g.get("code") == "missing_description" for g in gaps)
    has_gtin_gap = any(g.get("code") == "missing_gtin" for g in gaps)

    source_fields = _build_source_fields(product)
    base_hash = _source_hash(source_fields)

    extracted = await client.extract(source_fields, spec_targets) if spec_targets else None
    candidates = extracted.attributes if extracted else []

    grounded: dict[str, AttributeCandidate] = {}
    dropped: list[DroppedCandidate] = []
    for candidate in candidates:
        if candidate.attribute not in spec_targets or candidate.attribute in grounded:
            continue
        if ground_attribute(candidate, source_fields) is not None:
            grounded[candidate.attribute] = candidate
        elif candidate.value is not None and not candidate.ambiguous:
            # The model asserted a value that did not literally ground — the guard drops it.
            dropped.append(
                DroppedCandidate(
                    attribute=candidate.attribute,
                    value=candidate.value,
                    source_field=candidate.source_field,
                )
            )

    fixes: list[Fix] = []

    for attribute in spec_targets:
        if attribute in grounded:
            candidate = grounded[attribute]
            fixes.append(
                Fix(
                    product_id=product_id, run_id=run_id,
                    type=FixType.metafield, status=FixStatus.proposed,
                    target=f"metafield:{_METAFIELD_NAMESPACE}.{attribute}",
                    before_json=None,
                    after_json=_metafield_object(attribute, candidate.value),
                    source_json=[_citation(attribute, candidate)],
                    diff=(
                        f"set metafield {_METAFIELD_NAMESPACE}.{attribute} = "
                        f"{candidate.value!r} (from {candidate.source_field})"
                    ),
                    base_source_hash=base_hash,
                )
            )
        else:
            fixes.append(
                Fix(
                    product_id=product_id, run_id=run_id,
                    type=FixType.merchant_todo, status=FixStatus.proposed,
                    target=f"spec:{attribute}", after_json=None,
                    reason=(
                        f"No {attribute.replace('_', ' ')} stated in any source field; "
                        "a merchant must add it."
                    ),
                )
            )

    if has_description_gap:
        if grounded:
            fixes.append(
                Fix(
                    product_id=product_id, run_id=run_id,
                    type=FixType.description, status=FixStatus.proposed,
                    target="body_html",
                    before_json={"body_html": product.body},
                    after_json={"body_html": _compose_description(product.body, grounded)},
                    source_json=[_citation(a, c) for a, c in grounded.items()],
                    diff="restructured description from grounded attributes",
                    base_source_hash=base_hash,
                )
            )
        else:
            fixes.append(
                Fix(
                    product_id=product_id, run_id=run_id,
                    type=FixType.merchant_todo, status=FixStatus.proposed,
                    target="body_html", after_json=None,
                    reason=(
                        "No extractable specs to compose a spec-dense description; "
                        "a merchant must write one."
                    ),
                )
            )

    if has_gtin_gap:
        fixes.append(
            Fix(
                product_id=product_id, run_id=run_id,
                type=FixType.merchant_todo, status=FixStatus.proposed,
                target="gtin", after_json=None,
                reason=(
                    "A barcode/GTIN cannot be derived from existing data; "
                    "a merchant must assign one."
                ),
            )
        )

    session.add_all(fixes)
    await session.commit()

    fillable = sum(1 for f in fixes if f.type in (FixType.metafield, FixType.description))
    todos = sum(1 for f in fixes if f.type == FixType.merchant_todo)
    return OptimizerReport(
        product_id=product_id, run_id=run_id, fillable=fillable, todos=todos, dropped=dropped
    )
