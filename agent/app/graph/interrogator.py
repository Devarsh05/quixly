"""Interrogator — generates the deterministic buyer-intent query panel for a vertical.

PRD §6: the Interrogator "generates/loads the buyer-intent query panel per category". This
is the first graph node; every downstream node (EngineRunner → Extractor → ShareOfModel →
Verifier) reads off the panel it produces. Generation is therefore **deterministic**: the same
inputs always yield the same queries in the same order and the same content fingerprint, so runs
are comparable period-over-period and a template change shows up as a diff.

Scope of this module is pure generation — no DB writes, no LLM/engine/network calls. Persistence
to ``query_panels`` lands with EngineRunner. ``build_query_panel`` returns a typed object held in
graph state. (LangGraph is not yet a dependency; this ships as a pure builder, wired into a
``StateGraph`` in a later phase.)

The attribute vocabularies live as named module constants so a later per-vertical swap is a data
change, not a rewrite.
"""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# --- Coffee vocabulary (swap per vertical later) ---------------------------------------------

ROASTS: tuple[str, ...] = ("light", "medium", "dark", "espresso", "decaf")
ORIGINS: tuple[str, ...] = (
    "Ethiopian",
    "Colombian",
    "Kenyan",
    "Guatemalan",
    "Costa Rican",
    "Brazilian",
    "Sumatran",
)
METHODS: tuple[str, ...] = ("pour over", "espresso", "cold brew", "French press", "drip")
PRICE_BANDS: tuple[int, ...] = (15, 20, 25)

# usecase lines are NOT template-filled: "best coffee beans for whole bean" / "for subscription"
# are not real buyer queries and would pollute the panel. Each usecase token maps to a
# hand-written full query line. token -> query text.
USECASE_QUERIES: dict[str, str] = {
    "beginners": "best coffee beans for beginners",
    "whole bean": "best whole bean coffee",
    "subscription": "best coffee subscription",
    "gift": "best coffee beans for a gift",
}

# One template per filled-intent family, addressed by a stable ``template_id`` so a wording
# change surfaces in the snapshot test. usecase has no template (see USECASE_QUERIES).
_TEMPLATES: dict[str, str] = {
    "roast": "best {roast} coffee beans",
    "origin": "best {origin} coffee beans",
    "method": "best coffee beans for {method}",
    "price": "best coffee beans under ${price}",
}

# Panel size is a cost lever (each query fans out across every engine downstream); don't let it
# grow unbounded. A per-call knob, not deployment config, so the node stays pure and testable.
DEFAULT_MAX_QUERIES = 30


class IntentCategory(StrEnum):
    """Buyer-intent family a query belongs to. Each has its own template family."""

    ROAST = "roast"
    ORIGIN = "origin"
    METHOD = "method"
    PRICE = "price"
    USECASE = "usecase"


class PanelQuery(BaseModel):
    """One buyer-intent query and the template/attribute it was generated from."""

    model_config = ConfigDict(frozen=True)

    text: str
    intent_category: IntentCategory
    template_id: str
    attribute: str  # the value that filled the template, stringified ("20", "Ethiopian", ...)


class QueryPanel(BaseModel):
    """A deterministic set of buyer-intent queries for one vertical."""

    model_config = ConfigDict(frozen=True)

    category: str  # the VERTICAL (e.g. "coffee"), not the per-query intent category
    queries: list[PanelQuery]
    fingerprint: str  # sha256 over ordered content; excludes ``generated_at``
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _generate_queries(category: str) -> list[PanelQuery]:
    """Emit every query for the vertical in fixed family order, before dedup/truncation.

    Only ``coffee`` is supported today; other verticals get their own vocabularies later.
    """
    if category != "coffee":
        raise ValueError(f"No query vocabulary for vertical {category!r}")

    queries: list[PanelQuery] = []

    for roast in ROASTS:
        queries.append(
            PanelQuery(
                text=_TEMPLATES["roast"].format(roast=roast),
                intent_category=IntentCategory.ROAST,
                template_id="roast",
                attribute=roast,
            )
        )
    for origin in ORIGINS:
        queries.append(
            PanelQuery(
                text=_TEMPLATES["origin"].format(origin=origin),
                intent_category=IntentCategory.ORIGIN,
                template_id="origin",
                attribute=origin,
            )
        )
    for method in METHODS:
        queries.append(
            PanelQuery(
                text=_TEMPLATES["method"].format(method=method),
                intent_category=IntentCategory.METHOD,
                template_id="method",
                attribute=method,
            )
        )
    for band in PRICE_BANDS:
        queries.append(
            PanelQuery(
                text=_TEMPLATES["price"].format(price=band),
                intent_category=IntentCategory.PRICE,
                template_id="price",
                attribute=str(band),
            )
        )
    for token, text in USECASE_QUERIES.items():
        queries.append(
            PanelQuery(
                text=text,
                intent_category=IntentCategory.USECASE,
                template_id="usecase",
                attribute=token,
            )
        )

    return queries


def _dedupe(queries: list[PanelQuery]) -> list[PanelQuery]:
    """Drop any query whose text already appeared, preserving first-occurrence order."""
    seen: set[str] = set()
    deduped: list[PanelQuery] = []
    for query in queries:
        if query.text not in seen:
            seen.add(query.text)
            deduped.append(query)
    return deduped


def _fingerprint(category: str, queries: list[PanelQuery]) -> str:
    """Stable sha256 over category + ordered (intent_category, text). Excludes generated_at."""
    payload = [category, [[q.intent_category.value, q.text] for q in queries]]
    canonical = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_query_panel(
    category: str = "coffee",
    max_queries: int = DEFAULT_MAX_QUERIES,
    now: datetime | None = None,
) -> QueryPanel:
    """Build the deterministic buyer-intent panel for ``category``.

    Same inputs → identical ``queries`` (order included) and ``fingerprint``. ``now`` is an
    injectable clock for ``generated_at``; it never affects the fingerprint. Queries are deduped
    by text and truncated to the first ``max_queries`` in generation order if the set is larger.
    """
    queries = _dedupe(_generate_queries(category))[:max_queries]
    return QueryPanel(
        category=category,
        queries=queries,
        fingerprint=_fingerprint(category, queries),
        generated_at=now or datetime.now(UTC),
    )
