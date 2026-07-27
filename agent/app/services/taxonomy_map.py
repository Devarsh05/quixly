"""Static Standard-taxonomy map for the taxonomy write path (Phase 3, step 2d).

The 2c write-target spike (``docs/decisions/2c-write-target.md``) settled the write mechanics:

* **L1** — a category-attribute metafield (e.g. ``shopify.coffee-roast``) is
  ``list.metaobject_reference`` and **rejects the canonical ``TaxonomyValue`` GID**
  (``INVALID_VALUE`` "Value require that you select a metaobject."). It needs a **per-shop
  metaobject-entry GID**.
* **L2 + followup** — that metaobject GID is invisible without the ``read_metaobjects`` scope, and
  assigning the category does not expose it.

So GID resolution is split in two, and **this module holds only the STATIC half** (baked from the
published taxonomy files, version ``v2026-05`` — the coffee attribute set/values are identical
across ``v2026-05`` / ``main`` / ``2026-08-unstable``, cross-checked in the spike):

1. **STATIC (here):** a grounded, canonicalised value → its canonical ``TaxonomyValue`` GID. Pure,
   deterministic, testable now — no network, no scope.
2. **LIVE (Step-4 Publisher, post-consent):** that ``TaxonomyValue`` GID → the per-shop
   ``Metaobject`` GID, via a ``read_metaobjects`` Admin call (the resolver contract in the plan).

The Optimizer stays Shopify-free: it emits a taxonomy metafield fix carrying the
``taxonomy_value_gid`` from this map, and the Publisher resolves the metaobject GID at write time.

**Only the 4 taxonomy-home families live here** (roast, origin, coffee_product_form,
decaffeination_method). tasting_notes is native but a closed 30-value list that excludes specialty
notes, so it is description-only; process / variety / altitude / brew_method have no taxonomy
attribute at all. A grounded value that does not appear in the map below (e.g. an origin *region*
like "Yirgacheffe", which is not a taxonomy ``Country``) yields NO taxonomy fix — it falls to the
description path. We never fabricate a canonical value to force a taxonomy write.
"""

from app.services.matching import normalize_text

# Standard-taxonomy category GIDs for the ``category`` fix (spike §1).
CATEGORY_COFFEE_BEANS_GROUND = "gid://shopify/TaxonomyCategory/fb-1-3-1"  # whole-bean / ground
CATEGORY_COFFEE_CONCENTRATE = "gid://shopify/TaxonomyCategory/fb-1-3-5"  # cold-brew concentrate


class TaxonomyAttribute:
    """One taxonomy-home family's write target: the ``shopify`` attribute handle + a canonical
    value → ``TaxonomyValue`` GID table.

    ``values`` keys are the canonical taxonomy value spellings; lookups are normalized-substring so
    a grounded ``"Medium-Light"`` / ``"medium light roast"`` resolves to ``Medium-light``. The
    metafield ``type`` is ``list.metaobject_reference`` for every category choice-list attribute.
    """

    metafield_type = "list.metaobject_reference"

    def __init__(self, handle: str, values: dict[str, str]):
        self.handle = handle
        self.values = values
        # Precomputed normalized keys, longest-first so "medium-light" wins over "medium".
        self._normalized = sorted(
            ((normalize_text(name), name, gid) for name, gid in values.items()),
            key=lambda t: len(t[0].split()),
            reverse=True,
        )

    def resolve(self, grounded_value: str) -> tuple[str, str] | None:
        """Map a grounded value to ``(canonical_value, taxonomy_value_gid)`` or ``None``.

        ``None`` means the value is not a member of this taxonomy attribute (e.g. an origin region
        that is not a ``Country``) — the caller must NOT write a taxonomy metafield and should fall
        back to the description path. Never invents a value.
        """
        tokens = normalize_text(grounded_value).split()
        if not tokens:
            return None
        for norm_name, canonical, gid in self._normalized:
            name_tokens = norm_name.split()
            span = len(name_tokens)
            if span and any(
                tokens[i : i + span] == name_tokens for i in range(len(tokens) - span + 1)
            ):
                return canonical, gid
        return None


