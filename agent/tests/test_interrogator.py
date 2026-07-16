"""Interrogator generation tests.

Pure generation logic (no DB/LLM/network): the panel must be deterministic — same inputs yield
the same queries in the same order and the same content fingerprint — and the snapshot below makes
any template/vocab/order change visible in the diff.
"""

from datetime import UTC, datetime

from app.graph.interrogator import (
    IntentCategory,
    build_query_panel,
)

# The full ordered coffee panel. A template, vocabulary, or ordering change must update this.
EXPECTED_COFFEE_TEXTS = [
    "best light coffee beans",
    "best medium coffee beans",
    "best dark coffee beans",
    "best espresso coffee beans",
    "best decaf coffee beans",
    "best Ethiopian coffee beans",
    "best Colombian coffee beans",
    "best Kenyan coffee beans",
    "best Guatemalan coffee beans",
    "best Costa Rican coffee beans",
    "best Brazilian coffee beans",
    "best Sumatran coffee beans",
    "best coffee beans for pour over",
    "best coffee beans for espresso",
    "best coffee beans for cold brew",
    "best coffee beans for French press",
    "best coffee beans for drip",
    "best coffee beans under $15",
    "best coffee beans under $20",
    "best coffee beans under $25",
    "best coffee beans for beginners",
    "best whole bean coffee",
    "best coffee subscription",
    "best coffee beans for a gift",
]


def test_snapshot_of_default_coffee_panel():
    panel = build_query_panel()
    assert [q.text for q in panel.queries] == EXPECTED_COFFEE_TEXTS


def test_deterministic_same_inputs_same_panel_and_fingerprint():
    now = datetime(2026, 7, 16, tzinfo=UTC)
    a = build_query_panel(now=now)
    b = build_query_panel(now=now)
    assert [q.text for q in a.queries] == [q.text for q in b.queries]
    assert a.queries == b.queries  # full PanelQuery equality (intent/template/attribute too)
    assert a.fingerprint == b.fingerprint


def test_fingerprint_independent_of_generated_at():
    a = build_query_panel(now=datetime(2026, 1, 1, tzinfo=UTC))
    b = build_query_panel(now=datetime(2030, 12, 31, tzinfo=UTC))
    assert a.generated_at != b.generated_at
    assert a.fingerprint == b.fingerprint


def test_no_duplicate_query_texts():
    texts = [q.text for q in build_query_panel().queries]
    assert len(texts) == len(set(texts))


def test_every_intent_category_is_represented():
    categories = {q.intent_category for q in build_query_panel().queries}
    assert categories == set(IntentCategory)


def test_default_panel_is_not_truncated_at_default_cap():
    # 24 generated queries < DEFAULT_MAX_QUERIES (30): nothing is dropped.
    assert len(build_query_panel().queries) == len(EXPECTED_COFFEE_TEXTS)


def test_cap_truncates_deterministically_to_a_stable_prefix():
    full = build_query_panel()
    capped = build_query_panel(max_queries=10)
    assert len(capped.queries) == 10
    assert capped.queries == full.queries[:10]  # prefix-stable, deterministic
    assert build_query_panel(max_queries=10).queries == capped.queries  # repeatable


def test_price_band_attribute_is_stringified():
    panel = build_query_panel()
    price_20 = next(q for q in panel.queries if q.text == "best coffee beans under $20")
    assert price_20.intent_category is IntentCategory.PRICE
    assert price_20.template_id == "price"
    assert price_20.attribute == "20"


def test_usecase_lines_are_fixed_not_template_filled():
    panel = build_query_panel()
    usecase = {q.attribute: q for q in panel.queries if q.intent_category is IntentCategory.USECASE}
    assert usecase["whole bean"].text == "best whole bean coffee"
    assert usecase["subscription"].text == "best coffee subscription"
    assert all(q.template_id == "usecase" for q in usecase.values())
