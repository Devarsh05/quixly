"""run_scan_task end-to-end over FAKE engine + extractor clients (no live API).

Real Postgres via the transaction-scoped ``db`` fixture. ``SessionLocal`` is pointed at that
session (so the task's commits land inside the rolled-back test transaction), and the engine /
extractor client classes the task constructs are replaced with canned fakes. The point of these
tests is the LOAD-BEARING COMMIT: the run must be persisted as completed / failed, never left
``running``.
"""

import pytest
from sqlalchemy import select

from app.graph.interrogator import IntentCategory, PanelQuery, QueryPanel
from app.jobs import scan as scan_job
from app.models import AgentRun, AgentRunStatus, EngineRun, ShareOfModel, Shop, ShopStatus
from app.services.extractor_llm import ExtractedBrand, ExtractedBrands
from app.services.panels import upsert_panel
from app.services.perplexity import EngineAnswer
from app.services.runs import create_agent_run

SHOP = "scan-task-test.myshopify.com"

A0 = "We recommend Northwind Coffee and Blue Bottle."
A1 = "Try Blue Bottle."


def _answer(text: str) -> EngineAnswer:
    return EngineAnswer(
        answer_text=text,
        citations=[],
        search_results=[],
        usage=None,
        raw={"choices": [{"message": {"content": text}}], "citations": [], "search_results": []},
    )


class FakeEngineClient:
    """Canned engine: one answer per query text."""

    engine = "perplexity"

    def __init__(self, answers: dict[str, EngineAnswer]):
        self._answers = answers

    async def run_query(self, query: str) -> EngineAnswer:
        return self._answers[query]


class FakeExtractorClient:
    """Canned extractor: brands per answer text (empty for anything unseen)."""

    def __init__(self, answers: dict[str, ExtractedBrands]):
        self._answers = answers

    async def extract(self, answer_text: str) -> ExtractedBrands:
        return self._answers.get(answer_text, ExtractedBrands(brands=[]))


def _fakes() -> tuple[FakeEngineClient, FakeExtractorClient]:
    engine = FakeEngineClient({"q0": _answer(A0), "q1": _answer(A1)})
    extractor = FakeExtractorClient(
        {
            A0: ExtractedBrands(
                brands=[
                    ExtractedBrand(rank=1, brand="Northwind Coffee", verbatim="Northwind Coffee"),
                    ExtractedBrand(rank=2, brand="Blue Bottle", verbatim="Blue Bottle"),
                ]
            ),
            A1: ExtractedBrands(
                brands=[ExtractedBrand(rank=1, brand="Blue Bottle", verbatim="Blue Bottle")]
            ),
        }
    )
    return engine, extractor


@pytest.fixture
async def shop(db):
    shop = Shop(shop_domain=SHOP, status=ShopStatus.active)
    db.add(shop)
    await db.commit()
    await db.refresh(shop)
    return shop


@pytest.fixture
def patched_task(db, monkeypatch):
    """Point the task at the test session and install fake engine/extractor clients."""

    class SessionCtx:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(scan_job, "SessionLocal", lambda: SessionCtx())

    def install(engine_client, extractor_client):
        monkeypatch.setattr(scan_job, "PerplexitySonarClient", lambda *a, **k: engine_client)
        monkeypatch.setattr(scan_job, "OpenAIExtractorClient", lambda *a, **k: extractor_client)

    return install


async def _seed_run(db, shop_id: int, *, fingerprint: str = "fp-scan-task") -> AgentRun:
    """Persist a 2-query panel + a running agent_run, as the scan route would before enqueue."""
    panel = QueryPanel(
        category="coffee",
        queries=[
            PanelQuery(
                text="q0", intent_category=IntentCategory.ROAST, template_id="roast",
                attribute="light",
            ),
            PanelQuery(
                text="q1", intent_category=IntentCategory.ORIGIN, template_id="origin",
                attribute="Ethiopian",
            ),
        ],
        fingerprint=fingerprint,
    )
    panel_id = await upsert_panel(db, panel, shop_id)
    run = await create_agent_run(db, shop_id, panel_id)
    await db.commit()
    return run


async def test_scan_task_completes_and_writes_share_of_model(db, shop, patched_task):
    run = await _seed_run(db, shop.id)
    patched_task(*_fakes())

    await scan_job.run_scan_task({}, run.id)

    # Re-read fresh: the run is COMMITTED as completed (the point of the test).
    fresh = (
        await db.execute(
            select(AgentRun).where(AgentRun.id == run.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert fresh.status == AgentRunStatus.completed
    assert fresh.completed_at is not None

    (row,) = (
        await db.execute(select(ShareOfModel).where(ShareOfModel.run_id == run.id))
    ).scalars().all()
    assert row.engine == "perplexity"
    assert row.total_queries == 2
    assert row.our_mentions == 1  # Northwind Coffee mentioned in q0 only
    assert row.our_rate == 0.5
    assert row.competitor_rates_json["Blue Bottle"] == {"mention_rate": 1.0, "mentions": 2}


async def test_scan_task_failure_marks_run_failed_and_commits(db, shop, patched_task, monkeypatch):
    run = await _seed_run(db, shop.id, fingerprint="fp-fail")
    patched_task(*_fakes())

    async def boom(*args, **kwargs):
        raise RuntimeError("aggregation failed")

    monkeypatch.setattr(scan_job, "run_share_of_model", boom)

    with pytest.raises(RuntimeError, match="aggregation failed"):
        await scan_job.run_scan_task({}, run.id)

    fresh = (
        await db.execute(
            select(AgentRun).where(AgentRun.id == run.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert fresh.status == AgentRunStatus.failed  # committed, not left running
    assert fresh.completed_at is not None

    # No aggregate row for the failed run...
    som = (
        await db.execute(select(ShareOfModel).where(ShareOfModel.run_id == run.id))
    ).scalars().all()
    assert som == []
    # ...but the engine_runs the earlier nodes committed survive (partial data preserved).
    ers = (
        await db.execute(select(EngineRun).where(EngineRun.run_id == run.id))
    ).scalars().all()
    assert len(ers) == 2


async def test_scan_task_touches_only_its_own_run(db, shop, patched_task):
    # Run B already holds a share_of_model row from a prior scan; running the task for run A must
    # not touch it — identity is run_id.
    run_a = await _seed_run(db, shop.id, fingerprint="fp-a")
    run_b = await _seed_run(db, shop.id, fingerprint="fp-b")

    db.add(
        ShareOfModel(
            run_id=run_b.id, shop_id=shop.id, engine="perplexity", period="2026-01-01",
            our_rate=0.9, our_mentions=9, total_queries=10, competitor_rates_json={},
        )
    )
    await db.commit()

    patched_task(*_fakes())
    await scan_job.run_scan_task({}, run_a.id)

    # Run A produced its own row...
    row_a = (
        await db.execute(
            select(ShareOfModel)
            .where(ShareOfModel.run_id == run_a.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row_a.our_rate == 0.5
    # ...and run B's pre-existing row is untouched.
    row_b = (
        await db.execute(
            select(ShareOfModel)
            .where(ShareOfModel.run_id == run_b.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row_b.our_rate == 0.9
    assert row_b.our_mentions == 9