# gid values from the published taxonomy ``attribute_values.txt`` (v2026-05); roast Medium-light
# = 7459 matches the spike's L1 evidence exactly.
TAXONOMY_ATTRIBUTES: dict[str, TaxonomyAttribute] = {
    "roast_level": TaxonomyAttribute(
        "coffee-roast",
        {
            "Light": "gid://shopify/TaxonomyValue/19313",
            "Medium-light": "gid://shopify/TaxonomyValue/7459",
            "Medium": "gid://shopify/TaxonomyValue/19314",
            "Medium-dark": "gid://shopify/TaxonomyValue/9422",
            "Dark": "gid://shopify/TaxonomyValue/19315",
        },
    ),
    "coffee_product_form": TaxonomyAttribute(
        "coffee-product-form",
        {
            "Whole bean": "gid://shopify/TaxonomyValue/8285",
            "Ground": "gid://shopify/TaxonomyValue/7556",
        },
    ),
    "decaffeination_method": TaxonomyAttribute(
        "decaffeination-method",
        {
            "Swiss Water": "gid://shopify/TaxonomyValue/53270",
            "Mountain Water": "gid://shopify/TaxonomyValue/53269",
            "Carbon Dioxide": "gid://shopify/TaxonomyValue/53266",
            "Methylene Chloride": "gid://shopify/TaxonomyValue/53268",
            "Ethyl Acetate": "gid://shopify/TaxonomyValue/53267",
        },
    ),
    "origin": TaxonomyAttribute(
        "country",
        {
            # Real taxonomy ``Country`` values only. Coffee regions (Yirgacheffe, Huila, Java,
            # Sumatra) are NOT countries and deliberately absent → description-only.
            "Ethiopia": "gid://shopify/TaxonomyValue/8882",
            "Colombia": "gid://shopify/TaxonomyValue/8864",
            "Kenya": "gid://shopify/TaxonomyValue/8909",
            "Guatemala": "gid://shopify/TaxonomyValue/8892",
            "Costa Rica": "gid://shopify/TaxonomyValue/8868",
            "Brazil": "gid://shopify/TaxonomyValue/8810",
            "Tanzania": "gid://shopify/TaxonomyValue/8983",
            "El Salvador": "gid://shopify/TaxonomyValue/8877",
            "Honduras": "gid://shopify/TaxonomyValue/8897",
            "Rwanda": "gid://shopify/TaxonomyValue/8959",
            "Peru": "gid://shopify/TaxonomyValue/8954",
            "Panama": "gid://shopify/TaxonomyValue/8951",
            "Nicaragua": "gid://shopify/TaxonomyValue/8943",
            "Mexico": "gid://shopify/TaxonomyValue/8818",
            "Burundi": "gid://shopify/TaxonomyValue/8857",
            "Yemen": "gid://shopify/TaxonomyValue/8999",
            "India": "gid://shopify/TaxonomyValue/8900",
            "Uganda": "gid://shopify/TaxonomyValue/8992",
            "Bolivia": "gid://shopify/TaxonomyValue/8851",
            "Ecuador": "gid://shopify/TaxonomyValue/8875",
            "Jamaica": "gid://shopify/TaxonomyValue/8906",
            "Indonesia": "gid://shopify/TaxonomyValue/8901",
        },
    ),
}

# The families with a taxonomy write target (spike Decision). Everything else is description-only.
TAXONOMY_HOME_FAMILIES = frozenset(TAXONOMY_ATTRIBUTES)


def resolve_taxonomy_value(family: str, grounded_value: str) -> tuple[str, str, str] | None:
    """``(attribute_handle, canonical_value, taxonomy_value_gid)`` for a grounded value, or None.

    ``None`` for a non-taxonomy family or a value that is not a member of the family's taxonomy
    attribute — the caller then routes to description-only, never a fabricated taxonomy value.
    """
    attribute = TAXONOMY_ATTRIBUTES.get(family)
    if attribute is None:
        return None
    resolved = attribute.resolve(grounded_value)
    if resolved is None:
        return None
    canonical, gid = resolved
    return attribute.handle, canonical, gid
