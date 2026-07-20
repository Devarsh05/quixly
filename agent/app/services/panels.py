"""Query-panel persistence — the single upsert/load path for ``query_panels``.

``build_query_panel`` (the Interrogator) produces an in-memory ``QueryPanel``; this module is
where it meets the database. ``upsert_panel`` is the ONE writer of a ``(shop_id, fingerprint)``
row — used by both the scan route (which needs a ``panel_id`` before it can create the agent_run)
and ``run_engine`` (which re-upserts idempotently on every run). Keeping a single writer avoids
two divergent upserts of the same row.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.interrogator import PanelQuery, QueryPanel
from app.models import QueryPanel as QueryPanelRow


async def upsert_panel(session: AsyncSession, panel: QueryPanel, shop_id: int) -> int:
    """Insert the panel, or reuse the row for ``(shop_id, fingerprint)``. Returns its id."""
    statement = insert(QueryPanelRow).values(
        shop_id=shop_id,
        category=panel.category,
        queries_json=[q.model_dump() for q in panel.queries],
        fingerprint=panel.fingerprint,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[QueryPanelRow.shop_id, QueryPanelRow.fingerprint],
        # Touch category so RETURNING yields the id on conflict too. queries_json is a pure
        # function of the fingerprint, so this is a no-op refresh, not a semantic change.
        set_={"category": statement.excluded.category},
    ).returning(QueryPanelRow.id)

    return (await session.execute(statement)).scalar_one()


async def load_query_panel(session: AsyncSession, panel_id: int) -> QueryPanel:
    """Reconstruct the in-memory ``QueryPanel`` from its persisted row.

    The committed row is a scan's source of truth, so a task rebuilds the panel from it rather
    than re-deriving via ``build_query_panel`` — ``run_engine`` then re-upserts the same
    ``(shop_id, fingerprint)`` and gets the same id back. Raises ``NoResultFound`` on a bad id.
    """
    row = (
        await session.execute(select(QueryPanelRow).where(QueryPanelRow.id == panel_id))
    ).scalar_one()
    return QueryPanel(
        category=row.category,
        queries=[PanelQuery(**q) for q in row.queries_json],
        fingerprint=row.fingerprint,
    )
