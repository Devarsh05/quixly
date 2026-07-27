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

**Three-state spec model — UNCHANGED since step 2b.** Every (product, spec family) is one of:

* ``structured``   — carried by its **taxonomy attribute** metafield (the ``shopify`` namespace).
  Machine-readable; nothing to do.
* ``unstructured`` — stated in the PROSE (title/body) but not structured. Optimizer-fixable.
* ``absent``       — nowhere. A merchant to-do.

Step 2d did **not** move this classification. What changed is only **which write makes a family
``structured``** — the ``shopify`` taxonomy attribute, **not** ``custom.*`` (the 2c spike proved
``custom.*`` is not the AI-legible channel), and only the 4 **taxonomy-home** families
(``services.taxonomy_map.TAXONOMY_HOME_FAMILIES``) can ever reach ``structured``; the other 5 top
out at ``unstructured`` (their legible home is the description prose).

**Two channel-specific coverage numbers, NOT blended** (blending would re-bury the spike's central
finding that legibility is channel-specific):

* ``taxonomy_coverage`` — families written to their taxonomy attribute / **applicable taxonomy-home
  families** (3, or 4 for a decaf product). The headline score (the filter channel Shopify feeds
  agentic surfaces).
* ``spec_coverage`` — families stated in the PROSE / **applicable spec families** (8, or 9 for a
  decaf product). ``decaffeination_method`` is applicable only to decaf products (like GTIN for
  equipment), so both denominators are per-product.

The **difference is the addressable set** ("prose 0.86 / taxonomy 0.00" is a sharper finding than
either alone). Prose presence is a normalised substring match (``services.matching.normalize_text``)
over title + description only — **metafield values are the structured channel and are deliberately
excluded** from the prose text, or a structured family would also inflate prose coverage.

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
from app.services.taxonomy_map import TAXONOMY_ATTRIBUTES, TAXONOMY_HOME_FAMILIES

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

# ONE definition of an elevation unit, two patterns over it (they answer different questions):
_ELEVATION_UNIT = r"(?:masl|m\.?a\.?s\.?l\.?|met(?:er|re)s?|feet|ft|m)"

# VALIDATION — "does this value contain an elevation?" A number followed by an elevation unit
# (masl / m / ft / meters). "340 g" (a weight) and a bare "340" do not match. ``search`` so a range
# like "1,900-2,100 masl" still validates.
_ALTITUDE_RE = re.compile(rf"\d[\d,.\s]*{_ELEVATION_UNIT}\b", re.IGNORECASE)

# RECOVERY — "what is the WHOLE elevation span?" Same units, but it also spans a range separator,
# which the validation pattern deliberately does not need to. Without this, "1,900-2,100 masl"
# recovers as "2,100 masl" — literally present and validating, yet it reports the top of a range as
# *the* altitude. That narrowing is a misrepresentation of the merchant's own copy, so the span
# pattern must capture the range whole or the value is not safe to publish.
_ALTITUDE_SPAN_RE = re.compile(
    rf"\d[\d,.\s]*(?:[-–—]\s*\d[\d,.\s]*)*{_ELEVATION_UNIT}\b", re.IGNORECASE
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
    # --- step 2d: the two "free native wins" the 2c spike found (taxonomy-home) ------------------
    "coffee_product_form": SpecFamily(
        detect=("whole bean", "whole beans", "whole-bean", "ground coffee", "pre-ground"),
        kind="closed",
        # Bare "ground" is a valid VALUE (whole-word matched by validate_spec_value) but too generic
        # to DETECT on (it would false-match "background"), so it is out of ``detect``.
        values=("whole bean", "whole beans", "whole-bean", "ground"),
        labels=("product form", "form"),
        metafield_aliases=("coffee product form", "product form", "form"),
    ),
    "decaffeination_method": SpecFamily(
        # CONDITIONAL family (see ``CONDITIONAL_FAMILIES``): applicable only to decaf products, so a
        # regular coffee is never dinged for lacking a decaffeination method.
        detect=(
            "swiss water", "mountain water", "carbon dioxide", "co2 process", "methylene chloride",
            "ethyl acetate", "decaffeination", "sugarcane process",
        ),
        kind="closed",
        values=(
            "swiss water", "mountain water", "carbon dioxide", "methylene chloride",
            "ethyl acetate", "co2",
        ),
        labels=("decaffeination method", "decaf method", "decaffeination"),
        metafield_aliases=("decaffeination method", "decaf method"),
    ),
}

