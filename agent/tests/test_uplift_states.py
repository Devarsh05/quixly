"""The uplift presentation state matrix — pure, no DB, no fixtures.

Every case here is named for the merchant-facing lie it prevents. These assertions are the ONLY
enforcement of the display invariants: the app shell's vitest harness runs ``environment: "node"``
with no DOM and no renderer, so nothing over there can test how a state is drawn.
"""

import pytest

from app.models import AgentRunStatus
from app.services.uplift import (
    EngineState,
    RunState,
    classify_engine,
    classify_run,
    deltas_reportable,
)

# ---------------------------------------------------------------------------
# Run-level state
# ---------------------------------------------------------------------------


def test_unsettled_run_is_not_reportable_even_though_its_delta_is_a_number():
    """Run 2787: ``delta = 0.0`` at 12.6h of a 168h window — it measured NOTHING.

    Keying display off the delta's value would render "0% uplift, your fixes did nothing". The
    state must be driven by ``settle_satisfied`` alone.
    """
    state = classify_run(
        status=AgentRunStatus.completed, has_rows=True, settle_satisfied=False
    )

    assert state is RunState.unsettled
    assert deltas_reportable(state) is False


def test_settled_run_with_rows_is_the_only_reportable_state():
    state = classify_run(status=AgentRunStatus.completed, has_rows=True, settle_satisfied=True)

    assert state is RunState.settled
    assert deltas_reportable(state) is True


@pytest.mark.parametrize("settle_satisfied", [True, False, None])
def test_running_outranks_settle_satisfied(settle_satisfied):
    """An in-flight run is never a result, whatever a stale row claims about settling."""
    state = classify_run(
        status=AgentRunStatus.running, has_rows=True, settle_satisfied=settle_satisfied
    )

    assert state is RunState.running
    assert deltas_reportable(state) is False


@pytest.mark.parametrize("settle_satisfied", [True, False, None])
def test_failed_outranks_settle_satisfied(settle_satisfied):
    state = classify_run(
        status=AgentRunStatus.failed, has_rows=True, settle_satisfied=settle_satisfied
    )

    assert state is RunState.failed
    assert deltas_reportable(state) is False


def test_completed_run_without_rows_is_empty_not_settled():
    state = classify_run(status=AgentRunStatus.completed, has_rows=False, settle_satisfied=None)

    assert state is RunState.empty
    assert deltas_reportable(state) is False


def test_an_unknown_status_is_empty_never_settled():
    """The allowlist, pinned.

    ``agent_runs.status`` is a plain ``String(32)`` and its model docstring says adding a status
    is a code-only change. A rule shaped "not running and not failed, therefore settled" would
    silently promote a future ``queued`` into a reportable result. Fail closed instead.
    """
    state = classify_run(status="queued", has_rows=True, settle_satisfied=True)

    assert state is RunState.empty
    assert deltas_reportable(state) is False


# ---------------------------------------------------------------------------
# Per-engine state
# ---------------------------------------------------------------------------


def test_settled_zero_delta_with_data_on_both_sides_is_no_movement():
    """A real "nothing changed" finding — distinct from every no-data state."""
    assert classify_engine(pre_rate=0.25, post_rate=0.25) is EngineState.no_movement


def test_a_null_post_rate_is_no_data_never_a_regression():
    """The fabricated-regression guard.

    ``_side_rates`` NULLs a side whose ``total_queries`` is 0. Coalescing that to 0.0 would
    compute ``0.0 - 0.5`` and tell the merchant they lost half their visibility to what was only
    a flaky engine.
    """
    state = classify_engine(pre_rate=0.5, post_rate=None)

    assert state is EngineState.no_data_post
    assert state is not EngineState.declined


def test_a_null_pre_rate_is_no_data_never_an_improvement():
    state = classify_engine(pre_rate=None, post_rate=0.5)

    assert state is EngineState.no_data_pre
    assert state is not EngineState.improved


def test_both_sides_null_is_no_data_both():
    assert classify_engine(pre_rate=None, post_rate=None) is EngineState.no_data_both


def test_a_genuine_zero_rate_is_data_not_absence():
    """0.0 over a non-zero denominator is a real measurement — the case run 2787 exercised live.

    Only NULL means "no data"; a literal 0.0 must classify as a normal comparison.
    """
    assert classify_engine(pre_rate=0.0, post_rate=0.0) is EngineState.no_movement
    assert classify_engine(pre_rate=0.0, post_rate=0.25) is EngineState.improved
    assert classify_engine(pre_rate=0.25, post_rate=0.0) is EngineState.declined


def test_direction_matches_the_sign_of_the_persisted_delta():
    """Classification derives direction from ``post - pre``, the same rule ``verifier._delta``
    used to compute the stored column — so the two can never disagree."""
    pairs = [(0.1, 0.4), (0.4, 0.1), (0.2, 0.2), (0.0, 0.0), (0.0, 1.0)]

    for pre, post in pairs:
        delta = post - pre
        state = classify_engine(pre_rate=pre, post_rate=post)

        if delta > 0:
            assert state is EngineState.improved, (pre, post)
        elif delta < 0:
            assert state is EngineState.declined, (pre, post)
        else:
            assert state is EngineState.no_movement, (pre, post)
