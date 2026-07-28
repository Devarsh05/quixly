"""Publisher node — the FIRST code that writes to a live merchant store (PRD §6, §13).

Consumes what the approval gate produced: ``fixes.status = approved``, and nothing else. Two
proven write paths only (``docs/decisions/2c-write-target.md`` L1), as **two risk tiers**, not one
publish loop:

* **description** (LOW) — an append-only ``productUpdate(descriptionHtml)``. Reversible, needs no
  extra scope, and is the field the Copilot agentic channel reads.
* **category** (HIGH) — ``productUpdate(category)`` with a scalar ``TaxonomyCategory`` GID. Real
  tax and sales-channel consequences; a wrong category is merchant harm.

The taxonomy **metafield** path is out of scope — blocked on ``read_metaobject_definitions`` (L6).
Step 3 refuses to approve those rows, and this node **asserts** that rather than trusting it: an
approved row of a non-publishable type means the gate was bypassed, and it aborts the run.

Token custody is unchanged
--------------------------
This node makes live Admin API writes through the injected ``ShopifyAdminClient`` →
``TokenProvider``, i.e. short-lived tokens minted by the app shell, which remains the **single
refresh authority**. The invariant is a single *refresh* authority, not a single *caller* — the
agent has read Shopify this way since Phase 1. This node fetches no token itself, stores none, and
must never construct its own ``TokenProvider``.

The three guarantees
--------------------
1. **Staleness — two hard layers, both evaluated BEFORE any write.** An approved fix was computed
   against a snapshot that may have moved. Layer 1 is exact: the live field must still equal
   ``before_json``, so a description is never appended to a body that changed and a category is
   never assigned over a merchant's own choice. Layer 2 is ``base_source_hash`` recomputed from the
   live read (``services.catalog.stable_source_hash``), which catches drift in fields the fix
   *grounded on* but does not *write*.
2. **A 200 is not a success.** ``productUpdate`` returns HTTP 200 with a non-empty ``userErrors``
   when it refuses, and even a clean write may not land as intended. Nothing reaches ``verified``
   without a **fresh re-read** — never the mutation's own return payload, which is the same round
   trip that may have lied.
3. **Replay cannot double-append.** Only ``approved`` rows are selected, and reconciliation runs
   *before* the staleness gate: a product whose live state already equals ``after_json`` had its
   write land before a crash, so it is verified with **zero mutations**. The only paths out of
   ``approved`` are "already landed" (no write) and "still equals ``before_json``" (safe to write);
   a body matching neither is ``stale``.
"""

import logging
from datetime import UTC, datetime
from html.parser import HTMLParser

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Fix, FixStatus, FixType, Product, Shop
from app.services.catalog import product_row_from_node, stable_source_hash, strip_html
from app.services.shopify_admin import ShopifyAdminClient, ShopifyAdminError

logger = logging.getLogger(__name__)

# The two proven write paths. Deliberately NOT derived from ``api.fixes.APPROVABLE_TYPES``: the two
# sets answer different questions ("may a merchant approve this?" vs "can we write it?") and
# unblocking one must never silently unblock the other.
PUBLISHABLE_TYPES = frozenset({FixType.description, FixType.category})

# Ascending risk. A systemic problem surfaces on the reversible write before any tax-relevant one.
_RISK_RANK = {FixType.description: 0, FixType.category: 1}


class PublishAborted(RuntimeError):
    """An invariant breach or a rollout guard. The run fails and NOTHING is written.

    Distinct from a per-fix failure on purpose: these mean our own data or configuration is wrong
    (the approval gate was bypassed, supersede failed, the shop is not cleared for publishing), and
    continuing would write to a store on the strength of state we know is broken.
    """


class FixOutcome(BaseModel):
    """What actually happened to one fix. Every attempted fix gets one — no silent skips."""

    fix_id: int
    product_id: int
    type: str
    status: str
    detail: str | None = None


class PublishReport(BaseModel):
    shop_domain: str
    run_id: int | None
    verified: int = 0
    stale: int = 0
    failed: int = 0
    outcomes: list[FixOutcome] = []


