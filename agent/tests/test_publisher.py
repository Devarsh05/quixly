"""The Publisher node (Phase 3, step 4) — the first code that writes to a live merchant store.

These are the behavioural tests, driven against the transaction-scoped ``db`` fixture with a fake
Admin client that records every mutation. They are **not sufficient on their own**: the live
contract with Shopify is covered by ``test_publisher_live`` (opt-in, no mocks), because green tests
over a mock are not evidence that a write works.

What each assertion is protecting:

* **staleness** — a fix whose product moved after approval is never written, in either layer;
* **a 200 is not a success** — a write whose re-read does not confirm is ``publish_failed``, never
  ``published``, and ``published_at`` stays NULL;
* **replay cannot double-append** — the append-only description write is the one that would corrupt
  merchant copy, so reconciliation and the status guard are tested from both directions;
* **nothing non-publishable can reach a write** — asserted, not assumed, because the approval gate
  is a different module and a bypass there must fail loudly here;
* **the token invariant** — this node never becomes a second token path.
"""

import copy
import inspect
from datetime import UTC, datetime

import pytest

from app.graph import publisher as publisher_module
from app.graph.publisher import PublishAborted, run_publisher
from app.jobs import publish as publish_job_module
from app.models import Fix, FixStatus, FixType, Product, Shop, ShopStatus
from app.services.catalog import product_row_from_node, stable_source_hash
from app.services.shopify_admin import ShopifyWriteError

# A dedicated domain: the real dev store already has a ``shops`` row in the local database, and
# the allowlist is injected into ``run_publisher`` rather than read from settings, so these tests
# never need the live domain. ``test_the_default_allowlist_is_the_dev_store_alone`` covers the
# default itself.
SHOP = "publisher-test.myshopify.com"
ALLOWED = SHOP
PRODUCT_GID = "gid://shopify/Product/15436808192371"

BODY_BEFORE = "<p>A bright everyday coffee.</p>"
DETAILS = (
    "<p><strong>Details</strong></p>"
    "<ul><li>Origin: Ethiopia</li><li>Process: Washed</li></ul>"
)
BODY_AFTER = BODY_BEFORE + DETAILS

CATEGORY_GID = "gid://shopify/TaxonomyCategory/fb-1-3-1"
CATEGORY_NAME = "Food, Beverages & Tobacco > Beverages > Coffee > Coffee Beans & Ground Coffee"
UNCATEGORIZED = {"id": "gid://shopify/TaxonomyCategory/na", "name": "Uncategorized",
                 "fullName": "Uncategorized"}


def make_node(*, body: str = BODY_BEFORE, category: dict | None = None) -> dict:
    """A GraphQL product node in exactly the shape ``PRODUCT_FIELDS`` selects."""
    return {
        "id": PRODUCT_GID,
        "title": "Ethiopia Yirgacheffe 340 g",
        "descriptionHtml": body,
        "status": "ACTIVE",
        "productType": "Coffee",
        "category": copy.deepcopy(UNCATEGORIZED if category is None else category),
        "variants": {"nodes": [
            {"id": "gid://shopify/ProductVariant/1", "title": "340 g", "sku": "ETH-340",
             "barcode": None, "price": "18.00", "inventoryQuantity": 12},
        ]},
        "metafields": {"nodes": []},
    }


class FakeShopify:
    """Records every mutation and models the store's state, so replay is observable.

    ``land`` controls whether a write actually takes effect — that is how a "200 but the result is
    wrong or absent" is simulated, which is the failure mode a transport-status check misses.
    """

    def __init__(self, node: dict | None, *, land: bool = True, raises: Exception | None = None):
        self.node = node
        self.land = land
        self.raises = raises
        self.mutations: list[dict] = []
        self.reads = 0

    async def fetch_product(self, product_gid: str) -> dict | None:
        self.reads += 1
        return copy.deepcopy(self.node) if self.node is not None else None

    async def update_product(self, product_input: dict) -> dict:
        self.mutations.append(copy.deepcopy(product_input))
        if self.raises is not None:
            raise self.raises
        if self.land:
            if "descriptionHtml" in product_input:
                self.node["descriptionHtml"] = product_input["descriptionHtml"]
            if "category" in product_input:
                self.node["category"] = {"id": product_input["category"], "name": "Coffee",
                                         "fullName": CATEGORY_NAME}
        return copy.deepcopy(self.node)

    @property
    def written_fields(self) -> list[str]:
        return [key for call in self.mutations for key in call if key != "id"]


