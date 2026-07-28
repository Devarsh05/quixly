"""run_verify_task end-to-end over FAKE engine + extractor clients (no live API).

Mirrors ``test_scan_task.py``: real Postgres via the transaction-scoped ``db`` fixture,
``SessionLocal`` pointed at that session so the task's commits land inside the rolled-back test
transaction, and the client classes the task constructs replaced with canned fakes.

Two things are being proven beyond "it runs":

* **Pipeline REUSE, not a fork.** The job must drive the same ``run_scan_pipeline`` as
  ``jobs.scan`` — verified by monkeypatching a node in the *scan* module and watching the verify
  job fail through it.
* **The load-bearing commit.** The run must be persisted as completed / failed, never left
  ``running`` — including on the abort path, where the scan succeeded but the delta could not be
  trusted.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.graph.interrogator import IntentCategory, PanelQuery, QueryPanel, _fingerprint
from app.jobs import scan as scan_job
from app.jobs import verify as verify_job
from app.models import (
    AgentRun,
    AgentRunStatus,
    EngineRun,
    FixType,
    ShareOfModel,
    Shop,
    ShopStatus,
    Verification,
)
from app.services.extractor_llm import ExtractedBrand, ExtractedBrands
from app.services.panels import upsert_panel
from app.services.perplexity import EngineAnswer
from app.services.runs import create_agent_run

SHOP = "verify-job-test.myshopify.com"

A0 = "We recommend Northwind Coffee and Blue Bottle."
A1 = "Try Blue Bottle."

PUBLISHED_AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
MEASURED = [
    {
        "fix_id": 9702,
        "product_id": 114,
        "type": FixType.description.value,
        "target": "body_html",
        "published_at": PUBLISHED_AT.isoformat(),
    }
]


def _answer(text: str) -> EngineAnswer:
    return EngineAnswer(
        answer_text=text,
        citations=[],
        search_results=[],
        usage=None,
        raw={"choices": [{"message": {"content": text}}], "citations": [], "search_results": []},
    )


class FakeEngineClient:
    engine = "perplexity"

    def __init__(self, answers: dict[str, EngineAnswer]):
        self._answers = answers

    async def run_query(self, query: str) -> EngineAnswer:
        return self._answers[query]


class FakeExtractorClient:
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
    """Point the verify task at the test session and install fake engine/extractor clients."""

    class SessionCtx:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(verify_job, "SessionLocal", lambda: SessionCtx())

    def install(engine_client, extractor_client):
        monkeypatch.setattr(verify_job, "PerplexitySonarClient", lambda *a, **k: engine_client)
        monkeypatch.setattr(verify_job, "OpenAIExtractorClient", lambda *a, **k: extractor_client)

    return install


async def _seed_panel(db, shop_id: int) -> int:
    """A 2-query panel carrying its REAL content hash, so the fingerprint recheck is exercised."""
    queries = [
        PanelQuery(
            text="q0", intent_category=IntentCategory.ROAST, template_id="roast", attribute="light"
        ),
        PanelQuery(
            text="q1", intent_category=IntentCategory.ORIGIN, template_id="origin",
            attribute="Ethiopian",
        ),
    ]
    panel = QueryPanel(
        category="coffee",
        queries=queries,
        fingerprint=_fingerprint("coffee", queries),
    )
    panel_id = await upsert_panel(db, panel, shop_id)
    await db.commit()
    return panel_id


async def _seed_baseline(db, shop_id: int, panel_id: int, *, our_rate: float | None) -> AgentRun:
    run = AgentRun(
        shop_id=shop_id,
        panel_id=panel_id,
        status=AgentRunStatus.completed,
        started_at=PUBLISHED_AT - timedelta(days=7),
        completed_at=PUBLISHED_AT - timedelta(days=7),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    db.add(
        ShareOfModel(
            run_id=run.id, shop_id=shop_id, engine="perplexity", period="2026-06-24",
            our_rate=our_rate, our_mentions=0, total_queries=2, competitor_rates_json={},
        )
    )
    await db.commit()
    return run


async def test_verify_task_runs_the_scan_and_records_the_delta(db, shop, patched_task):
    panel_id = await _seed_panel(db, shop.id)
    baseline = await _seed_baseline(db, shop.id, panel_id, our_rate=0.0)
    post = await create_agent_run(db, shop.id, panel_id)
    await db.commit()
    patched_task(*_fakes())

    # force=False deliberately: PUBLISHED_AT is weeks old, so the settle window is genuinely met
    # and no label is needed. The forced path is covered in test_verifier.py.
    report = await verify_job.run_verify_task({}, post.id, baseline.id, MEASURED, False)

    assert report.engines[0].pre_rate == 0.0
    assert report.engines[0].post_rate == 0.5  # Northwind mentioned in q0 of two queries
    assert report.engines[0].delta == 0.5

    fresh = (
        await db.execute(
            select(AgentRun).where(AgentRun.id == post.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert fresh.status == AgentRunStatus.completed  # committed, not left running
    assert fresh.completed_at is not None

    # It IS a scan run: it produced its own engine_runs + share_of_model under its own run_id.
    engine_rows = (
        await db.execute(select(EngineRun).where(EngineRun.run_id == post.id))
    ).scalars().all()
    assert len(engine_rows) == 2
    (aggregate,) = (
        await db.execute(select(ShareOfModel).where(ShareOfModel.run_id == post.id))
    ).scalars().all()
    assert aggregate.our_rate == 0.5

    (verification,) = (
        await db.execute(select(Verification).where(Verification.run_id == post.id))
    ).scalars().all()
    assert verification.delta == 0.5
    assert verification.baseline_run_id == baseline.id
    assert verification.measured_fixes_json[0]["fix_id"] == 9702
    assert verification.settle_satisfied is True  # weeks past the window; no force needed
    assert verification.settle_hours > 168.0


async def test_verify_task_reuses_the_scan_pipeline(db, shop, patched_task, monkeypatch):
    """Patching a node in ``jobs.scan`` must break the verify job — proof it is not a fork."""
    panel_id = await _seed_panel(db, shop.id)
    baseline = await _seed_baseline(db, shop.id, panel_id, our_rate=0.0)
    post = await create_agent_run(db, shop.id, panel_id)
    await db.commit()
    patched_task(*_fakes())

    async def boom(*args, **kwargs):
        raise RuntimeError("aggregation failed")

    monkeypatch.setattr(scan_job, "run_share_of_model", boom)

    with pytest.raises(RuntimeError, match="aggregation failed"):
        await verify_job.run_verify_task({}, post.id, baseline.id, MEASURED, True)

    fresh = (
        await db.execute(
            select(AgentRun).where(AgentRun.id == post.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert fresh.status == AgentRunStatus.failed  # committed, not left running

    assert (
        await db.execute(select(Verification).where(Verification.run_id == post.id))
    ).scalars().all() == []


async def test_verify_task_abort_marks_failed_and_records_no_delta(db, shop, patched_task):
    """Unsettled without force: the scan's own rows survive, but no delta is recorded."""
    panel_id = await _seed_panel(db, shop.id)
    baseline = await _seed_baseline(db, shop.id, panel_id, our_rate=0.0)
    post = await create_agent_run(db, shop.id, panel_id)
    await db.commit()
    patched_task(*_fakes())

    recent = [{**MEASURED[0], "published_at": datetime.now(UTC).isoformat()}]

    with pytest.raises(verify_job.VerificationAborted):
        await verify_job.run_verify_task({}, post.id, baseline.id, recent, False)

    fresh = (
        await db.execute(
            select(AgentRun).where(AgentRun.id == post.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert fresh.status == AgentRunStatus.failed

    assert (
        await db.execute(select(Verification).where(Verification.run_id == post.id))
    ).scalars().all() == []
    # The scan half committed and is preserved — only the delta was refused.
    assert len(
        (await db.execute(select(EngineRun).where(EngineRun.run_id == post.id))).scalars().all()
    ) == 2


async def test_verify_task_touches_only_its_own_run(db, shop, patched_task):
    panel_id = await _seed_panel(db, shop.id)
    baseline = await _seed_baseline(db, shop.id, panel_id, our_rate=0.0)
    post = await create_agent_run(db, shop.id, panel_id)
    other = await create_agent_run(db, shop.id, panel_id)
    await db.commit()

    # A pre-existing verification on another run, seeded so the run-scoping is falsifiable.
    db.add(
        Verification(
            run_id=other.id, baseline_run_id=baseline.id, shop_id=shop.id, engine="perplexity",
            panel_id=panel_id, panel_fingerprint="fp-other", pre_rate=0.9, post_rate=0.9, delta=0.0,
            competitor_deltas_json={}, measured_fixes_json=[], measured_fix_count=0,
            published_at_max=PUBLISHED_AT, settle_hours=999.0, settle_satisfied=True,
        )
    )
    await db.commit()

    patched_task(*_fakes())
    await verify_job.run_verify_task({}, post.id, baseline.id, MEASURED, True)

    untouched = (
        await db.execute(
            select(Verification)
            .where(Verification.run_id == other.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert untouched.delta == 0.0
    assert untouched.panel_fingerprint == "fp-other"