class _ListItems(HTMLParser):
    """Collect the normalised text of every ``<li>``.

    Used for the description re-read's structural fallback: it both proves the live body still
    parses as HTML and yields the appended lines to compare, without adding a dependency.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[str] = []
        self._depth = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "li":
            self._depth += 1
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._depth:
            self._depth -= 1
            self.items.append(_squash(" ".join(self._buffer)))
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._buffer.append(data)


def _squash(text: str) -> str:
    return " ".join(text.split())


def _list_items(html: str | None) -> list[str]:
    parser = _ListItems()
    parser.feed(html or "")
    parser.close()
    return [item for item in parser.items if item]


def _text(html: str | None) -> str:
    return _squash(strip_html(html))


def _body(payload: dict | list | None) -> str:
    return (payload or {}).get("body_html") or "" if isinstance(payload, dict) else ""


def _live_body(node: dict) -> str:
    return node.get("descriptionHtml") or ""


def _live_category_gid(node: dict) -> str | None:
    return (node.get("category") or {}).get("id")


def _live_category_name(node: dict) -> str | None:
    return (node.get("category") or {}).get("fullName") or None


def _target_state(fix: Fix, node: dict) -> tuple[str | None, str | None]:
    """``(live value, the value this fix WROTE)`` for the one field the fix touches.

    Equality of the two means the write has landed — which is both the reconciliation test (before
    any write) and the verification test (after one).
    """
    after = fix.after_json if isinstance(fix.after_json, dict) else {}
    if fix.type == FixType.description:
        return _live_body(node), after.get("body_html") or ""
    # ``after_json.category`` is the scalar TaxonomyCategory GID; ``before_json.category`` is the
    # stored fullName. The two halves of a category fix are deliberately different shapes.
    return _live_category_gid(node), after.get("category")


def _already_landed(fix: Fix, node: dict) -> bool:
    live, written = _target_state(fix, node)
    return bool(written) and live == written


def _base_matches(fix: Fix, node: dict) -> tuple[bool, str | None]:
    """Layer 1 of the staleness gate: does the live field still equal ``before_json`` exactly?

    This is what makes each write safe, and it is immune to the writer-shape problem that
    ``stable_source_hash`` exists to solve, because it compares one field against one stored value.
    """
    if fix.type == FixType.description:
        live, base = _live_body(node), _body(fix.before_json)
        if live == base:
            return True, None
        return False, (
            "the product description changed after this fix was approved, so appending to it "
            "could duplicate or corrupt the merchant's copy"
        )

    before = fix.before_json if isinstance(fix.before_json, dict) else {}
    base_name = before.get("category") or None
    live_name = _live_category_name(node)
    if live_name == base_name:
        return True, None
    return False, (
        f"the product category changed after this fix was approved "
        f"({base_name!r} → {live_name!r}); assigning ours would clobber that choice"
    )


def _verify(fix: Fix, node: dict) -> tuple[bool, str | None]:
    """Did the write land as intended? Read from a FRESH fetch, never the mutation's payload."""
    live, written = _target_state(fix, node)
    if live == written:
        return True, None

    if fix.type == FixType.category:
        # A scalar GID: there is no benign way for this to differ. No fallback.
        return False, f"Shopify reports category {live!r}, not the {written!r} we wrote"

    # Whether Shopify echoes ``descriptionHtml`` back byte-for-byte is unproven, so a non-exact
    # match falls back to a STRUCTURAL check rather than being guessed either way: the merchant's
    # original copy must still be there, and every line we appended must be present as a parsed
    # list item. Anything less is a failed write.
    original = _text(_body(fix.before_json))
    appended = _list_items(written[len(_body(fix.before_json)) :])
    live_items = _list_items(live)

    if original and original not in _text(live):
        return False, "the merchant's original description is no longer present after the write"
    if missing := [item for item in appended if item not in live_items]:
        return False, f"the live description is missing the appended line(s) {missing}"

    logger.warning(
        "Fix %s verified structurally, not byte-for-byte — Shopify normalised the HTML it "
        "echoed back. Appended lines and original copy are all present.",
        fix.id,
    )
    return True, None


def _write_input(product_gid: str, fix: Fix) -> dict:
    after = fix.after_json if isinstance(fix.after_json, dict) else {}
    if fix.type == FixType.description:
        return {"id": product_gid, "descriptionHtml": after.get("body_html") or ""}
    return {"id": product_gid, "category": after.get("category")}


def _rewind(node: dict, landed: list[Fix]) -> dict:
    """The live node with any already-landed write undone, for the Layer-2 hash comparison.

    Without this, a fix that landed before a crash would move its own product's hash and falsely
    stale every sibling fix queued behind it. Only ``description`` touches a hashed field, so the
    rewind is exactly "put the pre-publish body back".
    """
    rewound = dict(node)
    for fix in landed:
        if fix.type == FixType.description:
            rewound["descriptionHtml"] = _body(fix.before_json)
    return rewound


