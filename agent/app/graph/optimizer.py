"""Optimizer/Writer node — proposes GROUNDED fixes from a product's own data (PRD §6, §13).

The Optimizer **extracts and restructures — it never generates.** The only grounding sources are
the product's own persisted fields (title, body, variants_json, metafields_json, product_type). An
LLM proposes candidate attribute values; the **grounding guard** (``ground_attribute`` →
``services.matching.is_grounded``, the same primitive the Extractor uses) is what actually decides
what survives: a value is kept only if the model's cited snippet is literally in the named source
field AND the value is literally in that snippet. A hallucinated value is dropped here, not trusted.

**What it targets (step 2b, applicability-gated in 2d).** Every spec family APPLICABLE to the
product (``applicable_families``) that it does not already carry as a TAXONOMY-attribute metafield —
a structural set, NOT the audit's gap list. Each grounded value is then ROUTED (step 2d, after the
2c write-target spike proved ``custom.*`` is not the AI-legible channel):

* **PRIMARY — description** fix: the dominant path. A spec-dense block composed only from grounded
  attributes (grounded by construction, "restructure existing content, no new claims"), appended
  HTML-safe to the body. Reaches ALL families — including the four with no taxonomy home (process,
  variety, altitude, brew_method) — and has NO dependency on category assignment.
* **SECONDARY — metafield** fix: for the four taxonomy-home families (roast, origin,
  coffee_product_form, decaffeination_method), a ``shopify`` taxonomy-attribute metafield. Gated on
  the product having an assigned category AND the value mapping to a canonical taxonomy value
  (``services.taxonomy_map``); it carries the ``taxonomy_value_gid`` for the Step-4 Publisher to
  resolve to a per-shop metaobject GID. No ``custom.*`` is ever written.
* **category** fix (new): assign a Standard-taxonomy category — the precondition for any taxonomy
  write. APPROVAL-GATED like a publish. Grounded from the product's own data.
* **merchant_todo** — every applicable gap that could NOT be grounded (absent/ambiguous), and
  always ``missing_gtin`` (a barcode cannot be derived — PRD §13). ``after_json = NULL``.

**NEGATIVE claims are grounded too (step 2e).** A to-do is a merchant-facing assertion about the
product, and a false one survives the approval gate because it reads as advice rather than a diff.
So absence is guarded exactly the way presence is. Extraction returning nothing is **not** evidence
of absence — it is routinely a flaky-LLM miss — so before a family is written off:

1. **Deterministic recovery** (``audit_rubric.recover_spec_value``) reads the value straight out of
   the source text with no LLM, keyed on the same ``SPEC_FAMILIES`` vocabulary the audit detects
   with. It PROPOSES a candidate for the unchanged guards; it never bypasses them.
2. **The negative literal-presence guard** then permits the ``absent`` claim only when no
   vocabulary token for the family is in ANY source field **and** the deterministic audit did not
   classify it ``unstructured``. Otherwise the to-do is downgraded to the truthful
   ``mentioned_no_value`` tier, which cites what was found.

Both outcomes still terminate in {taxonomy fill, description line, to-do}, so the gap→row
accounting invariant below is unchanged — recovery only moves a family between existing routes.

Convergence: once a taxonomy metafield is published a family becomes ``structured`` and drops from
targets. A taxonomy-less family has no structured channel, so its labeled description line is the
terminal legibility action — proposals stay deterministic run-to-run (idempotent over the same
state); the Publisher (Step 4) must not re-append a Details block already present. No Shopify
writes here: this node only persists ``fixes`` rows (``status = proposed``); the Publisher
executes writes behind the approval gate. The LLM client is injected so tests drive a scripted
client against the transaction-scoped ``db`` fixture.
"""

from html import escape

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Audit, Fix, FixStatus, FixType, Product
from app.services.audit_rubric import (
    SEVERITY_NOT_AUDITED,
    SPEC_MISSING,
    SPEC_SCORED_CLASSES,
    STATE_UNSTRUCTURED,
    applicable_families,
    detect_hit_in_fields,
    recover_spec_value,
    structured_families,
    validate_spec_value,
)
from app.services.catalog import stable_source_hash, strip_html
from app.services.matching import is_grounded
from app.services.optimizer_llm import AttributeCandidate, OptimizerClient
from app.services.taxonomy_map import (
    CATEGORY_COFFEE_BEANS_GROUND,
    CATEGORY_COFFEE_CONCENTRATE,
    resolve_taxonomy_value,
)


