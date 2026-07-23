"""The per-class product-audit rubric (Phase 3, step 1 — Gate G).

Pure rule checks over a product's catalog fields — **no LLM, no DB, no network** — so the same
product always yields the same gaps and severity. This is the Diagnostician's signal on a store
with zero AI-recommendation wins (dev-store ``our_rate`` = 0.0): an absolute AI-legibility rubric,
not a relative competitor diff (that evidence join is Phase 4).

**The rubric is per product class** (``coffee`` / ``equipment`` / ``other``), classified from
merchant data by ``services.catalog.classify_product`` — never inferred by a model.

* **Spec scoring** (roast level, origin, process, variety, tasting notes, altitude, brew method)
  applies to ``coffee`` only. The vocabulary is anchored to the attributes the competitor pages
  cited in run 75 actually carry. ``equipment`` has **no** grounded vocabulary — run 75's panel is
  coffee-bean buyer queries, so there are no cited equipment pages to anchor one — and ``other`` is
  unknown, so neither is spec-scored (``spec_coverage`` is ``None``, never a misleading ``0.0``).
* **GTIN** is applicable to ``equipment`` only (third-party manufactured goods carry a manufacturer
  GTIN); self-roasted coffee is GTIN-not-applicable. Presence is read from the **variant barcode**
  via ``extract_gtin`` — the single source of truth the Optimizer also grounds on.
* **Not-discoverable** products (draft/archived/unlisted) are **excluded** from the audit
  population — reported separately as "not audited", never scored and banded.
* **Metafields** are a store-level finding (computed by the caller across the population), not a
  per-product gap — so an empty catalog no longer inflates every product's severity.

Presence is a normalised substring match (``services.matching.normalize_text``) over the product's
searchable text — title + description + every metafield value.
"""

import re

from pydantic import BaseModel

from app.services.catalog import extract_gtin
from app.services.matching import normalize_text

# Product bodies are HTML; strip tags before any text test so ``<p><br></p>`` doesn't read as text.
_HTML_TAG = re.compile(r"<[^>]+>")

# --- Gap codes ------------------------------------------------------------------------------
MISSING_DESCRIPTION = "missing_description"
MISSING_GTIN = "missing_gtin"
SPEC_MISSING = "spec_missing"

# Visibility states that exclude a product from the audit population (mirrors catalog.py). A
# deliberately-not-live product is reported separately, not scored.
_NOT_DISCOVERABLE_STATES = {"draft", "archived", "unlisted"}

# Classes for which a check applies.
_SPEC_SCORED_CLASSES = {"coffee"}
_GTIN_APPLICABLE_CLASSES = {"equipment"}

# --- Spec vocabulary (anchored to run-75 cited competitor pages) ----------------------------
# ONE definition, two consumers (CLAUDE.md "one normalizer"): the rubric reads ``.detect`` (family
# mentioned at all?) exactly as before; the Optimizer reads the validation fields (``kind`` +
# ``values`` / ``pattern`` / ``labels``) to POSITIVELY validate that an extracted value is valid
# *for its target family* — not merely present in source. Three family kinds:
#   * closed  — value must be a member of the family's value vocabulary (catches "washed" as a
#               brew_method: "washed" is not a brew value).
#   * format  — value must match a numeric + elevation-unit pattern (catches altitude:="340"
#               grounded from a weight/bare number).
#   * open    — the citation snippet must carry the family's OWN label and no competing family's
#               label (for open-ended descriptors like tasting notes).
# ``detect`` tuples are byte-for-byte the old SPEC_VOCABULARY, so rubric detection is unchanged.

# A number followed by an elevation unit (masl / m / ft / meters). "340 g" (a weight) and a bare
# "340" do not match. ``search`` so a range like "1,900-2,100 masl" still validates.
_ALTITUDE_RE = re.compile(
    r"\d[\d,.\s]*(masl|m\.?a\.?s\.?l\.?|met(?:er|re)s?|feet|ft|m)\b", re.IGNORECASE
)