async def seed(db, node: dict, *, category_name: str | None = None) -> tuple[Shop, Product]:
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.flush()

    row = product_row_from_node(node)
    if category_name is not None:
        row["category"] = category_name
    product = Product(shop_id=shop.id, visibility_state="active", **row)
    db.add(product)
    await db.flush()
    return shop, product


def description_fix(product: Product, node: dict, **overrides) -> Fix:
    defaults = dict(
        product_id=product.id,
        type=FixType.description,
        status=FixStatus.approved,
        target="body_html",
        before_json={"body_html": BODY_BEFORE},
        after_json={"body_html": BODY_AFTER},
        source_json=[{"attribute": "origin", "source_field": "title", "snippet": "Ethiopia"}],
        base_source_hash=stable_source_hash(product_row_from_node(node)),
    )
    return Fix(**{**defaults, **overrides})


def category_fix(product: Product, node: dict, **overrides) -> Fix:
    defaults = dict(
        product_id=product.id,
        type=FixType.category,
        status=FixStatus.approved,
        target="category",
        before_json={"category": "Uncategorized"},
        after_json={"category": CATEGORY_GID, "fullName": CATEGORY_NAME},
        source_json=[{"attribute": "category", "source_field": "product_type",
                      "snippet": "Coffee"}],
        base_source_hash=stable_source_hash(product_row_from_node(node)),
    )
    return Fix(**{**defaults, **overrides})


async def publish(db, shop, client, *, allowed: str = ALLOWED):
    return await run_publisher(db, shop, client, run_id=1, allowed_shops=allowed)


# --------------------------------------------------------------------------------------------
# The happy paths, per risk tier
# --------------------------------------------------------------------------------------------


async def test_description_write_is_verified_by_a_fresh_reread(db):
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    db.add(description_fix(product, node))
    await db.commit()

    client = FakeShopify(node)
    report = await publish(db, shop, client)

    assert client.written_fields == ["descriptionHtml"]
    assert client.mutations[0]["descriptionHtml"] == BODY_AFTER
    # A read BEFORE the write (snapshot) and a separate one AFTER it (verification). Verifying
    # from the mutation's own return payload would prove nothing.
    assert client.reads == 2

    fix = (await db.execute(Fix.__table__.select())).mappings().one()
    assert fix["status"] == FixStatus.verified
    assert fix["published_at"] is not None
    assert fix["publish_error"] is None
    assert report.verified == 1 and report.failed == 0 and report.stale == 0


async def test_category_write_lands_the_gid_and_refreshes_our_copy(db):
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    db.add(category_fix(product, node))
    await db.commit()

    client = FakeShopify(node)
    await publish(db, shop, client)

    assert client.mutations == [{"id": PRODUCT_GID, "category": CATEGORY_GID}]

    fix = (await db.execute(Fix.__table__.select())).mappings().one()
    assert fix["status"] == FixStatus.verified
    # Our stored copy is refreshed from the verifying read, so the next audit sees reality.
    await db.refresh(product)
    assert product.category == CATEGORY_NAME


async def test_description_publishes_before_category_and_low_risk_products_go_first(db):
    """Ascending risk: a systemic failure surfaces on a reversible write, never on a tax one."""
    node = make_node()
    shop, risky = await seed(db, node, category_name="Uncategorized")

    # A second, description-only product. Lower risk, so it must publish first.
    safe_node = make_node()
    safe_node["id"] = "gid://shopify/Product/999"
    safe = Product(shop_id=shop.id, visibility_state="active",
                   **product_row_from_node(safe_node))
    db.add(safe)
    await db.flush()

    db.add(category_fix(risky, node))
    db.add(description_fix(risky, node))
    db.add(description_fix(safe, safe_node))
    await db.commit()

    order: list[str] = []

    class Recording(FakeShopify):
        async def fetch_product(self, product_gid: str):
            self.node = node if product_gid == PRODUCT_GID else safe_node
            return await super().fetch_product(product_gid)

        async def update_product(self, product_input: dict):
            order.append(f"{product_input['id'].rsplit('/', 1)[1]}:"
                         f"{[k for k in product_input if k != 'id'][0]}")
            return await super().update_product(product_input)

    await publish(db, shop, Recording(node))

    assert order == ["999:descriptionHtml", "15436808192371:descriptionHtml",
                     "15436808192371:category"]