class DroppedCandidate(BaseModel):
    """A candidate a guard dropped, with which guard fired.

    ``reason``: ``fabrication`` — value/snippet was not literally in source (anti-hallucination);
    ``mis_assignment`` — the value was literally present but is not valid for the target family
    (e.g. a process term grounded as a brew_method). Either way the gap stays unfilled → a to-do.
    """

    attribute: str
    value: str
    source_field: str | None
    reason: str


class RecoveredCandidate(BaseModel):
    """A family the DETERMINISTIC recovery rescued after the LLM extractor failed to ground it.

    ``missed_as`` is the drop reason the to-do would have carried had recovery not fired —
    ``absent`` means a **false absence claim was prevented** (the step-2e bug), so this list is
    both the flakiness telemetry and the count that must go to zero for false to-dos.
    """

    attribute: str
    value: str
    source_field: str
    missed_as: str


class OptimizerReport(BaseModel):
    """The Optimizer node's typed return."""

    product_id: int
    run_id: int | None
    fillable: int
    todos: int
    dropped: list[DroppedCandidate]
    recovered: list[RecoveredCandidate] = []


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
        "body_html": strip_html(product.body).strip(),
        "variants_json": _variants_text(product.variants_json),
        "metafields": _metafields_text(product.metafields_json),
        "product_type": product.product_type or "",
    }


_CONCENTRATE_TERMS = ("concentrate", "instant")


def _taxonomy_metafield_object(handle: str, canonical_value: str, taxonomy_value_gid: str) -> dict:
    """A taxonomy attribute metafield fix payload (step 2d).

    The ``shopify`` namespace category attribute is ``list.metaobject_reference`` and rejects the
    canonical ``TaxonomyValue`` GID (spike L1) — so this carries the ``taxonomy_value_gid`` + the
    canonical value, and the Step-4 Publisher resolves the per-shop metaobject-entry GID at write
    time (via ``read_metaobjects``). No ``custom.*`` is ever written.
    """
    return {
        "namespace": "shopify",
        "key": handle,
        "type": "list.metaobject_reference",
        "taxonomy_value_gid": taxonomy_value_gid,
        "value": canonical_value,
    }


def has_taxonomy_category(category: str | None) -> bool:
    """True iff the product carries a real Standard-taxonomy category (not ``Uncategorized``/NULL).

    The hard precondition for any taxonomy attribute write: category attributes do not exist until a
    category is assigned (spike §5). ``category`` is the stored taxonomy ``fullName``.
    """
    return bool(category) and "uncategorized" not in category.casefold()


def _infer_coffee_category(product: Product) -> tuple[str, str, dict] | None:
    """Infer the Standard-taxonomy category for a coffee product → ``(gid, fullName, citation)``.

    GROUNDED in the product's own data (never fabricated): concentrate/instant → coffee concentrates
    (``fb-1-3-5``), otherwise whole-bean/ground coffee (``fb-1-3-1``). Returns ``None`` if no coffee
    signal can be cited (then no auto-proposal — a merchant to-do, per the approval bar).
    """
    for field in ("product_type", "title", "body"):
        raw = getattr(product, field) if field != "body" else strip_html(product.body)
        text = (raw or "")
        low = text.casefold()
        if not low.strip():
            continue
        if any(term in low for term in _CONCENTRATE_TERMS):
            snippet = next(t for t in _CONCENTRATE_TERMS if t in low)
            return (
                CATEGORY_COFFEE_CONCENTRATE,
                "Food, Beverages & Tobacco > Beverages > Coffee > Coffee Concentrates",
                {"attribute": "category", "source_field": field, "snippet": snippet},
            )
    # Default coffee category — cite the field that classified it as coffee.
    for field in ("product_type", "title"):
        text = (getattr(product, field) or "")
        if text.strip():
            return (
                CATEGORY_COFFEE_BEANS_GROUND,
                "Food, Beverages & Tobacco > Beverages > Coffee > Coffee Beans & Ground Coffee",
                {"attribute": "category", "source_field": field, "snippet": text},
            )
    return None


def _citation(attribute: str, candidate: AttributeCandidate) -> dict:
    return {
        "attribute": attribute,
        "source_field": candidate.source_field,
        "snippet": candidate.snippet,
    }


