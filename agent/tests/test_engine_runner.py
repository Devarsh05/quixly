"""EngineRunner: fan-out + persistence.

Uses the real Postgres-backed ``db`` fixture (transaction rolled back per test), not empty-table
mocks. Engine responses are faked at the ``EngineClient`` boundary, except the retry test which
drives the real ``PerplexitySonarClient`` through a mocked httpx transport.
"""

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.graph.engine_runner import _map_sources, run_engine
from app.graph.interrogator import build_query_panel
from app.models import EngineRun, Shop, ShopStatus
from app.models import QueryPanel as QueryPanelRow
from app.services.perplexity import (
    PERPLEXITY_ENDPOINT,
    EngineAnswer,
    EngineError,
    PerplexitySonarClient,
    SearchResult,
)

SHOP = "engine-test.myshopify.com"


def _answer(text="answer", citations=None, search_results=None) -> EngineAnswer:
    citations = citations or []
    search_results = search_results or []
    return EngineAnswer(
        answer_text=text,
        citations=citations,
        search_results=[SearchResult(**sr) for sr in search_results],
        usage=None,
        raw={
            "choices": [{"message": {"content": text}}],
            "citations": citations,
            "search_results": search_results,
        },
    )


class FakeEngineClient:
    """Canned engine: returns a per-query answer, or raises for queries in ``fail``."""

    engine = "perplexity"

    def __init__(
        self, answers: dict[str, EngineAnswer] | None = None, fail: set[str] | None = None
    ):
        self._answers = answers or {}
        self._fail = fail or set()

    async def run_query(self, query: str) -> EngineAnswer:
        if query in self._fail:
            raise EngineError(f"boom for {query}")
        return self._answers.get(query, _answer())


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


# --- _map_sources (pure) ---------------------------------------------------------------------


def test_map_sources_maps_search_results_in_order():
    answer = _answer(
        citations=["https://z.example"],  # ignored when search_results is present
        search_results=[
            {"title": "A", "url": "https://a.example", "snippet": "sa"},
            {"title": "B", "url": "https://b.example", "snippet": "sb"},
        ],
    )
    mapped = _map_sources(answer)
    assert [s["url"] for s in mapped] == ["https://a.example", "https://b.example"]
    assert mapped[0] == {"title": "A", "url": "https://a.example", "snippet": "sa"}
    # No None-valued keys leak through.
    assert "date" not in mapped[0]


def test_map_sources_falls_back_to_citations_when_no_search_results():
    answer = _answer(citations=["https://a.example", "https://b.example"], search_results=[])
    assert _map_sources(answer) == [
        {"url": "https://a.example"},
        {"url": "https://b.example"},
    ]


# --- fan-out + persistence -------------------------------------------------------------------


async def test_fan_out_writes_one_row_per_query_with_sources_and_null_extractor_columns(db, shop):
    panel = build_query_panel(max_queries=3)
    client = FakeEngineClient(
        answers={
            q.text: _answer(
                text=f"a:{q.text}",
                search_results=[{"title": "T", "url": "https://x.example", "snippet": "s"}],
            )
            for q in panel.queries
        }
    )

    report = await run_engine(db, panel, shop.id, client)

    assert report.engine == "perplexity"
    assert len(report.outcomes) == 3
    assert all(o.ok for o in report.outcomes)

    # Exactly one panel row, matching the step-1 fingerprint.
    panels = (
        await db.execute(select(QueryPanelRow).where(QueryPanelRow.shop_id == shop.id))
    ).scalars().all()
    assert len(panels) == 1
    assert panels[0].id == report.panel_id
    assert panels[0].fingerprint == panel.fingerprint
    assert panels[0].category == "coffee"
    assert len(panels[0].queries_json) == 3

    rows = (
        await db.execute(select(EngineRun).where(EngineRun.panel_id == report.panel_id))
    ).scalars().all()
    assert len(rows) == 3
    query_texts = {q.text for q in panel.queries}
    for row in rows:
        assert row.engine == "perplexity"
        assert row.query in query_texts
        assert row.response_raw["choices"][0]["message"]["content"].startswith("a:")
        assert row.cited_sources_json == [
            {"title": "T", "url": "https://x.example", "snippet": "s"}
        ]
        # The Extractor fills these in step 3 — never EngineRunner.
        assert row.cited_brands_json is None
        assert row.our_mentions_json is None


async def test_identical_rerun_reuses_the_panel_row_and_accumulates_engine_runs(db, shop):
    panel = build_query_panel(max_queries=3)
    client = FakeEngineClient()

    first = await run_engine(db, panel, shop.id, client)
    second = await run_engine(db, panel, shop.id, client)

    # The fingerprint is the natural key: same panel row reused, not duplicated.
    assert second.panel_id == first.panel_id
    panel_count = await db.scalar(
        select(func.count()).select_from(QueryPanelRow).where(QueryPanelRow.shop_id == shop.id)
    )
    assert panel_count == 1

    # engine_runs always INSERT, so runs accumulate period over period.
    run_count = await db.scalar(
        select(func.count()).select_from(EngineRun).where(EngineRun.panel_id == first.panel_id)
    )
    assert run_count == 6


async def test_one_failing_query_does_not_sink_the_batch(db, shop):
    panel = build_query_panel(max_queries=3)
    doomed = panel.queries[1].text
    client = FakeEngineClient(fail={doomed})

    report = await run_engine(db, panel, shop.id, client)

    # All three persisted; exactly one recorded as a failure.
    assert len(report.outcomes) == 3
    failed = [o for o in report.outcomes if not o.ok]
    assert len(failed) == 1
    assert failed[0].query == doomed
    assert failed[0].answer is None
    assert failed[0].error

    row = (
        await db.execute(select(EngineRun).where(EngineRun.query == doomed))
    ).scalar_one()
    assert row.response_raw == {"error": failed[0].error}
    assert row.cited_sources_json is None

    total = await db.scalar(
        select(func.count()).select_from(EngineRun).where(EngineRun.panel_id == report.panel_id)
    )
    assert total == 3


# --- real client retry/backoff ---------------------------------------------------------------


@respx.mock
async def test_perplexity_client_retries_429_then_succeeds(db, shop, monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
    from app.settings import get_settings

    get_settings.cache_clear()

    # No real sleeping between attempts.
    async def _no_sleep(_attempt):
        return None

    monkeypatch.setattr(PerplexitySonarClient, "_backoff", staticmethod(_no_sleep))

    body = {
        "choices": [{"message": {"content": "sonar says hi"}}],
        "citations": ["https://a.example"],
        "search_results": [{"title": "A", "url": "https://a.example", "snippet": "s"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    route = respx.post(PERPLEXITY_ENDPOINT).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=body)]
    )

    client = PerplexitySonarClient()
    panel = build_query_panel(max_queries=1)
    report = await run_engine(db, panel, shop.id, client)

    assert route.call_count == 2
    assert len(report.outcomes) == 1
    outcome = report.outcomes[0]
    assert outcome.ok
    assert outcome.answer.answer_text == "sonar says hi"
    assert outcome.answer.usage.total_tokens == 30

    row = (
        await db.execute(select(EngineRun).where(EngineRun.panel_id == report.panel_id))
    ).scalar_one()
    assert row.cited_sources_json == [{"title": "A", "url": "https://a.example", "snippet": "s"}]