def _allowed_shops(raw: str) -> set[str]:
    return {domain.strip().casefold() for domain in raw.split(",") if domain.strip()}


def _outcome(fix: Fix, detail: str | None = None) -> FixOutcome:
    return FixOutcome(
        fix_id=fix.id,
        product_id=fix.product_id,
        type=fix.type,
        status=fix.status,
        detail=detail,
    )


def _mark(fix: Fix, status: FixStatus, detail: str | None = None) -> FixOutcome:
    """Apply a terminal state. ``publish_error`` carries the cause; ``reason`` is never touched."""
    fix.status = status
    if status == FixStatus.verified:
        # Set ONLY here — on a confirmed re-read, never on the mutation's 200.
        fix.published_at = datetime.now(UTC)
        fix.publish_error = None
    else:
        fix.publish_error = detail
    return _outcome(fix, detail)


async def _publish_product(
    session: AsyncSession,
    client: ShopifyAdminClient,
    product: Product,
    fixes: list[Fix],
) -> list[FixOutcome]:
    """Publish one product's approved fixes: one snapshot, gate, write in risk order, re-read."""
    outcomes: list[FixOutcome] = []
    live = await client.fetch_product(product.shopify_product_id)

    if live is None:
        for fix in fixes:
            outcomes.append(
                _mark(fix, FixStatus.publish_failed, "the product no longer exists in Shopify")
            )
        await session.commit()
        return outcomes

    # 1. RECONCILIATION, before the staleness gate. A fix whose write already landed (we crashed
    #    between the write and recording it) is verified here against this fresh read, with zero
    #    mutations. This is what makes an append-only replay structurally impossible.
    landed = [fix for fix in fixes if _already_landed(fix, live)]
    landed_ids = {fix.id for fix in landed}
    pending = [fix for fix in fixes if fix.id not in landed_ids]
    for fix in landed:
        logger.info("Fix %s was already live on Shopify — reconciling without a write", fix.id)
        outcomes.append(_mark(fix, FixStatus.verified))

    # 2. STALENESS LAYER 2 — the writer-stable source hash, over the snapshot rewound by any
    #    already-landed write, so our own earlier publish cannot stale its siblings.
    live_hash = stable_source_hash(product_row_from_node(_rewind(live, landed)))
    still_pending: list[Fix] = []
    for fix in pending:
        if fix.base_source_hash and fix.base_source_hash != live_hash:
            outcomes.append(
                _mark(
                    fix,
                    FixStatus.stale,
                    "the product data this fix was grounded on has changed since it was approved",
                )
            )
        else:
            if not fix.base_source_hash:
                logger.warning("Fix %s carries no base_source_hash; Layer 2 skipped", fix.id)
            still_pending.append(fix)

    # 3. STALENESS LAYER 1 — exact ``before_json`` equality, plus the append-only precondition.
    writable: list[Fix] = []
    for fix in still_pending:
        matches, why = _base_matches(fix, live)
        if not matches:
            outcomes.append(_mark(fix, FixStatus.stale, why))
            continue
        after, before = _body(fix.after_json), _body(fix.before_json)
        if fix.type == FixType.description and not after.startswith(before):
            # Enforced, never assumed: the composer appends, and a fix that does not is a bug we
            # refuse to write rather than overwrite merchant copy with.
            outcomes.append(
                _mark(
                    fix,
                    FixStatus.publish_failed,
                    "refused: this fix would overwrite the description rather than append to it",
                )
            )
            continue
        writable.append(fix)

    await session.commit()  # persist every no-write outcome before touching Shopify

    # 4. WRITES, ascending risk: the reversible description before the tax-relevant category.
    written: list[Fix] = []
    for fix in sorted(writable, key=lambda f: _RISK_RANK[f.type]):
        try:
            await client.update_product(_write_input(product.shopify_product_id, fix))
        except ShopifyAdminError as exc:
            # Per-fix isolation: one product's bad state must not block the rest of the batch.
            # TokenUnavailableError/TokenFetchError deliberately do NOT land here — they mean
            # nothing will work, and they halt the run.
            logger.exception("Publish failed for fix %s", fix.id)
            outcomes.append(_mark(fix, FixStatus.publish_failed, str(exc)))
            continue
        # ``published`` is committed BEFORE the verifying re-read, so a crash in between leaves a
        # state the next run reconciles rather than re-writes.
        fix.status = FixStatus.published
        written.append(fix)
    await session.commit()

    if not written:
        return outcomes

    # 5. VERIFY from a FRESH read. The mutation's own return payload is not evidence.
    confirmed = await client.fetch_product(product.shopify_product_id)
    if confirmed is None:
        for fix in written:
            outcomes.append(
                _mark(fix, FixStatus.publish_failed, "could not re-read the product to verify")
            )
        await session.commit()
        return outcomes

    for fix in written:
        verified, why = _verify(fix, confirmed)
        if verified:
            outcomes.append(_mark(fix, FixStatus.verified))
        else:
            outcomes.append(_mark(fix, FixStatus.publish_failed, why))

    # Keep our copy of the product truthful. The products/update webhook our own write triggers
    # will also fire, but it carries the REST variant shape and may lose the race; this write is
    # the authoritative GraphQL-shaped one.
    for column, value in product_row_from_node(confirmed).items():
        setattr(product, column, value)
    product.updated_at = datetime.now(UTC)

    await session.commit()
    return outcomes