def _compose_description(body_html: str | None, grounded: dict[str, AttributeCandidate]) -> str:
    """APPEND a valid-HTML Details list to the ORIGINAL body_html; existing markup is untouched.

    The column is ``body_html`` — the written value must stay valid HTML and preserve the
    merchant's markup byte-for-byte. HTML stripping is for EXTRACTION INPUT ONLY
    (``_build_source_fields``) and must never reach ``after_json``. Values are HTML-escaped.
    """
    items = "".join(
        f"<li>{escape(attr.replace('_', ' ').title())}: {escape(cand.value or '')}</li>"
        for attr, cand in grounded.items()
    )
    block = f"<p><strong>Details</strong></p><ul>{items}</ul>"
    return f"{body_html or ''}{block}"


# Drop reason for "the family IS mentioned in source, but no specific value could be read" — the
# third to-do tier added in step 2e. It exists so the ABSENCE claim never has to be stretched to
# cover a family that is demonstrably present (e.g. a body saying "single-origin" with no country).
DROP_MENTIONED_NO_VALUE = "mentioned_no_value"
DROP_ABSENT = "absent"


def _todo_reason(
    attribute: str,
    drop_reason: str,
    value: str | None,
    source_field: str | None,
    mention: tuple[str, str] | None = None,
) -> str:
    """Merchant-facing reason for an unfilled spec gap — truthful about WHY it is unfilled.

    Every branch is a claim the node can defend from evidence it holds. In particular the
    ``absent`` branch is reachable ONLY behind the negative grounding guard in ``run_optimizer``
    (step 2e): the family must be literally absent from every source field AND not classified
    ``unstructured`` by the deterministic audit.
    """
    family = attribute.replace("_", " ")
    if drop_reason == "mis_assignment":
        return (
            f"A value ({value!r}) was found in {source_field} but did not validate as {family}; "
            f"a merchant must add a valid {family}."
        )
    if drop_reason == "fabrication":
        # Merchant-facing: never expose what the model proposed.
        return f"No verified {family} was found in your product data; a merchant must add it."
    if drop_reason == DROP_MENTIONED_NO_VALUE:
        if mention is not None:
            field, phrase = mention
            return (
                f"{family} is referred to in {field} ({phrase!r}) but no specific {family} value "
                f"could be read; a merchant must state it explicitly."
            )
        # No live mention: the audit classified the family as stated in the description. Its own
        # gap detail is the evidence; we still refuse to claim absence.
        return (
            f"The audit found {family} stated in this product's description, but no specific "
            f"value could be read; a merchant must state it explicitly."
        )
    return f"No {family} stated in any source field; a merchant must add it."


def _todo_evidence(
    attribute: str,
    drop_reason: str,
    rejected_value: str | None,
    rejected_field: str | None,
    mention: tuple[str, str] | None,
    audit_state: str | None,
) -> list[dict] | None:
    """``source_json`` for a spec to-do — SQL NULL **only** for a true absence claim.

    A genuinely absent family has no evidence to cite, so it keeps the established NULL. Every
    other tier carries the evidence behind its claim, queryable by ``drop_reason``.
    """
    if drop_reason == DROP_ABSENT:
        return None
    if drop_reason == DROP_MENTIONED_NO_VALUE:
        field, phrase = mention if mention is not None else (None, None)
        return [{
            "attribute": attribute, "source_field": field, "detected_phrase": phrase,
            "audit_state": audit_state, "drop_reason": drop_reason,
        }]
    return [{
        "attribute": attribute, "source_field": rejected_field,
        "rejected_value": rejected_value, "drop_reason": drop_reason,
    }]


