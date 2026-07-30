"""Presentation state for a verification run (Phase 4 — the uplift chart).

**PURE.** No DB, no I/O, no Shopify, no settings. It classifies values the Verifier already
persisted so the app shell can render them without deriving anything. That split is deliberate
twice over: CLAUDE.md keeps business logic out of the thin React Router shell, and the shell's
vitest harness runs ``environment: "node"`` with no DOM and no renderer — component states are
not unit-testable over there, so the invariants below would be enforced by nothing. Here they are
enforced by ``tests/test_uplift_states.py``, which needs no Postgres.

Three merchant-facing lies this module exists to prevent:

1. **An unsettled run rendered as a result.** Run 2787 (the only real verification on the dev
   store) holds ``delta = 0.0`` with ``settle_satisfied = false`` — at 12.6h of a 168h window no
   engine had re-crawled, so it measured *nothing*. Rendered as a number it reads "0% uplift,
   your fixes did nothing." Display state is therefore keyed on :class:`RunState`, **never** on
   the delta's value.
2. **A no-data engine rendered as a regression.** ``verifier._side_rates`` NULLs a side whose
   ``total_queries`` is 0 or NULL, and that NULL propagates to ``delta``. Coalescing it to 0.0
   would compute ``0.0 - pre_rate`` and show a **fabricated** decline. NULL gets its own
   ``no_data_*`` state and is never a number.
3. **A genuine zero confused with missing data.** A settled ``delta = 0.0`` with real queries on
   both sides is a true "no movement" finding. It is a *different* state from (2), and collapsing
   them would either hide a real result or invent one.
"""

import enum

from app.models import AgentRunStatus


class RunState(enum.StrEnum):
    """How much of a verification run may be shown as a result."""

    running = "running"
    """The measurement is still in flight; it has produced no rows yet."""

    failed = "failed"
    """The job aborted (a panel/fingerprint mismatch, or an engine-side failure)."""

    empty = "empty"
    """Terminal but with nothing to show — no rows, or a run status we do not recognise."""

    unsettled = "unsettled"
    """Rows exist, but the settle window was not met: a reading, NOT a result."""

    settled = "settled"
    """The only state whose deltas may be presented as a finding."""


class EngineState(enum.StrEnum):
    """What one engine's before/after pair actually says."""

    no_data_both = "no_data_both"
    no_data_pre = "no_data_pre"
    no_data_post = "no_data_post"
    no_movement = "no_movement"
    improved = "improved"
    declined = "declined"


def classify_run(
    *, status: str, has_rows: bool, settle_satisfied: bool | None
) -> RunState:
    """Classify a run for display.

    **This is an allowlist on ``completed``, not a fall-through, and that is load-bearing.**
    ``agent_runs.status`` is a plain ``String(32)`` — not a DB enum — and its model docstring
    states outright that "adding a status is a code-only change". A rule shaped as *"not running
    and not failed, therefore settled"* would silently promote a future fourth status (``queued``,
    say) into a reportable result. Anything we do not recognise lands in :attr:`RunState.empty`,
    which reports nothing. Fail closed.
    """
    if status == AgentRunStatus.running:
        return RunState.running
    if status == AgentRunStatus.failed:
        return RunState.failed
    if status != AgentRunStatus.completed:
        return RunState.empty
    if not has_rows:
        return RunState.empty
    if not settle_satisfied:
        return RunState.unsettled
    return RunState.settled


def deltas_reportable(state: RunState) -> bool:
    """Whether the shell may render a delta figure at all.

    The single flag invariant 1 hangs on. It is an explicit server-computed field rather than a
    comparison in JSX precisely so a Python test can assert it — the shell cannot test its own
    rendering.
    """
    return state is RunState.settled


def classify_engine(*, pre_rate: float | None, post_rate: float | None) -> EngineState:
    """Classify one engine's before/after pair.

    Nullness is checked **first**: a missing side is "no data", never a direction. Only once both
    sides are known does sign matter.

    Direction is derived from ``post_rate - pre_rate`` rather than read off the persisted
    ``delta`` column, so classification cannot disagree with the stored value — it applies the
    same rule ``graph.verifier._delta`` used to compute it. The stored ``delta`` is still what
    gets displayed; this only decides *how* to frame it.
    """
    if pre_rate is None and post_rate is None:
        return EngineState.no_data_both
    if pre_rate is None:
        return EngineState.no_data_pre
    if post_rate is None:
        return EngineState.no_data_post
    if post_rate > pre_rate:
        return EngineState.improved
    if post_rate < pre_rate:
        return EngineState.declined
    return EngineState.no_movement