class SpecFamily(BaseModel):
    """One spec family's detection phrases (rubric) + positive-validation rules (Optimizer)."""

    detect: tuple[str, ...]
    kind: str  # "closed" | "format" | "open"
    values: tuple[str, ...] = ()  # closed: valid value phrases (word-boundary matched)
    labels: tuple[str, ...] = ()  # the family's own label terms (open uses these + competitors')


SPEC_FAMILIES: dict[str, SpecFamily] = {
    "roast_level": SpecFamily(
        detect=(
            "light roast", "medium roast", "dark roast", "medium dark", "espresso roast",
            "roast level", "agtron", "decaf", "light medium roast",
        ),
        kind="closed",
        values=("light", "medium", "dark", "espresso", "decaf", "blonde", "cinnamon", "french"),
        labels=("roast level", "roast", "agtron"),
    ),
    "origin": SpecFamily(
        detect=(
            "single origin", "single-origin", "ethiopia", "colombia", "kenya", "guatemala",
            "costa rica", "brazil", "sumatra", "tanzania", "el salvador", "honduras", "rwanda",
            "peru", "yirgacheffe", "huila",
        ),
        kind="closed",
        values=(
            "ethiopia", "colombia", "kenya", "guatemala", "costa rica", "brazil", "sumatra",
            "tanzania", "el salvador", "honduras", "rwanda", "peru", "yirgacheffe", "huila",
            "panama", "panamá", "nicaragua", "mexico", "burundi", "yemen", "india", "uganda",
            "bolivia", "ecuador", "jamaica", "indonesia", "java",
        ),
        labels=("origin", "single-origin", "single origin"),
    ),
    "process": SpecFamily(
        detect=(
            "washed", "process", "fermentation", "honey process", "natural process",
            "naturally processed", "anaerobic", "semi washed", "wet hulled", "black honey",
        ),
        kind="closed",
        values=(
            "washed", "natural", "honey", "anaerobic", "semi washed", "wet hulled", "pulped",
            "black honey", "red honey", "white honey", "carbonic", "dry",
        ),
        labels=("process", "fermentation"),
    ),
    "variety": SpecFamily(
        detect=(
            "varietal", "variety", "heirloom", "gesha", "geisha", "bourbon", "typica", "caturra",
            "catuai", "sl28", "sl34", "peaberry", "pacamara", "mundo novo",
        ),
        kind="closed",
        values=(
            "heirloom", "gesha", "geisha", "bourbon", "typica", "caturra", "catuai", "sl28",
            "sl34", "peaberry", "pacamara", "mundo novo", "maragogipe", "villa sarchi", "castillo",
        ),
        labels=("varietal", "variety", "cultivar"),
    ),
    "tasting_notes": SpecFamily(
        detect=(
            "tasting notes", "notes of", "flavor notes", "flavour notes", "bergamot", "jasmine",
            "chocolate", "cocoa", "caramel", "citrus", "berry", "floral", "fruity", "nutty",
            "stone fruit", "blackcurrant",
        ),
        kind="open",
        labels=("tasting notes", "notes of", "flavor notes", "flavour notes", "tasting note",
                "aroma", "notes"),
    ),
    "altitude": SpecFamily(
        detect=(
            "altitude", "masl", "m a s l", "elevation", "meters above", "metres above",
            "high grown", "high altitude", "grown at",
        ),
        kind="format",
        labels=("altitude", "elevation", "masl", "grown at"),
    ),
    "brew_method": SpecFamily(
        detect=(
            "pour over", "pour-over", "espresso", "cold brew", "french press", "drip", "aeropress",
            "moka", "filter coffee",
        ),
        kind="closed",
        values=(
            "pour over", "pour-over", "espresso", "cold brew", "french press", "drip", "aeropress",
            "moka", "filter", "immersion", "percolator", "chemex", "v60", "batch brew",
        ),
        labels=("brew method", "brewing", "brew"),
    ),
}

# Derived view for the rubric — the detection phrases, exactly as before. Do NOT hand-edit; it is
# the single SPEC_FAMILIES definition projected.
SPEC_VOCABULARY: dict[str, tuple[str, ...]] = {
    family: spec.detect for family, spec in SPEC_FAMILIES.items()
}


