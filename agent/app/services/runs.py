"""Agent-run lifecycle helpers — the seam the scan route will own (step 6).

Nodes never create or complete runs; they only consume a ``run_id``. These helpers flush +
refresh (so the id and server-default timestamps are available) but do NOT commit — the caller
owns the transaction boundary.
"""

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, AgentRunStatus


async def create_agent_run(session: AsyncSession, shop_id: int, panel_id: int) -> AgentRun:
    """Insert a ``running`` agent_run and return it with id + started_at populated."""
    run = AgentRun(shop_id=shop_id, panel_id=panel_id, status=AgentRunStatus.running)
    session.add(run)
    await session.flush()  # assign run.id
    await session.refresh(run)  # read back started_at (server default)
    return run


async def complete_agent_run(
    session: AsyncSession,
    run_id: int,
    status: AgentRunStatus = AgentRunStatus.completed,
) -> AgentRun:
    """Stamp completed_at (server clock) and set the terminal status on an existing run."""
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise ValueError(f"agent_run {run_id} not found")
    run.status = status
    run.completed_at = func.now()
    await session.flush()
    await session.refresh(run)  # read back completed_at
    return run