# A family that applies only to a subset of products (mirrors GTIN → equipment). Value = a predicate
# over the product's prose text. ``decaffeination_method`` applies only to decaf products, so both
# coverage denominators and the gap list drop it for a regular coffee — it is not an "absent" gap.
def _is_decaf(prose: str) -> bool:
    return "decaf" in prose


CONDITIONAL_FAMILIES: dict[str, "object"] = {"decaffeination_method": _is_decaf}


def applicable_families(prose: str) -> list[str]:
    """Spec families that apply to this product, in ``SPEC_FAMILIES`` order (deterministic).

    ONE definition, both consumers (CLAUDE.md "one normalizer"): the rubric uses it for coverage
    denominators + gap emission, and the Optimizer uses it to gate targeting so a non-decaf coffee
    is never asked for (or given a to-do about) a decaffeination method. ``prose`` is the text each
    caller already has — title+body for the rubric, the combined source text for the Optimizer.
    """
    normalized = normalize_text(prose)
    return [
        family
        for family in SPEC_FAMILIES
        if family not in CONDITIONAL_FAMILIES or CONDITIONAL_FAMILIES[family](normalized)
    ]

# Derived view for the rubric — the detection phrases, exactly as before. Do NOT hand-edit; it is
# the single SPEC_FAMILIES definition projected.
SPEC_VOCABULARY: dict[str, tuple[str, ...]] = {
    family: spec.detect for family, spec in SPEC_FAMILIES.items()
}


def detect_hit(family: str, text: str) -> str | None:
    """The first ``SPEC_VOCABULARY[family]`` phrase literally present in ``text``, else ``None``.

    **The PRESENCE question** — "is this family mentioned at all?" — and the audit's own test,
    extracted verbatim from ``evaluate_product``'s ``in_prose`` comprehension so there is ONE
    definition with two consumers (CLAUDE.md "one normalizer"):

    * the rubric asks it of the PROSE (title+body) to split ``unstructured`` from ``absent``;
    * the Optimizer asks it of EVERY source field as the **negative grounding guard** — a
      merchant-facing "absent" claim is forbidden while any token of the family is literally
      present (step 2e). Returning the matched *phrase* rather than a bool lets the caller cite
      what it found, so a downgraded to-do carries its evidence.

    Normalized substring matching (``normalize_text``), NOT whole-word: byte-for-byte the test the
    rubric has always used, so detection and the three-state classification are unchanged.
    """
    spec_phrases = SPEC_VOCABULARY.get(family)
    if not spec_phrases:
        return None
    normalized = normalize_text(text)
    return next((phrase for phrase in spec_phrases if normalize_text(phrase) in normalized), None)


def detect_hit_in_fields(family: str, source_fields: dict[str, str]) -> tuple[str, str] | None:
    """``(source_field, matched_text)`` for the first field mentioning ``family``, else ``None``.

    The Optimizer's negative guard operates over ALL source fields, not just the prose: an
    "absent" to-do claims the family is in **no source field**, so the guard must check the same
    breadth the claim asserts (a superset of the audit's title+body). Field order is the caller's
    dict order, which ``graph.optimizer._build_source_fields`` fixes — so this is deterministic.

    ``matched_text`` is quoted back to the merchant in a downgraded to-do, so it is the span as
    THEY wrote it (``"Single-origin"``) rather than the vocabulary spelling (``"single origin"``)
    wherever the two differ and the span is locatable — detection is normalized-substring, so a
    phrase matched inside a longer word has no whole-word span and keeps the vocabulary form.
    """
    for field_name, text in source_fields.items():
        if not text:
            continue
        phrase = detect_hit(family, text)
        if phrase is not None:
            return field_name, _find_original_span(phrase, text) or phrase
    return None


def _normalize_key(text: str) -> str:
    """Normalize a metafield key for comparison. Underscores are word chars to ``normalize_text``
    (so ``roast_level`` would not equal ``roast level``); flatten them to spaces first."""
    return normalize_text(text.replace("_", " "))