def _contains_term(term: str, text: str) -> bool:
    """True if ``term`` appears as a whole word/phrase (not a substring) in ``text``, normalized."""
    words = normalize_text(text).split()
    term_words = normalize_text(term).split()
    if not term_words:
        return False
    span = len(term_words)
    return any(words[i : i + span] == term_words for i in range(len(words) - span + 1))


def validate_spec_value(family: str, value: str, snippet: str) -> bool:
    """Positively validate that ``value`` is valid FOR ``family`` (not merely present in source).

    The second grounding gate, after literal presence: it catches mis-assignment — a real source
    token grounded onto the wrong attribute (e.g. the process term "washed" proposed as a
    brew_method, or a weight "340" proposed as an altitude).
    """
    spec = SPEC_FAMILIES.get(family)
    if spec is None:
        return False
    if spec.kind == "closed":
        return any(_contains_term(v, value) for v in spec.values)
    if spec.kind == "format":
        return bool(_ALTITUDE_RE.search(value or ""))
    if spec.kind == "open":
        own = any(_contains_term(label, snippet) for label in spec.labels)
        competing = any(
            _contains_term(label, snippet)
            for other, other_spec in SPEC_FAMILIES.items()
            if other != family
            for label in other_spec.labels
        )
        return own and not competing
    return False

# --- Severity weighting ---------------------------------------------------------------------
# Weighted score → band, over the DISCOVERABLE population. Weights/bands are module constants so
# tuning is a data change. missing_gtin is heaviest per-gap so a manufactured good lacking its GTIN
# lands at medium on its own; a coffee product with almost no spec attributes accumulates to high.
_WEIGHTS: dict[str, int] = {
    MISSING_GTIN: 3,
    MISSING_DESCRIPTION: 2,
    SPEC_MISSING: 1,  # per missing family
}
_LOW_MAX = 2
_MEDIUM_MAX = 5

SEVERITY_NOT_AUDITED = "not_audited"


class AuditGap(BaseModel):
    """One deficiency found by the rubric. ``attribute`` is set only for ``spec_missing`` gaps."""

    code: str
    attribute: str | None = None
    detail: str


class AuditResult(BaseModel):
    """The rubric's verdict for one product."""

    audited: bool
    product_class: str
    gaps: list[AuditGap]
    spec_coverage: float | None  # None when the class is not spec-scored or the product is excluded
    severity: str  # none | low | medium | high | not_audited
    excluded_reason: str | None = None


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
    variants: list[dict] | None,
    metafields: list[dict] | None,
    visibility_state: str | None,
    product_class: str,
) -> AuditResult:
    """Score one product against the per-class rubric. Deterministic and side-effect-free."""
    # Population gate: deliberately-not-live products are excluded, not scored.
    if (visibility_state or "").lower() in _NOT_DISCOVERABLE_STATES:
        return AuditResult(
            audited=False,
            product_class=product_class,
            gaps=[],
            spec_coverage=None,
            severity=SEVERITY_NOT_AUDITED,
            excluded_reason="not_visible",
        )

    gaps: list[AuditGap] = []

    if not _has_text(body):
        gaps.append(AuditGap(code=MISSING_DESCRIPTION, detail="No product description text."))

    if product_class in _GTIN_APPLICABLE_CLASSES and extract_gtin(variants or []) is None:
        gaps.append(
            AuditGap(code=MISSING_GTIN, detail="No variant carries a manufacturer barcode / GTIN.")
        )

    spec_coverage: float | None = None
    if product_class in _SPEC_SCORED_CLASSES:
        text = _searchable_text(title, body, metafields)
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
    return AuditResult(
        audited=True,
        product_class=product_class,
        gaps=gaps,
        spec_coverage=spec_coverage,
        severity=_severity(score),
    )


def has_structured_metafields(metafields: list[dict] | None) -> bool:
    """True if the product carries at least one structured metafield.

    Metafield coverage is a **store-level** finding (e.g. "0 of 18 discoverable products carry
    structured metafields"), rolled up by the caller across the population — it is deliberately not
    a per-product gap, so an empty catalog does not inflate every product's severity.
    """
    return bool(metafields)
