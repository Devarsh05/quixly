"""Extractor: extraction, grounding, self-match, and persistence.

Uses the real Postgres-backed ``db`` fixture (transaction rolled back per test). The OpenAI
structured-output call is faked at the ``ExtractorClient`` boundary; the grounding check and the
alias matcher are the real code under test.
"""

import pytest
from sqlalchemy import select

from app.graph.extractor import STORE_ALIASES, run_extractor
from app.models import EngineRun, Shop, ShopStatus
from app.models import QueryPanel as QueryPanelRow
from app.services.extractor_llm import ExtractedBrand, ExtractedBrands, ExtractorError

SHOP = "extractor-test.myshopify.com"


def _brand(
    rank: int, brand: str, product: str | None = None, verbatim: str = "v"
) -> ExtractedBrand:
    return ExtractedBrand(rank=rank, brand=brand, product=product, verbatim=verbatim)


def _content(text: str) -> dict:
    """A stored engine payload shaped like EngineRunner writes it."""
    return {"choices": [{"message": {"content": text}}]}


class FakeExtractorClient:
    """Canned extractor: returns per-answer-text brands, or raises for texts in ``fail``."""

    def __init__(
        self,
        answers: dict[str, ExtractedBrands] | None = None,
        fail: set[str] | None = None,
    ):
        self._answers = answers or {}
        self._fail = fail or set()

    async def extract(self, answer_text: str) -> ExtractedBrands:
        if answer_text in self._fail:
            raise ExtractorError(f"boom for {answer_text!r}")
        return self._answers.get(answer_text, ExtractedBrands(brands=[]))


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


async def _seed_rows(db, shop_id: int, raws: list[dict], fingerprint: str = "fp1") -> int:
    """Create a panel and one engine_run per raw payload; return the panel id."""
    panel = QueryPanelRow(
        shop_id=shop_id,
        category="coffee",
        queries_json=[{"text": f"q{i}"} for i in range(len(raws))],
        fingerprint=fingerprint,
    )
    db.add(panel)
    await db.commit()
    await db.refresh(panel)

    for i, raw in enumerate(raws):
        db.add(EngineRun(panel_id=panel.id, engine="perplexity", query=f"q{i}", response_raw=raw))
    await db.commit()
    return panel.id


async def _rows(db, panel_id: int) -> list[EngineRun]:
    result = await db.execute(
        select(EngineRun).where(EngineRun.panel_id == panel_id).order_by(EngineRun.id)
    )
    return list(result.scalars().all())


# --- extraction + ordered persistence + self-mention -----------------------------------------


async def test_updates_rows_with_ordered_brands_and_self_mention(db, shop):
    text = "For espresso I'd pick Blue Bottle, but Northwind Coffee is a great value."
    panel_id = await _seed_rows(db, shop.id, [_content(text)])
    client = FakeExtractorClient(
        answers={
            text: ExtractedBrands(
                brands=[
                    _brand(1, "Blue Bottle"),
                    _brand(2, "Northwind Coffee", product="Northwind Espresso"),
                ]
            )
        }
    )

    report = await run_extractor(db, panel_id, client)

    assert report.processed == 1
    assert report.mentioned_count == 1
    assert report.rejected_hallucinations == []
    assert report.failures == []

    (row,) = await _rows(db, panel_id)
    assert row.cited_brands_json == [
        {"rank": 1, "brand": "Blue Bottle", "product": None},
        {"rank": 2, "brand": "Northwind Coffee", "product": "Northwind Espresso"},
    ]
    assert row.our_mentions_json == {
        "mentioned": True,
        "ranks": [2],
        "matched_alias": "Northwind Coffee",
        "products": ["Northwind Espresso"],
    }


async def test_mentioned_false_when_no_alias_matches(db, shop):
    text = "I recommend Blue Bottle and Stumptown for a bright cup."
    panel_id = await _seed_rows(db, shop.id, [_content(text)])
    client = FakeExtractorClient(
        answers={text: ExtractedBrands(brands=[_brand(1, "Blue Bottle"), _brand(2, "Stumptown")])}
    )

    report = await run_extractor(db, panel_id, client)

    assert report.processed == 1
    assert report.mentioned_count == 0

    (row,) = await _rows(db, panel_id)
    assert row.cited_brands_json == [
        {"rank": 1, "brand": "Blue Bottle", "product": None},
        {"rank": 2, "brand": "Stumptown", "product": None},
    ]
    assert row.our_mentions_json == {
        "mentioned": False,
        "ranks": [],
        "matched_alias": None,
        "products": [],
    }


# --- grounding: the no-fabrication guard (its own explicit test) ------------------------------


async def test_grounding_rejects_ungrounded_brand(db, shop):
    # "Phantom Roasters" is NOT in the text; "Blue Bottle" is. The ungrounded brand is dropped and
    # the surviving one is re-ranked to 1 — never persist a brand absent from the source.
    text = "Honestly, just buy Blue Bottle. It's the safe choice."
    panel_id = await _seed_rows(db, shop.id, [_content(text)])
    client = FakeExtractorClient(
        answers={
            text: ExtractedBrands(
                brands=[_brand(1, "Phantom Roasters"), _brand(2, "Blue Bottle")]
            )
        }
    )

    report = await run_extractor(db, panel_id, client)

    assert report.processed == 1
    (row,) = await _rows(db, panel_id)
    assert [(r.engine_run_id, r.brand) for r in report.rejected_hallucinations] == [
        (row.id, "Phantom Roasters")
    ]
    # Only the grounded brand persists, re-ranked to 1.
    assert row.cited_brands_json == [{"rank": 1, "brand": "Blue Bottle", "product": None}]