# Taxonomy attribute handle -> family (step 2d). ``structured`` now means "carried by the TAXONOMY
# attribute" — the ``shopify`` reserved namespace, the channel Shopify feeds to agentic surfaces —
# NOT ``custom.*`` (the 2c spike proved custom.* is not legible). Built from the single
# ``taxonomy_map`` definition, so only the 4 taxonomy-home families can ever be structured.
_TAXONOMY_STRUCTURED_KEY = "shopify"
_STRUCTURED_KEYS: dict[str, str] = {
    _normalize_key(attribute.handle): family
    for family, attribute in TAXONOMY_ATTRIBUTES.items()
}


def structured_families(metafields: list[dict] | None) -> set[str]:
    """Spec families already carried by their **taxonomy attribute** — the ``structured`` state.

    **One definition, both consumers** (CLAUDE.md "one normalizer"): the rubric reads it for
    ``taxonomy_coverage``, and the Optimizer subtracts it from the spec families to get its targets.
    Because targeting is derived from THIS and never from ``detect``/``audit.gaps``, refining
    detection can never add or remove a fill opportunity.

    Step 2d: a family is structured only when a metafield in the reserved ``shopify`` namespace is
    keyed to its taxonomy attribute handle (e.g. ``shopify.coffee-roast``). A ``custom.*`` metafield
    — including the ones the pre-2d Optimizer wrote — does **not** count: it is not the legible
    channel. A key with an empty/absent value does not count either.
    """
    found: set[str] = set()
    for field in metafields or []:
        if not isinstance(field, dict):
            continue
        if normalize_text(str(field.get("namespace") or "")) != _TAXONOMY_STRUCTURED_KEY:
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


# --- Deterministic value recovery (step 2e) -------------------------------------------------
# The Optimizer's fallback when the LLM extractor flakily misses a spec that is literally in the
# source. Pure, LLM-free and keyed on the SAME ``SPEC_FAMILIES`` definition — two projections for
# two questions, no second vocabulary: ``detect`` answers PRESENCE (above), ``values``/``kind``
# answer VALUE (here, arbitrated by the unchanged ``validate_spec_value``).

# A value phrase owned by MORE THAN ONE family cannot be deterministically assigned to either, so
# recovery refuses it. Derived at import time, never hand-maintained: today this is exactly
# {"espresso"} (a roast level AND a brew method). Without this an "Espresso Roast" product would
# gain a fabricated ``Brew Method: espresso`` line — trading a false negative for a false positive,
# which is strictly worse under the never-fabricate rule. The LLM path does not need this guard: a
# model reads the surrounding sentence, whereas this scan sees only the token.
def _build_value_owners() -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for family, spec in SPEC_FAMILIES.items():
        for value in spec.values:
            owners.setdefault(normalize_text(value), set()).add(family)
    return owners


_AMBIGUOUS_VALUES: frozenset[str] = frozenset(
    phrase for phrase, owners in _build_value_owners().items() if len(owners) > 1
)


class RecoveredValue(BaseModel):
    """A spec value recovered deterministically from source text after extraction missed it.

    Shaped to be interchangeable with an LLM ``AttributeCandidate``: the Optimizer puts it through
    the SAME ``ground_attribute`` + ``validate_spec_value`` guards, so recovery is not a second,
    weaker path into a fix — it is a second way to *propose* a candidate for the existing guards.
    """

    family: str
    value: str
    source_field: str
    snippet: str


def _find_original_span(term: str, text: str) -> str | None:
    """Locate ``term`` in ``text`` as a whole word/phrase; return the ORIGINAL-CASED span.

    ``normalize_text`` casefolds and flattens punctuation to spaces, so a value recovered from the
    normalized form would read ``"ethiopia"``. Merchants see this value in a description line, so
    the span is taken from the raw text instead — ``"Ethiopia"``. Mirrors ``_contains_term``'s
    word-boundary semantics (punctuation between words, none inside them), so ``"pour-over"``
    matches the term ``"pour over"`` while ``"background"`` never matches ``"ground"``.
    """
    words = normalize_text(term).split()
    if not words:
        return None
    pattern = r"(?<!\w)" + r"[^\w]+".join(re.escape(word) for word in words) + r"(?!\w)"
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(0) if match else None


