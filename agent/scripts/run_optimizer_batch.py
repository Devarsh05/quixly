"""Gate L evidence runner — audit + optimize a shop's catalog and report what actually happened.

A THIN WRAPPER around ``app.jobs.fix.propose_fixes_for_shop`` — the same entrypoint the step-3
route/task calls. It deliberately reimplements no node sequencing, no targeting and no validation;
if a number here looks wrong, the bug is in the graph, not in this script.

**Read-only against Shopify.** It reads already-ingested ``products`` rows and persists ``audits``
+ ``fixes`` (``status = proposed``). There is no publish path — that is step 4, behind the approval
gate.

Two report sections, kept apart on purpose:

* **DETERMINISTIC** — severity histogram + the three-state table. Pure functions of catalog fields,
  so these must be **byte-identical run to run**. If they are not, something nondeterministic
  leaked into the audit and Gate G is broken.
* **LLM-DEPENDENT** — fills and drop counts by category. These will vary between runs, which is
  exactly why the model and reasoning effort are pinned below and printed in every header: the
  numbers are meaningless without knowing what produced them.

Usage (from the agent dir):

    python scripts/run_optimizer_batch.py --shop quixly-ljymkoyb.myshopify.com --dry-run
    python scripts/run_optimizer_batch.py --shop quixly-ljymkoyb.myshopify.com

``--dry-run`` runs the real pipeline (the nodes really commit) inside an outer transaction that is
rolled back, so nothing is left behind. A persisted run stamps every ``audits``/``fixes`` row with
its ``run_id``; clean it up with:

    DELETE FROM fixes  WHERE run_id = <id>;
    DELETE FROM audits WHERE run_id = <id>;
    DELETE FROM agent_runs WHERE id = <id>;
"""

import argparse
import asyncio
import collections
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.jobs.fix import propose_fixes_for_shop
from app.models import AgentRunStatus, Audit, Fix, FixType, Product, QueryPanel, Shop
from app.services.audit_rubric import SPEC_FAMILIES, SPEC_SCORED_CLASSES, structured_families
from app.services.matching import normalize_text
from app.services.optimizer_llm import OpenAIOptimizerClient
from app.services.runs import complete_agent_run, create_agent_run
from app.settings import get_settings

# PINNED HERE, NOT TAKEN FROM ENV — the extraction surface is 7 families x every coffee product, so
# a report is only interpretable next to the model that produced it. Printed in every header.
MODEL = "gpt-5-nano"
REASONING_EFFORT = "minimal"

BANDS = ("none", "low", "medium", "high", "not_audited")


def _rule(title: str) -> str:
    return f"\n{'=' * 78}\n{title}\n{'=' * 78}"


async def _load_products(session, shop_id: int) -> list[Product]:
    return list(
        (
            await session.execute(
                select(Product).where(Product.shop_id == shop_id).order_by(Product.id)
            )
        ).scalars().all()
    )


def _three_state(product: Product) -> tuple[set[str], set[str], set[str]]:
    """The rubric's deterministic split for one product: (structured, unstructured, absent)."""
    structured = structured_families(product.metafields_json)
    prose = normalize_text(f"{product.title or ''} {product.body or ''}")
    in_prose = {
        family
        for family, spec in SPEC_FAMILIES.items()
        if any(normalize_text(p) in prose for p in spec.detect)
    }
    return structured, in_prose - structured, set(SPEC_FAMILIES) - in_prose - structured


def report_deterministic(products: list[Product], audits: dict[int, Audit]) -> None:
    """Severity histogram + three-state table. Must be identical on every run."""
    print(_rule("DETERMINISTIC — identical on every run (pure catalog reads, no LLM)"))

    hist = collections.Counter(a.severity for a in audits.values())
    print("\nSeverity histogram (Gate G re-baseline):")
    for band in BANDS:
        print(f"  {band:<12} {hist.get(band, 0):>3}  {'#' * hist.get(band, 0)}")
    print(f"  {'TOTAL':<12} {sum(hist.values()):>3}")

    print("\nThree-state coverage per product (spec-scored classes only):")
    print(f"  {'id':>4}  {'class':<9} {'prose':>6} {'struct':>7}  {'sev':<11} "
          "unstructured (fillable)")
    totals = collections.Counter()
    for product in products:
        audit = audits.get(product.id)
        if audit is None:
            continue
        if audit.product_class not in SPEC_SCORED_CLASSES or audit.spec_coverage is None:
            print(f"  {product.id:>4}  {audit.product_class or '?':<9} "
                  f"{'-':>6} {'-':>7}  {audit.severity:<11} (not spec-scored)")
            continue
        structured, unstructured, absent = _three_state(product)
        totals["structured"] += len(structured)
        totals["unstructured"] += len(unstructured)
        totals["absent"] += len(absent)
        print(f"  {product.id:>4}  {audit.product_class:<9} "
              f"{audit.spec_coverage:>6.2f} {audit.structured_coverage:>7.2f}  "
              f"{audit.severity:<11} {sorted(unstructured)}")

    total_pairs = sum(totals.values())
    print(f"\n  (product, family) pairs: structured={totals['structured']}  "
          f"unstructured={totals['unstructured']}  absent={totals['absent']}  total={total_pairs}")
    print(f"  ADDRESSABLE SET (unstructured) = {totals['unstructured']} pairs — the lower bound on "
          "fills.")


