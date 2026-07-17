"""Extractor — reads the answers EngineRunner wrote and fills the brand columns (PRD §6, §8).

EngineRunner (step 2) leaves ``cited_brands_json`` / ``our_mentions_json`` NULL on every
``engine_runs`` row. This node reads those rows for a panel and, per answer:

1. Calls the injected structured-output ``ExtractorClient`` to pull the brands the engine
   explicitly named, ordered by recommendation prominence (rank 1 = most-recommended).
2. Runs a **grounding check** (load-bearing anti-fabrication guard — CLAUDE.md risk zone): every
   emitted brand must appear literally in the answer text via a normalized substring match. Brands
   that don't verify are dropped and recorded as ``rejected_hallucinations`` — never persisted.
3. Runs a **self-mention** match against the store's own brand aliases via the reusable
   ``normalize_and_match`` helper (step 4 reuses it verbatim for the competitor set).
4. UPDATEs the two columns in place. A row whose extraction fails keeps BOTH columns NULL (so a
   re-run retries it) and is recorded in the report — never an error envelope in these columns.

No bare/unstructured LLM calls: every extraction is a typed ``ExtractedBrands``. The session and
client are injected, so tests drive it against the transaction-scoped ``db`` fixture.
"""

import asyncio
import logging
import re
from collections.abc import Iterable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EngineRun
from app.services.extractor_llm import ExtractedBrand, ExtractedBrands, ExtractorClient
from app.settings import get_settings

logger = logging.getLogger(__name__)

# The store's own brand identity, for self-mention matching. Placeholder module constant until the
# step-4 org-memory work persists brand identity + the competitor set. `shops` has no name column.
STORE_ALIASES: tuple[str, ...] = ("Northwind Coffee", "Northwind", "Northwind Coffee Roasters")

# Trailing tokens stripped before brand-alias matching, so "Northwind Coffee Roasters",
# "Northwind Coffee", and "Northwind" all collapse to the same key.
_STRIP_SUFFIXES = {"coffee", "roasters", "roaster", "co", "company", "inc"}

_PUNCT = re.compile(r"[^\w\s]")


class BrandMatch(BaseModel):
    """An extracted name matched to a store/competitor alias, keyed by its input position."""

    index: int  # 0-based position in the input name list
    name: str  # the extracted brand name as given
    matched_alias: str  # the canonical alias it matched


class RejectedHallucination(BaseModel):
    """A brand the model emitted that is absent from the answer text — dropped, never persisted."""

    engine_run_id: int
    brand: str


class ExtractionFailure(BaseModel):
    """A row whose extraction failed; its columns are left NULL so a re-run retries it."""

    engine_run_id: int
    error: str


class ExtractorReport(BaseModel):
    """Extractor's typed return, held in graph state."""

    panel_id: int
    processed: int
    mentioned_count: int
    rejected_hallucinations: list[RejectedHallucination]
    failures: list[ExtractionFailure]


def _normalize_text(text: str) -> str:
    """Casefold, replace punctuation with spaces, and collapse whitespace."""
    return " ".join(_PUNCT.sub(" ", text).casefold().split())


def _normalize_brand(name: str) -> str:
    """Normalize a brand name and strip trailing generic suffixes (coffee/roasters/co/...)."""
    tokens = _normalize_text(name).split()
    while tokens and tokens[-1] in _STRIP_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _is_grounded(brand: str, answer_text: str) -> bool:
    """True iff ``brand`` appears in ``answer_text`` under normalized substring matching.

    The anti-fabrication guard: literal presence only, no suffix stripping (that is for alias
    matching). An empty normalized brand never matches.
    """
    needle = _normalize_text(brand)
    return bool(needle) and needle in _normalize_text(answer_text)