def recover_spec_value(family: str, source_fields: dict[str, str]) -> RecoveredValue | None:
    """Recover ``family``'s value from ``source_fields`` deterministically, or ``None``.

    **Gated on presence first** (``detect_hit_in_fields``): a family that is not mentioned is not
    mined for a value, which is what stops a stray ``"5 m tall"`` from becoming an altitude or a
    bare ``"ground"`` in unrelated prose from becoming a product form. Per family ``kind``:

    * ``closed`` — the family's own ``values``, longest phrase first so ``"medium dark"`` wins over
      ``"medium"``; cross-family-ambiguous phrases refused.
    * ``format`` — the altitude pattern; the matched span IS the value (``"1,900-2,100 masl"``).
    * ``open``   — **no recovery.** ``tasting_notes`` has no closed vocabulary, so any span this
      picked would be an invented boundary. It falls to the caller's "mentioned, no value" path.

    Returns ``None`` whenever no value can be read — the caller must then emit a to-do that says
    so truthfully, never an absence claim.
    """
    spec = SPEC_FAMILIES.get(family)
    if spec is None or detect_hit_in_fields(family, source_fields) is None:
        return None

    if spec.kind == "closed":
        # Longest phrase first: "medium dark" must win over "medium" wherever both are present.
        ordered = sorted(
            spec.values, key=lambda v: len(normalize_text(v).split()), reverse=True
        )
        for value_phrase in ordered:
            if normalize_text(value_phrase) in _AMBIGUOUS_VALUES:
                continue
            for field_name, text in source_fields.items():
                if not text or not _contains_term(value_phrase, text):
                    continue
                span = _find_original_span(value_phrase, text) or value_phrase
                return RecoveredValue(
                    family=family, value=span, source_field=field_name, snippet=span
                )
        return None

    if spec.kind == "format":
        for field_name, text in source_fields.items():
            match = _ALTITUDE_SPAN_RE.search(text or "")
            if match is not None:
                span = match.group(0).strip()
                return RecoveredValue(
                    family=family, value=span, source_field=field_name, snippet=span
                )
    return None


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
    # PROSE coverage: families stated in title/body / applicable spec families. Informational; the
    # addressable set is the difference between this and taxonomy_coverage.
    spec_coverage: float | None  # None when the class is not spec-scored or the product is excluded
    # Headline AI-legibility: families written to their taxonomy attribute / applicable
    # taxonomy-home families — the channel Shopify feeds to agentic surfaces. NOT blended.
    taxonomy_coverage: float | None
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
            taxonomy_coverage=None,
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
    taxonomy_coverage: float | None = None
    if product_class in SPEC_SCORED_CLASSES:
        prose = _prose_text(title, body)
        # Applicable families only: decaffeination_method is dropped for a non-decaf coffee, so it
        # never counts as an "absent" gap or dilutes a denominator (mirrors GTIN → equipment).
        applicable = applicable_families(prose)
        applicable_set = set(applicable)
        structured = structured_families(metafields) & applicable_set
        # ``detect_hit`` IS this comprehension's old body, extracted and named (step 2e) so the
        # rubric and the Optimizer's negative guard can never fork on what "mentioned" means.
        # Same vocabulary, same normalized substring test — classification is unchanged.
        in_prose = {family for family in applicable if detect_hit(family, prose)}
        spec_coverage = len(in_prose) / len(applicable)
        # Taxonomy coverage: the headline channel, over applicable taxonomy-home families only.
        home = [f for f in applicable if f in TAXONOMY_HOME_FAMILIES]
        taxonomy_coverage = len(structured & set(home)) / len(home) if home else None

        # Three-state (detect-based proxy): a structured family is machine-readable and emits no
        # gap; everything else is a gap tagged with WHY it is one, which drives both the weight and
        # the merchant-facing detail.
        for family in applicable:
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
        taxonomy_coverage=taxonomy_coverage,
        severity=_severity(score),
    )


def has_structured_metafields(metafields: list[dict] | None) -> bool:
    """True if the product carries at least one structured metafield.

    Metafield coverage is a **store-level** finding (e.g. "0 of 18 discoverable products carry
    structured metafields"), rolled up by the caller across the population — it is deliberately not
    a per-product gap, so an empty catalog does not inflate every product's severity.
    """
    return bool(metafields)