# --------------------------------------------------------------------------------------------
# Staleness — nothing is written when the product moved under us
# --------------------------------------------------------------------------------------------


async def test_layer_1_refuses_when_the_live_body_changed_after_approval(db):
    """The exact guard: appending to a body that changed could duplicate or corrupt it."""
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = description_fix(product, node)
    db.add(fix)
    await db.commit()

    node["descriptionHtml"] = "<p>The merchant rewrote this.</p>"
    client = FakeShopify(node)
    # Keep Layer 2 out of it, so this proves Layer 1 fired on its own.
    fix.base_source_hash = stable_source_hash(product_row_from_node(node))
    await db.commit()

    report = await publish(db, shop, client)

    assert client.mutations == []
    await db.refresh(fix)
    assert fix.status == FixStatus.stale
    assert "description changed" in fix.publish_error
    assert fix.published_at is None
    assert report.stale == 1


async def test_layer_1_refuses_to_clobber_a_category_the_merchant_chose(db):
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = category_fix(product, node)
    db.add(fix)
    await db.commit()

    node["category"] = {"id": "gid://shopify/TaxonomyCategory/fb-1-3-5", "name": "Concentrates",
                        "fullName": "…> Coffee > Coffee Concentrates"}
    client = FakeShopify(node)

    await publish(db, shop, client)

    assert client.mutations == []
    await db.refresh(fix)
    assert fix.status == FixStatus.stale
    assert "category changed" in fix.publish_error


async def test_layer_2_refuses_when_a_grounding_source_changed(db):
    """Drift in a field the fix GROUNDED on but does not WRITE — the title it read origin from."""
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = description_fix(product, node)
    db.add(fix)
    await db.commit()

    # Body untouched (Layer 1 would pass), but the title moved.
    node["title"] = "Colombia Huila 340 g"
    client = FakeShopify(node)

    await publish(db, shop, client)

    assert client.mutations == []
    await db.refresh(fix)
    assert fix.status == FixStatus.stale
    assert "grounded on has changed" in fix.publish_error


async def test_the_hash_is_writer_stable_across_graphql_and_rest_variant_shapes(db):
    """The reason Layer 2 uses a projection: our own publish fires products/update moments later.

    That webhook rewrites ``variants_json`` in the REST shape. Hashing the raw column would make
    the digest depend on which writer touched the row last, and every publish would then stale its
    own product's remaining fixes.
    """
    graphql_row = product_row_from_node(make_node())
    rest_row = dict(graphql_row)
    rest_row["variants_json"] = [
        {"id": 44123, "product_id": 999, "title": "340 g", "sku": "ETH-340", "price": "18.00",
         "barcode": None, "position": 1, "inventory_policy": "deny", "grams": 340,
         "compare_at_price": None, "taxable": True},
    ]

    assert stable_source_hash(graphql_row) == stable_source_hash(rest_row)


# --------------------------------------------------------------------------------------------
# A 200 is not a success
# --------------------------------------------------------------------------------------------


async def test_a_write_that_does_not_land_is_publish_failed_not_published(db):
    """The flagship: the API returned success and the result is absent. That is a FAILURE."""
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = description_fix(product, node)
    db.add(fix)
    await db.commit()

    client = FakeShopify(node, land=False)  # 200, no userErrors, nothing changed
    report = await publish(db, shop, client)

    assert client.mutations  # we did attempt the write
    await db.refresh(fix)
    assert fix.status == FixStatus.publish_failed
    assert fix.published_at is None
    assert "missing the appended line" in fix.publish_error
    assert report.failed == 1 and report.verified == 0


async def test_a_category_write_landing_the_wrong_gid_is_publish_failed(db):
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = category_fix(product, node)
    db.add(fix)
    await db.commit()

    class WrongCategory(FakeShopify):
        async def update_product(self, product_input: dict):
            self.mutations.append(copy.deepcopy(product_input))
            self.node["category"] = {"id": "gid://shopify/TaxonomyCategory/fb-1-3-5",
                                     "name": "x", "fullName": "x"}
            return copy.deepcopy(self.node)

    await publish(db, shop, WrongCategory(node))

    await db.refresh(fix)
    assert fix.status == FixStatus.publish_failed
    assert fix.published_at is None
    assert "fb-1-3-5" in fix.publish_error


