"""The deterministic product-audit rubric (Phase 3, step 1 — Gate G).

Pure rule checks over a product's catalog fields — **no LLM, no DB, no network**, so the same
product always yields the same gaps and severity. This is the Diagnostician's signal on a store
with zero AI-recommendation wins (dev-store ``our_rate`` = 0.0): it judges each product against an
absolute AI-legibility rubric, not a relative competitor diff (that evidence join is Phase 4).

**The spec vocabulary is grounded, not invented.** Every attribute family below is one the
competitor / cited pages in run 75 organise their recommendations around — roast level, origin,
process, variety, tasting notes, altitude, and brew-method suitability (observed in run 75's
``engine_runs.cited_sources_json`` titles + snippets). A product an AI shopping engine can reason
about carries these; the audit flags the ones absent from the product's own text.

Presence is a normalised substring match (via ``services.matching.normalize_text``) over the
product's searchable text — title + description + every metafield value — so an attribute stated
in a structured metafield counts just as much as one written in prose.
"""

import re

from pydantic import BaseModel

from app.services.matching import normalize_text

# Product bodies are HTML (``descriptionHtml`` from ingest, ``body_html`` from the webhook), so
# tags are stripped before any text test — otherwise ``<p><br></p>`` normalises to the words
# "p br" and reads as real description text.
_HTML_TAG = re.compile(r"<[^>]+>")

# --- Gap codes ------------------------------------------------------------------------------
MISSING_DESCRIPTION = "missing_description"
MISSING_GTIN = "missing_gtin"
MISSING_METAFIELDS = "missing_metafields"
NOT_DISCOVERABLE = "not_discoverable"
SPEC_MISSING = "spec_missing"

# Visibility states that mean the product is not surfaced in search / collections /
# recommendations (mirrors the vocabulary normalised in services/catalog.py). ``active`` and a
# NULL state are treated as discoverable.
_NOT_DISCOVERABLE_STATES = {"draft", "archived", "unlisted"}

# --- Spec vocabulary (anchored to run-75 cited competitor pages) ----------------------------
# family -> phrases whose normalised form, if present anywhere in the searchable text, marks the
# family satisfied. Phrases are matched normalised (casefolded, punctuation->space), so
# "Process: Washed" and "washed process" both hit "washed".
SPEC_VOCABULARY: dict[str, tuple[str, ...]] = {
    "roast_level": (
        "light roast", "medium roast", "dark roast", "medium dark", "espresso roast",
        "roast level", "agtron", "decaf", "light medium roast",
    ),
    "origin": (
        "single origin", "single-origin", "ethiopia", "colombia", "kenya", "guatemala",
        "costa rica", "brazil", "sumatra", "tanzania", "el salvador", "honduras", "rwanda",
        "peru", "yirgacheffe", "huila",
    ),
    "process": (
        "washed", "process", "fermentation", "honey process", "natural process",
        "naturally processed", "anaerobic", "semi washed", "wet hulled", "black honey",
    ),
    "variety": (
        "varietal", "variety", "heirloom", "gesha", "geisha", "bourbon", "typica", "caturra",
        "catuai", "sl28", "sl34", "peaberry", "pacamara", "mundo novo",
    ),
    "tasting_notes": (
        "tasting notes", "notes of", "flavor notes", "flavour notes", "bergamot", "jasmine",
        "chocolate", "cocoa", "caramel", "citrus", "berry", "floral", "fruity", "nutty",
        "stone fruit", "blackcurrant",
    ),
    "altitude": (
        "altitude", "masl", "m a s l", "elevation", "meters above", "metres above",
        "high grown", "high altitude", "grown at",
    ),
    "brew_method": (
        "pour over", "pour-over", "espresso", "cold brew", "french press", "drip", "aeropress",
        "moka", "filter coffee",
    ),
}

# --- Severity weighting ---------------------------------------------------------------------
# Weighted score → band. Weights and bands are module constants so tuning is a data change, not a
# rewrite. not_discoverable is the heaviest single gap: an invisible product cannot be
# recommended no matter how good its data is.
_WEIGHTS: dict[str, int] = {
    NOT_DISCOVERABLE: 3,
    MISSING_DESCRIPTION: 2,
    MISSING_GTIN: 1,
    MISSING_METAFIELDS: 1,
    SPEC_MISSING: 1,  # per missing family
}
# score → severity: 0 none | 1-2 low | 3-5 medium | 6+ high.
_LOW_MAX = 2
_MEDIUM_MAX = 5


class AuditGap(BaseModel):
    """One deficiency found by the rubric. ``attribute`` is set only for ``spec_missing`` gaps."""

    code: str
    attribute: str | None = None
    detail: str


class AuditResult(BaseModel):
    """The rubric's verdict for one product: gaps, a spec-coverage ratio, and a severity band."""

    gaps: list[AuditGap]
    spec_coverage: float  # families present / total families, in [0, 1]
    severity: str  # none | low | medium | high


def _metafield_values(metafields: list[dict] | None) -> list[str]:
    values: list[str] = []
    for field in metafields or []:
        value = field.get("value")
        if isinstance(value, str) and value:
            values.append(value)
    return values


def _searchable_text(title: str | None, body: str | None, metafields: list[dict] | None) -> str:
    body_text = _HTML_TAG.sub(" ", body or "")
    parts = [title or "", body_text, *_metafield_values(metafields)]
    return normalize_text(" ".join(parts))


def _has_text(body: str | None) -> bool:
    """True if ``body`` carries any human text once HTML markup is stripped."""
    if not body:
        return False
    return bool(normalize_text(_HTML_TAG.sub(" ", body)).strip())


def _severity(score: int) -> str:
    if score == 0:
        return "none"
    if score <= _LOW_MAX:
        return "low"
    if score <= _MEDIUM_MAX:
        return "medium"
    return "high"


def evaluate_product(
    *,
    title: str | None,
    body: str | None,
    gtin: str | None,
    metafields: list[dict] | None,
    visibility_state: str | None,
) -> AuditResult:
    """Score one product against the AI-legibility rubric. Deterministic and side-effect-free."""
    text = _searchable_text(title, body, metafields)
    gaps: list[AuditGap] = []

    if not _has_text(body):
        gaps.append(AuditGap(code=MISSING_DESCRIPTION, detail="No product description text."))

    if not gtin:
        gaps.append(
            AuditGap(code=MISSING_GTIN, detail="No variant carries a barcode / GTIN.")
        )

    if not metafields:
        gaps.append(
            AuditGap(
                code=MISSING_METAFIELDS,
                detail="No structured metafields for machine-readable attributes.",
            )
        )

    if (visibility_state or "").lower() in _NOT_DISCOVERABLE_STATES:
        gaps.append(
            AuditGap(
                code=NOT_DISCOVERABLE,
                detail=f"visibility_state={visibility_state!r} is not surfaced to shoppers.",
            )
        )

    present = 0
    for family, phrases in SPEC_VOCABULARY.items():
        if any(normalize_text(phrase) in text for phrase in phrases):
            present += 1
        else:
            gaps.append(
                AuditGap(
                    code=SPEC_MISSING,
                    attribute=family,
                    detail=f"No {family.replace('_', ' ')} stated in the product's text.",
                )
            )

    spec_coverage = present / len(SPEC_VOCABULARY)
    score = sum(_WEIGHTS[gap.code] for gap in gaps)
    return AuditResult(gaps=gaps, spec_coverage=spec_coverage, severity=_severity(score))