async def test_explicitly_named_secondary_brand_survives(db, shop):
    # Recall guard: a brand explicitly named but framed as a budget/secondary option must NOT be
    # dropped. All four named brands are present in the text, so grounding keeps all four — the
    # low-prominence one lands at the lowest rank, never omitted. (The prompt fix drives the live
    # model to emit all four; this locks the node-side grounding/persistence contract.)
    text = (
        "Blue Bottle is my top pick, with Stumptown and Counter Culture close behind. "
        "Peets is a solid budget option."
    )
    panel_id = await _seed_rows(db, shop.id, [_content(text)])
    client = FakeExtractorClient(
        answers={
            text: ExtractedBrands(
                brands=[
                    _brand(1, "Blue Bottle"),
                    _brand(2, "Stumptown"),
                    _brand(3, "Counter Culture"),
                    _brand(4, "Peets"),
                ]
            )
        }
    )

    report = await run_extractor(db, panel_id, client)

    assert report.processed == 1
    assert report.rejected_hallucinations == []
    (row,) = await _rows(db, panel_id)
    assert row.cited_brands_json == [
        {"rank": 1, "brand": "Blue Bottle", "product": None},
        {"rank": 2, "brand": "Stumptown", "product": None},
        {"rank": 3, "brand": "Counter Culture", "product": None},
        {"rank": 4, "brand": "Peets", "product": None},
    ]


# --- idempotency: NULL-only by default, force re-runs ----------------------------------------


async def test_idempotency_null_only_selection_and_force(db, shop):
    text = "Try Blue Bottle for a reliable espresso."
    panel_id = await _seed_rows(db, shop.id, [_content(text)])

    first = FakeExtractorClient(answers={text: ExtractedBrands(brands=[_brand(1, "Blue Bottle")])})
    await run_extractor(db, panel_id, first)
    (row,) = await _rows(db, panel_id)
    assert row.cited_brands_json == [{"rank": 1, "brand": "Blue Bottle", "product": None}]

    # A default re-run must NOT touch already-filled rows, even if the client would now answer
    # differently (Northwind is present in the text so it would ground if reprocessed).
    changed = FakeExtractorClient(
        answers={text: ExtractedBrands(brands=[_brand(1, "Northwind Coffee")])}
    )
    report = await run_extractor(db, panel_id, changed)
    assert report.processed == 0
    (row,) = await _rows(db, panel_id)
    assert row.cited_brands_json == [{"rank": 1, "brand": "Blue Bottle", "product": None}]

    # force=True reprocesses the filled row. Repoint it at a text that contains Northwind so the
    # re-extraction grounds and self-matches.
    text2 = "Northwind Coffee is the pick here."
    (r0,) = await _rows(db, panel_id)
    r0.response_raw = _content(text2)
    await db.commit()

    forced = FakeExtractorClient(
        answers={text2: ExtractedBrands(brands=[_brand(1, "Northwind Coffee")])}
    )
    report = await run_extractor(db, panel_id, forced, force=True)
    assert report.processed == 1
    (row,) = await _rows(db, panel_id)
    assert row.cited_brands_json == [{"rank": 1, "brand": "Northwind Coffee", "product": None}]
    assert row.our_mentions_json["mentioned"] is True


# --- one failing row does not sink the batch -------------------------------------------------


async def test_one_row_failure_leaves_columns_null_and_batch_survives(db, shop):
    good = "Blue Bottle is a solid choice."
    bad = "This answer makes the extractor explode."
    panel_id = await _seed_rows(db, shop.id, [_content(good), _content(bad)])
    client = FakeExtractorClient(
        answers={good: ExtractedBrands(brands=[_brand(1, "Blue Bottle")])},
        fail={bad},
    )

    report = await run_extractor(db, panel_id, client)

    assert report.processed == 1
    assert len(report.failures) == 1

    rows = await _rows(db, panel_id)
    by_query = {r.query: r for r in rows}
    good_row, bad_row = by_query["q0"], by_query["q1"]

    assert good_row.cited_brands_json == [{"rank": 1, "brand": "Blue Bottle", "product": None}]
    # The failed row keeps BOTH columns NULL so a re-run retries it — no error envelope.
    assert bad_row.cited_brands_json is None
    assert bad_row.our_mentions_json is None
    assert report.failures[0].engine_run_id == bad_row.id


# --- error rows (no answer) are skipped ------------------------------------------------------


async def test_error_rows_are_skipped(db, shop):
    panel_id = await _seed_rows(db, shop.id, [{"error": "Perplexity returned 500"}])
    client = FakeExtractorClient()

    report = await run_extractor(db, panel_id, client)

    assert report.processed == 0
    assert report.failures == []
    (row,) = await _rows(db, panel_id)
    assert row.cited_brands_json is None
    assert row.our_mentions_json is None


# --- the reusable matcher (used verbatim by step 4) ------------------------------------------


def test_normalize_and_match_collapses_suffixes_and_aliases():
    from app.graph.extractor import normalize_and_match

    names = ["Blue Bottle", "Northwind Coffee Roasters", "Stumptown"]
    matches = normalize_and_match(names, STORE_ALIASES)

    assert len(matches) == 1
    assert matches[0].index == 1
    assert matches[0].name == "Northwind Coffee Roasters"
    assert matches[0].matched_alias == "Northwind Coffee"