async def run_publisher(
    session: AsyncSession,
    shop: Shop,
    client: ShopifyAdminClient,
    *,
    run_id: int | None = None,
    allowed_shops: str,
) -> PublishReport:
    """Publish every approved fix for ``shop``. The node's single entry point.

    Not scoped to ``run_id``: approvals are merchant decisions that outlive the run that proposed
    them, so an approved row from an earlier run is still publishable. ``run_id`` identifies the
    PUBLISH run, for reporting.
    """
    if shop.shop_domain.casefold() not in _allowed_shops(allowed_shops):
        # Staged rollout enforced in code, not discipline: no real merchant store until the full
        # write → re-read → verify path is proven end-to-end on the dev store.
        raise PublishAborted(
            f"{shop.shop_domain} is not cleared for publishing "
            f"(PUBLISH_ALLOWED_SHOPS); refusing to write."
        )

    rows = (
        await session.execute(
            select(Fix, Product)
            .join(Product, Product.id == Fix.product_id)
            .where(Product.shop_id == shop.id, Fix.status == FixStatus.approved)
            .order_by(Product.id, Fix.id)
        )
    ).all()

    # INVARIANT: nothing non-publishable can be approved. Step 3 enforces this server-side, so a
    # violation means the gate was bypassed — abort rather than "helpfully" skipping the row.
    for fix, _ in rows:
        if fix.type not in PUBLISHABLE_TYPES or fix.after_json is None:
            raise PublishAborted(
                f"fix {fix.id} is approved but type={fix.type!r} with "
                f"after_json={'set' if fix.after_json is not None else 'NULL'} — the approval "
                f"gate was bypassed; nothing was published."
            )

    # INVARIANT: at most ONE approved fix per (product, target). Step 3's supersede guarantees it,
    # and the append-only description write depends on it — two approved rows for one body would
    # append the Details block twice.
    seen: dict[tuple[int, str], int] = {}
    for fix, _ in rows:
        key = (fix.product_id, fix.target)
        if key in seen:
            raise PublishAborted(
                f"fixes {seen[key]} and {fix.id} are both approved for product {fix.product_id} "
                f"target {fix.target!r} — supersede failed; nothing was published."
            )
        seen[key] = fix.id

    by_product: dict[int, tuple[Product, list[Fix]]] = {}
    for fix, product in rows:
        by_product.setdefault(product.id, (product, []))[1].append(fix)

    # Low-risk products first: a product carrying a category fix publishes after every
    # description-only one, so a systemic failure surfaces before any tax-relevant write.
    ordered = sorted(
        by_product.values(),
        key=lambda entry: (
            any(fix.type == FixType.category for fix in entry[1]),
            entry[0].id,
        ),
    )

    report = PublishReport(shop_domain=shop.shop_domain, run_id=run_id)
    for product, fixes in ordered:
        report.outcomes.extend(await _publish_product(session, client, product, fixes))

    for outcome in report.outcomes:
        if outcome.status == FixStatus.verified:
            report.verified += 1
        elif outcome.status == FixStatus.stale:
            report.stale += 1
        else:
            report.failed += 1

    logger.info(
        "Publish run %s for %s: %d verified, %d stale, %d failed",
        run_id, shop.shop_domain, report.verified, report.stale, report.failed,
    )
    return report
