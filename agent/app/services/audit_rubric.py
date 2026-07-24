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

**Three-state spec model (step 2b).** Every (product, spec family) is one of:

* ``structured``   — carried by a metafield keyed to the family. Machine-readable; nothing to do.
* ``unstructured`` — stated in the PROSE (title/body) but not in a metafield. Optimizer-fixable.
* ``absent``       — nowhere. A merchant to-do.

So there are **two coverage numbers, and neither replaces the other**: ``structured_coverage`` is
the headline AI-legibility score (what engines actually read), ``spec_coverage`` is PROSE coverage,
and the **difference between them is the addressable set** ("prose 0.86 / structured 0.00" is a
sharper finding than either alone).

Prose presence is a normalised substring match (``services.matching.normalize_text``) over title +
description only — **metafield values are the structured channel and are deliberately excluded**
from the prose text, or a structured family would also inflate prose coverage.

**The audit's unstructured/absent split is a deterministic DETECT-BASED PROXY.** The rubric stays
LLM-free, so it cannot ask an extractor what is really in the prose; it asks ``detect``. The
Optimizer decides the same split by *extraction*, and the two **may diverge** — ``detect`` misses
``"Roast: Medium-Light"``, so the audit calls roast ``absent`` while the Optimizer extracts and
fills it. That divergence is expected: the audit is an estimate, the Optimizer's fills are ground
truth. Crucially the Optimizer does **not** read ``detect`` or the audit's gaps to choose targets
(see ``graph.optimizer``), so refining ``detect`` moves these numbers **without** adding or removing
a single fill.
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

# Classes for which a check applies. SPEC_SCORED_CLASSES is PUBLIC: the Optimizer reads it to gate
# spec targeting by class. Without that gate an equipment product would be asked for seven coffee
# families, because the Optimizer no longer derives its targets from the audit's gaps.
SPEC_SCORED_CLASSES = frozenset({"coffee"})
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
    # Metafield KEYS that mean "this family is already structured" (step 2b), beyond the family
    # name itself. Deliberately a separate, curated list rather than a reuse of ``labels``: labels
    # exist for snippet matching and include generic terms ("notes", "aroma", "masl") that would
    # false-positive as keys. A false ``structured`` is the dangerous direction — it both inflates
    # the headline score and silently drops the family from the Optimizer's targets — so this list
    # stays conservative.
    metafield_aliases: tuple[str, ...] = ()


SPEC_FAMILIES: dict[str, SpecFamily] = {
    "roast_level": SpecFamily(
        detect=(
            "light roast", "medium roast", "dark roast", "medium dark", "espresso roast",
            "roast level", "agtron", "decaf", "light medium roast",
        ),
        kind="closed",
        values=("light", "medium", "dark", "espresso", "decaf", "blonde", "cinnamon", "french"),
        labels=("roast level", "roast", "agtron"),
        metafield_aliases=("roast", "roast level", "roastlevel"),
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
        metafield_aliases=("origin", "single origin", "country of origin", "country"),
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
        metafield_aliases=("process", "processing", "processing method", "process method"),
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
        metafield_aliases=("variety", "varietal", "varietals", "cultivar"),
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
        metafield_aliases=("tasting notes", "tasting note", "flavor notes", "flavour notes",
                           "flavor profile", "flavour profile"),
    ),
    "altitude": SpecFamily(
        detect=(
            "altitude", "masl", "m a s l", "elevation", "meters above", "metres above",
            "high grown", "high altitude", "grown at",
        ),
        kind="format",
        labels=("altitude", "elevation", "masl", "grown at"),
        metafield_aliases=("altitude", "elevation", "growing altitude", "growing elevation"),
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
        metafield_aliases=("brew method", "brewing method", "brew", "recommended brew method"),
    ),
}

# Derived view for the rubric — the detection phrases, exactly as before. Do NOT hand-edit; it is
# the single SPEC_FAMILIES definition projected.
SPEC_VOCABULARY: dict[str, tuple[str, ...]] = {
    family: spec.detect for family, spec in SPEC_FAMILIES.items()
}


def _normalize_key(text: str) -> str:
    """Normalize a metafield key for comparison. Underscores are word chars to ``normalize_text``
    (so ``roast_level`` would not equal ``roast level``); flatten them to spaces first."""
    return normalize_text(text.replace("_", " "))


# Metafield key -> family, built once from the single SPEC_FAMILIES definition. Namespace-agnostic:
# merchants pick their own namespace, and the family is named by the KEY (the Optimizer writes
# ``custom.<family>``, see graph.optimizer).
_STRUCTURED_KEYS: dict[str, str] = {
    _normalize_key(alias): family
    for family, spec in SPEC_FAMILIES.items()
    for alias in (family, *spec.metafield_aliases)
}