async def run_optimizer(
    session: AsyncSession,
    product_id: int,
    client: OptimizerClient,
    *,
    run_id: int | None = None,
) -> OptimizerReport:
    """Propose grounded fixes for a product. Persists ``fixes`` rows.

    Spec targets are the families the product does not already carry as a metafield (see the module
    docstring); the audit is still required — for the not-audited exclusion, the product class, and
    the description/GTIN gaps — but it no longer decides which specs are targeted.

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
    source_fields = _build_source_fields(product)
    # The staleness guard the Publisher re-computes from a LIVE re-read, so it must be
    # writer-stable — see ``stable_source_hash``. Deliberately NOT hashed over ``source_fields``:
    # that projection feeds extraction, and it hashes raw ``variants_json``, whose stored shape
    # differs between the ingest writer and the products/update webhook writer.
    base_hash = stable_source_hash(product)

    # TARGETING IS STRUCTURAL, NOT GAP-DERIVED (step 2b), and APPLICABILITY-GATED (step 2d). Targets
    # are the spec families APPLICABLE to this product (``applicable_families`` drops
    # decaffeination_method for a non-decaf coffee, the same way GTIN is equipment-only) that it
    # does not already carry as a TAXONOMY-attribute metafield. This deliberately does NOT read the
    # audit's gaps or the rubric's ``detect`` list: a fill needs an *extractable* value, so when
    # targeting followed audit gaps a fill could only ever happen where ``detect`` had FAILED to
    # notice a spec sitting in the prose ("Roast: Medium-Light" is missed; "light roast" is not).
    # Detection quality and fill capability were inversely coupled — deriving targets structurally
    # severs that: refining ``detect`` moves only the audit's coverage/severity numbers.
    #
    # Which target is fillable vs *absent* (a to-do) is decided by extraction + the unchanged
    # grounding/validation guards below — not by ``detect``. Gated on the PERSISTED product_class
    # (never re-classified) — without that gate an equipment product would be asked for coffee
    # families, since no gap list is filtering it. ``applicable_families`` uses the combined source
    # text so a decaf product (decaf named anywhere in its data) is targeted for its decaf method.
    spec_targets: list[str] = []
    if audit.product_class in SPEC_SCORED_CLASSES:
        source_text = " ".join(source_fields.values())
        spec_targets = sorted(
            set(applicable_families(source_text)) - structured_families(product.metafields_json)
        )
    # The description/GTIN gaps are still read from the audit — those are genuine rubric findings
    # about the product, not a targeting decision.
    has_description_gap = any(g.get("code") == "missing_description" for g in gaps)
    has_gtin_gap = any(g.get("code") == "missing_gtin" for g in gaps)

    # THE AUTHORITY FOR ABSENCE (step 2e). The audit's three-state classification is deterministic
    # and literal; extraction is an LLM and is flaky. So for the ABSENCE question the audit wins:
    # a family the audit called ``unstructured`` (stated in the prose) may never be reported absent,
    # no matter what extraction did this run. This does NOT re-couple targeting to the audit — the
    # detect-proxy and the extraction split are still free to diverge (see ``audit_rubric``); the
    # audit gates only which merchant-facing CLAIM is permissible, never which families are
    # targeted.
    audit_states: dict[str, str] = {
        gap["attribute"]: gap.get("state") or ""
        for gap in gaps
        if gap.get("code") == SPEC_MISSING and gap.get("attribute")
    }

    extracted = await client.extract(source_fields, spec_targets) if spec_targets else None
    candidates = extracted.attributes if extracted else []

    grounded: dict[str, AttributeCandidate] = {}
    dropped: list[DroppedCandidate] = []
    # Per-attribute unfillability, so the persisted to-do can say WHY (queryable, not only in the
    # in-memory report): attribute -> (drop_reason, rejected_value, source_field).
    unfillable: dict[str, tuple[str, str | None, str | None]] = {}
    for candidate in candidates:
        if candidate.attribute not in spec_targets or candidate.attribute in grounded:
            continue
        value = ground_attribute(candidate, source_fields)
        if value is None:
            if candidate.value is not None and not candidate.ambiguous:
                # Asserted a value that is not literally in source — anti-hallucination drop.
                dropped.append(
                    DroppedCandidate(
                        attribute=candidate.attribute, value=candidate.value,
                        source_field=candidate.source_field, reason="fabrication",
                    )
                )
                unfillable[candidate.attribute] = (
                    "fabrication", candidate.value, candidate.source_field
                )
        elif not validate_spec_value(candidate.attribute, value, candidate.snippet or ""):
            # Literally present, but not valid for THIS family — mis-assignment drop.
            dropped.append(
                DroppedCandidate(
                    attribute=candidate.attribute, value=value,
                    source_field=candidate.source_field, reason="mis_assignment",
                )
            )
            unfillable[candidate.attribute] = (
                "mis_assignment", value, candidate.source_field
            )
        else:
            grounded[candidate.attribute] = candidate

    # DETERMINISTIC RECOVERY (step 2e). Extraction returning nothing is NOT evidence of absence —
    # it is routinely a flaky-LLM miss (run 839 missed ``origin`` on a product titled "Ethiopia
    # Yirgacheffe"). Before any family is written off, try to read its value straight out of the
    # source text with the LLM-free scanner, which is keyed on the SAME ``SPEC_FAMILIES`` vocabulary
    # the audit detects with. Runs for EVERY ungrounded target regardless of why extraction failed
    # (null return, fabrication, mis-assignment) — all three are extraction failures, and a to-do is
    # equally wrong in all three when the value is sitting in the title.
    #
    # Recovery PROPOSES, it does not bypass: every recovered value goes through the exact same
    # ``ground_attribute`` + ``validate_spec_value`` guards as an LLM candidate, so there is no
    # second, weaker path into a fix. A recovered family then routes through the unchanged
    # taxonomy/description logic below, which is why the gap→row accounting invariant is untouched.
    recovered: list[RecoveredCandidate] = []
    for attribute in spec_targets:
        if attribute in grounded:
            continue
        recovery = recover_spec_value(attribute, source_fields)
        if recovery is None:
            continue
        candidate = AttributeCandidate(
            attribute=attribute, value=recovery.value,
            source_field=recovery.source_field, snippet=recovery.snippet, ambiguous=False,
        )
        value = ground_attribute(candidate, source_fields)
        if value is None or not validate_spec_value(attribute, value, candidate.snippet or ""):
            continue
        grounded[attribute] = candidate
        recovered.append(
            RecoveredCandidate(
                attribute=attribute, value=value, source_field=recovery.source_field,
                missed_as=unfillable.get(attribute, (DROP_ABSENT, None, None))[0],
            )
        )

    fixes: list[Fix] = []
    product_categorized = has_taxonomy_category(product.category)
    # Accounting: every spec target lands in EXACTLY ONE of these three outcomes (asserted below).
    taxonomy_filled: set[str] = set()  # got a taxonomy metafield — its structured pair
    todo_families: set[str] = set()    # not grounded — a merchant to-do

    for attribute in spec_targets:
        if attribute in grounded:
            candidate = grounded[attribute]
            # SECONDARY (taxonomy) path. A taxonomy-home family whose grounded value maps to a
            # canonical taxonomy value, on a product with an assigned category, becomes a taxonomy
            # metafield fix (``shopify`` namespace). Gated on BOTH:
            #   * an assigned category (``product_categorized``) — category attributes do not exist
            #     until a category is set (the dependency chain); without one, wait for a re-run
            #     after the category fix is approved + published.
            #   * the value being a member of the family's taxonomy attribute (``resolve_taxonomy_
            #     value``) — an origin *region* like "Yirgacheffe" is not a taxonomy ``Country``.
            # A family that is not taxonomy-home, not yet categorized, or whose value does not map
            # is NOT dropped and is NOT a to-do: it is grounded, so it is carried by the PRIMARY
            # description path below (its labeled Details line IS its structured pair, since it has
            # no taxonomy channel). No ``custom.*`` is ever written.
            resolved = (
                resolve_taxonomy_value(attribute, candidate.value or "")
                if product_categorized
                else None
            )
            if resolved is not None:
                handle, canonical_value, taxonomy_value_gid = resolved
                taxonomy_filled.add(attribute)
                fixes.append(
                    Fix(
                        product_id=product_id, run_id=run_id,
                        type=FixType.metafield, status=FixStatus.proposed,
                        target=f"metafield:shopify.{handle}",
                        before_json=None,
                        after_json=_taxonomy_metafield_object(
                            handle, canonical_value, taxonomy_value_gid
                        ),
                        source_json=[_citation(attribute, candidate)],
                        diff=(
                            f"set taxonomy attribute shopify.{handle} = {canonical_value!r} "
                            f"({taxonomy_value_gid}) (from {candidate.source_field})"
                        ),
                        base_source_hash=base_hash,
                    )
                )
        else:
            todo_families.add(attribute)
            drop_reason, rejected_value, rejected_field = unfillable.get(
                attribute, (DROP_ABSENT, None, None)
            )
            # THE NEGATIVE LITERAL-PRESENCE GUARD (step 2e) — the mirror of ``ground_attribute``.
            # A fill must be literally present to be claimed; an ABSENCE must be literally absent.
            # Two independent conditions, either of which forbids the absence claim:
            #   * a vocabulary token for the family is literally in SOME source field — checked
            #     across ALL of them, because the claim itself says "any source field" (a superset
            #     of the audit's title+body prose); or
            #   * the deterministic audit classified the family ``unstructured``.
            # The audit only ever RAISES the bar, never lowers it: a stale audit saying
            # ``unstructured`` against a since-edited product still blocks the claim, which is the
            # conservative direction. Recovery already ran above, so reaching here means no value
            # could be read — the to-do stands, only its CLAIM is downgraded to a truthful one.
            mention = detect_hit_in_fields(attribute, source_fields)
            audit_state = audit_states.get(attribute)
            if drop_reason == DROP_ABSENT and (
                mention is not None or audit_state == STATE_UNSTRUCTURED
            ):
                drop_reason = DROP_MENTIONED_NO_VALUE
            fixes.append(
                Fix(
                    product_id=product_id, run_id=run_id,
                    type=FixType.merchant_todo, status=FixStatus.proposed,
                    target=f"spec:{attribute}", after_json=None,
                    reason=_todo_reason(
                        attribute, drop_reason, rejected_value, rejected_field, mention
                    ),
                    # Queryable drop category; SQL NULL only for a true absence claim.
                    source_json=_todo_evidence(
                        attribute, drop_reason, rejected_value, rejected_field, mention, audit_state
                    ),
                )
            )

    # NEW FIX TYPE (step 2d): assign a Standard-taxonomy category — the hard precondition for every
    # taxonomy attribute write. Proposed for a coffee product that lacks one, APPROVAL-GATED like a
    # publish (a wrong category has real tax/channel consequences). Grounded in the product's own
    # data via ``_infer_coffee_category``; if no coffee signal can be cited, no auto-proposal.
    if audit.product_class in SPEC_SCORED_CLASSES and not product_categorized:
        inferred = _infer_coffee_category(product)
        if inferred is not None:
            category_gid, full_name, citation = inferred
            fixes.append(
                Fix(
                    product_id=product_id, run_id=run_id,
                    type=FixType.category, status=FixStatus.proposed,
                    target="category",
                    before_json={"category": product.category},
                    after_json={"category": category_gid, "fullName": full_name},
                    source_json=[citation],
                    diff=f"assign product category {full_name} ({category_gid})",
                    base_source_hash=base_hash,
                )
            )

    # PRIMARY path: a labeled Details line for every grounded family that did NOT get a taxonomy
    # metafield (its structured pair). A taxonomy-less family — or a taxonomy-home family not yet
    # categorized / whose value is not a taxonomy member — has NO structured channel, so the labeled
    # pair ("Process: Washed") IS its agent-legible structured surface, even when the raw words
    # already appear in the body: a labeled attribute-value pair is more machine-legible than a spec
    # buried in a sentence. Taxonomy-filled families are excluded — re-appending would duplicate the
    # pair the metafield already carries (the de-dup the old source-based gate was really for; that
    # gate broke once 2d retired the custom.* metafield that used to catch body-grounded specs).
    # ``_compose_description`` preserves the merchant's HTML byte-for-byte and only appends.
    description_families = {
        attr: cand for attr, cand in grounded.items() if attr not in taxonomy_filled
    }
    if description_families:
        fixes.append(
            Fix(
                product_id=product_id, run_id=run_id,
                type=FixType.description, status=FixStatus.proposed,
                target="body_html",
                before_json={"body_html": product.body},
                after_json={"body_html": _compose_description(product.body, description_families)},
                source_json=[_citation(a, c) for a, c in description_families.items()],
                diff=(
                    f"appended {len(description_families)} labeled spec line(s) to the "
                    "description; existing prose unchanged"
                ),
                base_source_hash=base_hash,
            )
        )
    elif has_description_gap:
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

    # GAP→ROW ACCOUNTING INVARIANT. Every spec target must land in EXACTLY ONE outcome — a taxonomy
    # metafield fill, a description line, or a to-do — never zero. 2d shipped a silent hole here (a
    # body-grounded taxonomy-less family produced no row at all); this converts any recurrence into
    # a loud failure at the source. The node is deterministic given ``grounded``/``spec_targets``,
    # so a violation is strictly a routing bug, not a data condition.
    accounted = taxonomy_filled | set(description_families) | todo_families
    if accounted != set(spec_targets):
        raise AssertionError(
            f"optimizer routing hole for product {product_id}: spec targets "
            f"{sorted(set(spec_targets) - accounted)} produced no fix and no to-do"
        )
    if not (
        taxonomy_filled.isdisjoint(todo_families)
        and taxonomy_filled.isdisjoint(description_families)
        and todo_families.isdisjoint(description_families)
    ):
        raise AssertionError(f"optimizer double-routed a spec target for product {product_id}")

    session.add_all(fixes)
    await session.commit()

    fillable = sum(
        1 for f in fixes if f.type in (FixType.metafield, FixType.description, FixType.category)
    )
    todos = sum(1 for f in fixes if f.type == FixType.merchant_todo)
    return OptimizerReport(
        product_id=product_id, run_id=run_id, fillable=fillable, todos=todos, dropped=dropped,
        recovered=recovered,
    )