def normalize_and_match(extracted_names: list[str], alias_set: Iterable[str]) -> list[BrandMatch]:
    """Match extracted brand names against an alias set by normalized, suffix-stripped equality.

    Returns one ``BrandMatch`` per matching name, carrying its input ``index`` so the caller can map
    it back to a rank/product. Designed to be reused verbatim in step 4 by passing the competitor
    alias set instead of ``STORE_ALIASES``.
    """
    # Normalized alias -> first original alias string (aliases that collapse to the same key share
    # one canonical label).
    canonical: dict[str, str] = {}
    for alias in alias_set:
        norm = _normalize_brand(alias)
        if norm and norm not in canonical:
            canonical[norm] = alias

    matches: list[BrandMatch] = []
    for index, name in enumerate(extracted_names):
        norm = _normalize_brand(name)
        if norm and norm in canonical:
            matches.append(BrandMatch(index=index, name=name, matched_alias=canonical[norm]))
    return matches


def _answer_text(response_raw: dict) -> str | None:
    """Pull ``choices[0].message.content`` from a stored engine payload, or None for error rows."""
    choices = response_raw.get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}
    content = message.get("content")
    return content if isinstance(content, str) and content else None


def _ground(extracted: ExtractedBrands, answer_text: str) -> tuple[list[ExtractedBrand], list[str]]:
    """Split extracted brands into grounded (re-ranked 1..N in order) and rejected names."""
    kept: list[ExtractedBrand] = []
    rejected: list[str] = []
    for brand in extracted.brands:
        if _is_grounded(brand.brand, answer_text):
            kept.append(brand)
        else:
            rejected.append(brand.brand)

    grounded = [
        ExtractedBrand(rank=i + 1, brand=b.brand, product=b.product, verbatim=b.verbatim)
        for i, b in enumerate(kept)
    ]
    return grounded, rejected


async def run_extractor(
    session: AsyncSession,
    panel_id: int,
    client: ExtractorClient,
    store_aliases: Iterable[str] = STORE_ALIASES,
    *,
    max_concurrency: int | None = None,
    force: bool = False,
) -> ExtractorReport:
    """Extract, ground, and self-match brands for a panel's engine_runs; UPDATE the two columns."""
    concurrency = max_concurrency or get_settings().extractor_max_concurrency

    statement = select(EngineRun).where(EngineRun.panel_id == panel_id)
    if not force:
        statement = statement.where(EngineRun.cited_brands_json.is_(None))
    rows = (await session.execute(statement)).scalars().all()

    # Only rows with a usable answer are eligible; error rows (no choices/content) are skipped.
    eligible = [(row, text) for row in rows if (text := _answer_text(row.response_raw))]

    semaphore = asyncio.Semaphore(concurrency)

    async def _extract(
        row: EngineRun, answer_text: str
    ) -> tuple[EngineRun, str, ExtractedBrands | None, str | None]:
        async with semaphore:
            try:
                return row, answer_text, await client.extract(answer_text), None
            except Exception as exc:  # noqa: BLE001 — one row's failure must not sink the batch
                logger.warning("Extraction failed for engine_run %s: %s", row.id, exc)
                return row, answer_text, None, str(exc)

    results = await asyncio.gather(*(_extract(row, text) for row, text in eligible))

    processed = 0
    mentioned_count = 0
    rejected_hallucinations: list[RejectedHallucination] = []
    failures: list[ExtractionFailure] = []

    for row, answer_text, extracted, error in results:
        if extracted is None:
            # Leave both columns NULL so a re-run retries this row; record the failure.
            failures.append(ExtractionFailure(engine_run_id=row.id, error=error or "unknown error"))
            continue

        grounded, rejected = _ground(extracted, answer_text)
        rejected_hallucinations.extend(
            RejectedHallucination(engine_run_id=row.id, brand=name) for name in rejected
        )

        matches = normalize_and_match([b.brand for b in grounded], store_aliases)
        mentioned = bool(matches)
        our_mentions = {
            "mentioned": mentioned,
            "ranks": [grounded[m.index].rank for m in matches],
            "matched_alias": matches[0].matched_alias if matches else None,
            "products": [grounded[m.index].product for m in matches if grounded[m.index].product],
        }

        row.cited_brands_json = [
            {"rank": b.rank, "brand": b.brand, "product": b.product} for b in grounded
        ]
        row.our_mentions_json = our_mentions

        processed += 1
        if mentioned:
            mentioned_count += 1

    await session.commit()
    return ExtractorReport(
        panel_id=panel_id,
        processed=processed,
        mentioned_count=mentioned_count,
        rejected_hallucinations=rejected_hallucinations,
        failures=failures,
    )