async def test_user_errors_are_a_failure_even_though_the_transport_returned_200(db):
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = category_fix(product, node)
    db.add(fix)
    await db.commit()

    client = FakeShopify(node, raises=ShopifyWriteError("productUpdate(category) refused: [...]"))
    await publish(db, shop, client)

    await db.refresh(fix)
    assert fix.status == FixStatus.publish_failed
    assert "refused" in fix.publish_error


async def test_shopify_normalising_the_html_still_verifies_structurally(db):
    """Byte-identity of ``descriptionHtml`` is unproven, so a reformat must not read as a failure.

    The contract is structural: the merchant's copy survives and every appended line is present.
    """
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = description_fix(product, node)
    db.add(fix)
    await db.commit()

    class Normalising(FakeShopify):
        async def update_product(self, product_input: dict):
            self.mutations.append(copy.deepcopy(product_input))
            self.node["descriptionHtml"] = (
                product_input["descriptionHtml"]
                .replace("<ul>", "<ul>\n  ")
                .replace("</li>", "</li>\n  ")
                .replace("<strong>Details</strong>", "<strong>Details</strong> ")
            )
            return copy.deepcopy(self.node)

    await publish(db, shop, Normalising(node))

    await db.refresh(fix)
    assert fix.status == FixStatus.verified
    assert fix.published_at is not None


async def test_a_write_that_drops_the_merchants_copy_is_a_failure(db):
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = description_fix(product, node)
    db.add(fix)
    await db.commit()

    class LosesOriginal(FakeShopify):
        async def update_product(self, product_input: dict):
            self.mutations.append(copy.deepcopy(product_input))
            self.node["descriptionHtml"] = DETAILS  # the merchant's prose is gone
            return copy.deepcopy(self.node)

    await publish(db, shop, LosesOriginal(node))

    await db.refresh(fix)
    assert fix.status == FixStatus.publish_failed
    assert "original description is no longer present" in fix.publish_error


# --------------------------------------------------------------------------------------------
# Idempotence and replay — the append-only write must never run twice
# --------------------------------------------------------------------------------------------


async def test_replaying_a_verified_fix_writes_nothing(db):
    node = make_node(body=BODY_AFTER)
    shop, product = await seed(db, node, category_name="Uncategorized")
    db.add(description_fix(product, node, status=FixStatus.verified,
                           published_at=datetime.now(UTC)))
    await db.commit()

    client = FakeShopify(node)
    report = await publish(db, shop, client)

    assert client.mutations == []
    assert client.reads == 0  # a terminal row is not even fetched
    assert report.outcomes == []
    assert node["descriptionHtml"] == BODY_AFTER  # no second Details block


async def test_a_write_that_landed_before_a_crash_is_reconciled_without_rewriting(db):
    """The crash window: Shopify accepted the write, we died before recording it.

    Re-running must NOT append a second time. Reconciliation runs before the staleness gate
    precisely so this row resolves as "already live", not as "stale".
    """
    node = make_node(body=BODY_AFTER)  # the write is already live on the store
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = description_fix(product, node)
    # The fix was grounded on the PRE-publish body, so its hash is the pre-publish one.
    fix.base_source_hash = stable_source_hash(
        product_row_from_node(make_node(body=BODY_BEFORE))
    )
    db.add(fix)
    await db.commit()

    client = FakeShopify(node)
    report = await publish(db, shop, client)

    assert client.mutations == []
    await db.refresh(fix)
    assert fix.status == FixStatus.verified
    assert fix.published_at is not None
    assert report.verified == 1
    assert node["descriptionHtml"] == BODY_AFTER


async def test_a_landed_sibling_does_not_stale_the_category_fix_behind_it(db):
    """The hash rewind. Our own landed write moved the product; that must not stale its siblings."""
    node = make_node(body=BODY_AFTER)  # description already live, category still not assigned
    shop, product = await seed(db, node, category_name="Uncategorized")

    pre_publish_hash = stable_source_hash(product_row_from_node(make_node(body=BODY_BEFORE)))
    db.add(description_fix(product, node, base_source_hash=pre_publish_hash))
    db.add(category_fix(product, node, base_source_hash=pre_publish_hash))
    await db.commit()

    client = FakeShopify(node)
    report = await publish(db, shop, client)

    # The description reconciles with no write; the category still publishes.
    assert client.written_fields == ["category"]
    assert report.verified == 2 and report.stale == 0