def report_llm(reports: list, fixes: list[Fix]) -> None:
    """Fills and drop counts. Varies run to run — read it next to the pinned model above."""
    print(_rule(f"LLM-DEPENDENT — model={MODEL} reasoning_effort={REASONING_EFFORT}"))

    drops = collections.Counter(d.reason for r in reports for d in r.dropped)
    # ``fixes.type`` is a plain String column holding a StrEnum value, so rows read back from the
    # DB are ordinary strings — compare/format them as such (StrEnum equality still matches).
    by_type = collections.Counter(str(f.type) for f in fixes)

    print(f"\nFixes proposed: {len(fixes)}")
    for fix_type, count in sorted(by_type.items()):
        print(f"  {fix_type:<16} {count:>3}")

    print("\nDrop counts BY CATEGORY (the grounding guards firing):")
    if not drops:
        print("  (none)")
    for reason, count in sorted(drops.items()):
        print(f"  {reason:<16} {count:>3}")

    fills = [f for f in fixes if f.type in (FixType.metafield, FixType.description)]
    print(f"\nFILLS ({len(fills)}) — each with its source citation:")
    for fix in fills:
        print(f"\n  product={fix.product_id}  type={fix.type}  target={fix.target}")
        print(f"    diff   : {fix.diff}")
        print(f"    after  : {fix.after_json}")
        print(f"    source : {fix.source_json}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shop", required=True, help="shop_domain, e.g. foo.myshopify.com")
    parser.add_argument("--dry-run", action="store_true", help="persist nothing (rolled back)")
    args = parser.parse_args()

    # Pin the model for this process before any client is constructed.
    os.environ["OPENAI_EXTRACTOR_MODEL"] = MODEL
    os.environ["EXTRACTOR_REASONING_EFFORT"] = REASONING_EFFORT
    get_settings.cache_clear()
    settings = get_settings()

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    connection = await engine.connect()
    outer = await connection.begin()
    # ``create_savepoint`` lets the nodes' real commit() calls run (releasing SAVEPOINTs) while the
    # outer transaction still controls whether anything survives. Same mechanism as the test `db`
    # fixture, so --dry-run exercises the real commit path rather than a special-cased one.
    session = AsyncSession(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )

    try:
        shop = (
            await session.execute(select(Shop).where(Shop.shop_domain == args.shop))
        ).scalar_one_or_none()
        if shop is None:
            raise SystemExit(f"shop {args.shop!r} not found — ingest it first")

        panel = (
            await session.execute(
                select(QueryPanel).where(QueryPanel.shop_id == shop.id).order_by(QueryPanel.id)
            )
        ).scalars().first()
        if panel is None:
            raise SystemExit(f"no query_panel for shop {args.shop!r} — run a scan first")

        run = await create_agent_run(session, shop.id, panel.id)
        await session.commit()
        run_id = run.id

        print(_rule("Gate L acceptance run"))
        print(f"  shop      : {args.shop}")
        print(f"  run_id    : {run_id}{'  (DRY RUN — rolled back)' if args.dry_run else ''}")
        print(f"  model     : {MODEL} (reasoning_effort={REASONING_EFFORT})")

        client = OpenAIOptimizerClient()
        reports = await propose_fixes_for_shop(session, shop.id, client, run_id=run_id)
        await complete_agent_run(session, run_id, AgentRunStatus.completed)
        await session.commit()

        products = await _load_products(session, shop.id)
        audits = {
            a.product_id: a
            for a in (
                await session.execute(select(Audit).where(Audit.run_id == run_id))
            ).scalars().all()
        }
        fixes = list(
            (
                await session.execute(
                    select(Fix).where(Fix.run_id == run_id).order_by(Fix.product_id, Fix.id)
                )
            ).scalars().all()
        )

        report_deterministic(products, audits)
        report_llm(reports, fixes)

        if args.dry_run:
            print("\nDRY RUN — rolling back; no audits/fixes/agent_run rows persisted.")
        else:
            print(f"\nPersisted under run_id={run_id}. Clean up with:")
            print(f"  DELETE FROM fixes  WHERE run_id = {run_id};")
            print(f"  DELETE FROM audits WHERE run_id = {run_id};")
            print(f"  DELETE FROM agent_runs WHERE id = {run_id};")
    finally:
        await session.close()
        if args.dry_run:
            await outer.rollback()
        else:
            await outer.commit()
        await connection.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