def structured_families(metafields: list[dict] | None) -> set[str]:
    """Spec families already carried by a structured metafield — the ``structured`` state.

    **One definition, both consumers** (CLAUDE.md "one normalizer"): the rubric reads it for
    ``structured_coverage``, and the Optimizer subtracts it from the spec families to get its
    targets. Because targeting is derived from THIS and never from ``detect``/``audit.gaps``,
    refining detection can never add or remove a fill opportunity.

    A key with an empty/absent value does **not** count as structured — an empty metafield is not
    machine-readable, and treating it as structured would both inflate the headline score and drop
    a real gap from the Optimizer's targets.
    """
    found: set[str] = set()
    for field in metafields or []:
        if not isinstance(field, dict):
            continue
        value = field.get("value")
        if not (isinstance(value, str) and value.strip()):
            continue
        family = _STRUCTURED_KEYS.get(_normalize_key(str(field.get("key") or "")))
        if family is not None:
            found.add(family)
    return found


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
# tuning is a data change.
#
# Step 2b re-baseline (Gate G re-approved): a spec family's weight is now STATE-based — an
# ``absent`` family (the merchant must supply it) weighs more than an ``unstructured`` one (the
# Optimizer can structure it automatically), and a ``structured`` family emits no gap at all.
# The non-spec weights are scaled by the same factor as ``absent`` so they keep their step-1
# meaning: missing_gtin is still heavy enough that a manufactured good lacking its GTIN lands at
# medium ON ITS OWN. (Leaving them at 3/2 against the doubled spec weights would silently demote
# every GTIN-less product to low.)
STATE_UNSTRUCTURED = "unstructured"
STATE_ABSENT = "absent"

_MISSING_GTIN_WEIGHT = 6
_MISSING_DESCRIPTION_WEIGHT = 4
_SPEC_STATE_WEIGHTS: dict[str, int] = {STATE_ABSENT: 2, STATE_UNSTRUCTURED: 1}
_LOW_MAX = 4
_MEDIUM_MAX = 9

SEVERITY_NOT_AUDITED = "not_audited"


class AuditGap(BaseModel):
    """One deficiency found by the rubric.

    ``attribute`` and ``state`` are set only for ``spec_missing`` gaps. ``state`` is the
    DETECT-BASED, deterministic three-state proxy: ``unstructured`` = detected in the prose but not
    in a metafield, ``absent`` = detected nowhere. It is an *approximation* — the only one the
    rubric can compute without an LLM — and the Optimizer's extraction-based split may legitimately
    differ (see the module docstring).
    """

    code: str
    attribute: str | None = None
    state: str | None = None
    detail: str


class AuditResult(BaseModel):
    """The rubric's verdict for one product."""

    audited: bool
    product_class: str
    gaps: list[AuditGap]
    # PROSE coverage: families stated in title/body. Informational; the addressable set is the
    # difference between this and structured_coverage.
    spec_coverage: float | None  # None when the class is not spec-scored or the product is excluded
    # Headline AI-legibility: families carried by a structured metafield — what engines read.
    structured_coverage: float | None
    severity: str  # none | low | medium | high | not_audited
    excluded_reason: str | None = None


def _gap_weight(gap: AuditGap) -> int:
    if gap.code == MISSING_GTIN:
        return _MISSING_GTIN_WEIGHT
    if gap.code == MISSING_DESCRIPTION:
        return _MISSING_DESCRIPTION_WEIGHT
    return _SPEC_STATE_WEIGHTS[gap.state or STATE_ABSENT]


def _prose_text(title: str | None, body: str | None) -> str:
    """Title + description only — the PROSE channel.

    Metafield values are the STRUCTURED channel and are deliberately excluded: counting them here
    too would make a structured family raise prose coverage as well, and the difference between the
    two numbers (the addressable set) would collapse.
    """
    return normalize_text(" ".join([title or "", _HTML_TAG.sub(" ", body or "")]))


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
            structured_coverage=None,
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
    structured_coverage: float | None = None
    if product_class in SPEC_SCORED_CLASSES:
        prose = _prose_text(title, body)
        structured = structured_families(metafields)
        in_prose = {
            family
            for family, phrases in SPEC_VOCABULARY.items()
            if any(normalize_text(phrase) in prose for phrase in phrases)
        }
        total = len(SPEC_VOCABULARY)
        spec_coverage = len(in_prose) / total
        structured_coverage = len(structured) / total

        # Three-state (detect-based proxy): a structured family is machine-readable and emits no
        # gap; everything else is a gap tagged with WHY it is one, which drives both the weight and
        # the merchant-facing detail.
        for family in SPEC_VOCABULARY:
            if family in structured:
                continue
            label = family.replace("_", " ")
            if family in in_prose:
                gaps.append(
                    AuditGap(
                        code=SPEC_MISSING,
                        attribute=family,
                        state=STATE_UNSTRUCTURED,
                        detail=(
                            f"{label} is stated in the description but not in a metafield, "
                            "so AI engines cannot read it reliably."
                        ),
                    )
                )
            else:
                gaps.append(
                    AuditGap(
                        code=SPEC_MISSING,
                        attribute=family,
                        state=STATE_ABSENT,
                        detail=f"No {label} stated anywhere in the product's data.",
                    )
                )

    score = sum(_gap_weight(gap) for gap in gaps)
    return AuditResult(
        audited=True,
        product_class=product_class,
        gaps=gaps,
        spec_coverage=spec_coverage,
        structured_coverage=structured_coverage,
        severity=_severity(score),
    )


def has_structured_metafields(metafields: list[dict] | None) -> bool:
    """True if the product carries at least one structured metafield.

    Metafield coverage is a **store-level** finding (e.g. "0 of 18 discoverable products carry
    structured metafields"), rolled up by the caller across the population — it is deliberately not
    a per-product gap, so an empty catalog does not inflate every product's severity.
    """
    return bool(metafields)