async def test_a_published_row_left_by_a_crash_is_not_picked_up_again(db):
    """Only ``approved`` is a work item. ``published`` is mid-flight, not re-writable."""
    node = make_node(body=BODY_AFTER)
    shop, product = await seed(db, node, category_name="Uncategorized")
    db.add(description_fix(product, node, status=FixStatus.published))
    await db.commit()

    client = FakeShopify(node)
    await publish(db, shop, client)

    assert client.mutations == []


# --------------------------------------------------------------------------------------------
# Invariant breaches abort the run — nothing is written on state we know is broken
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fix_type,after_json",
    [
        (FixType.metafield, {"namespace": "shopify", "key": "coffee-roast", "value": "Light"}),
        (FixType.merchant_todo, None),
    ],
)
async def test_a_non_publishable_type_that_reached_approved_aborts_the_run(db, fix_type,
                                                                          after_json):
    """The approval gate refuses these. If one is approved anyway, the gate was bypassed."""
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    db.add(Fix(product_id=product.id, type=fix_type, status=FixStatus.approved,
               target="spec:roast", after_json=after_json))
    await db.commit()

    client = FakeShopify(node)
    with pytest.raises(PublishAborted, match="approval gate was bypassed"):
        await publish(db, shop, client)

    assert client.mutations == []


async def test_two_approved_rows_on_one_target_abort_the_run(db):
    """Supersede is what stops the append-only write running twice for one body."""
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    db.add(description_fix(product, node))
    db.add(description_fix(product, node))
    await db.commit()

    client = FakeShopify(node)
    with pytest.raises(PublishAborted, match="supersede failed"):
        await publish(db, shop, client)

    assert client.mutations == []


def test_the_default_allowlist_is_the_dev_store_alone():
    """Nothing but the dev store may be published to until the path is proven end-to-end.

    Widening this must be a deliberate commit, so the default is asserted rather than trusted.
    """
    from app.settings import Settings

    assert Settings().publish_allowed_shops == "quixly-ljymkoyb.myshopify.com"


async def test_a_shop_outside_the_allowlist_is_refused_before_any_read(db):
    """Staged rollout in code: no real merchant until the path is proven on the dev store."""
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    db.add(description_fix(product, node))
    await db.commit()

    client = FakeShopify(node)
    with pytest.raises(PublishAborted, match="not cleared for publishing"):
        await publish(db, shop, client, allowed="someone-else.myshopify.com")

    assert client.mutations == []
    assert client.reads == 0


async def test_a_fix_that_would_overwrite_rather_than_append_is_refused(db):
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = description_fix(product, node, after_json={"body_html": "<p>Replaced wholesale.</p>"})
    db.add(fix)
    await db.commit()

    client = FakeShopify(node)
    await publish(db, shop, client)

    assert client.mutations == []
    await db.refresh(fix)
    assert fix.status == FixStatus.publish_failed
    assert "append" in fix.publish_error


async def test_a_deleted_product_fails_the_fix_without_writing(db):
    node = make_node()
    shop, product = await seed(db, node, category_name="Uncategorized")
    fix = description_fix(product, node)
    db.add(fix)
    await db.commit()

    client = FakeShopify(None)  # product() → null
    await publish(db, shop, client)

    assert client.mutations == []
    await db.refresh(fix)
    assert fix.status == FixStatus.publish_failed
    assert "no longer exists" in fix.publish_error


# --------------------------------------------------------------------------------------------
# Token custody
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", [publisher_module, publish_job_module])
def test_the_publisher_never_becomes_a_second_token_path(module):
    """The app shell is the SINGLE refresh authority; this node must not fetch or hold a token.

    Structural, not behavioural, on purpose: the failure this guards against is someone adding a
    convenient direct call, which no amount of happy-path testing would catch.
    """
    source = inspect.getsource(module)
    assert "X-Shopify-Access-Token" not in source
    assert "admin-token" not in source
    assert "TokenProvider()" not in source  # the worker's shared instance, never a new one
