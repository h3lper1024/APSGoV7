"""Deterministic candidate and clock boundaries; no sleeping or search execution."""

from decimal import ROUND_UP, Decimal, Inexact, localcontext
from math import inf, nextafter

import pytest

from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    SearchStopReason,
    SolverPolicy,
)


def runtime(now, **changes):
    values = dict(
        started_at_monotonic=100.0,
        search_deadline_monotonic=110.0,
        final_deadline_monotonic=112.0,
        candidate_check_limit=2,
        candidate_check_count=0,
        cancellation=None,
        clock=lambda: now[0],
    )
    return SolveRuntimeBudget(**(values | changes))


def policy(**changes):
    values = dict(
        seed=590531,
        total_time_limit_seconds=Decimal("180"),
        finalization_reserve_seconds=Decimal("10"),
        candidate_check_limit=100000,
        construction_order_key=CONSTRUCTION_ORDER_KEY,
        numeric_semantics_key=NUMERIC_SEMANTICS_KEY,
        whole_chain_pair_scan_slack_weight=Decimal("40"),
    )
    return SolverPolicy(**(values | changes))


def test_zero_budget_allows_probes_but_rejects_first_candidate_without_increment():
    budget = runtime([100.0], candidate_check_limit=0)
    assert budget.stop_reason is None and not budget.must_stop
    assert budget.permit(0) and budget.allows_search()
    assert not budget.consume_candidate_check()
    assert budget.candidate_check_count == 0
    assert budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert budget.must_stop
    assert not budget.permit(0) and not budget.allows_search()


def test_exact_limit_accepts_last_candidate_and_only_strict_excess_stops():
    budget = runtime([100.0])
    assert budget.permit() and budget.consume_candidate_check()
    assert budget.candidate_check_count == 2
    for _ in range(4):
        assert budget.permit(0) and budget.allows_search()
    assert budget.stop_reason is None and not budget.must_stop
    assert not budget.permit()
    assert budget.candidate_check_count == 2
    assert budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED


def test_bulk_check_respects_existing_count_and_rejection_is_not_partial():
    budget = runtime([100.0], candidate_check_limit=5, candidate_check_count=1)
    assert budget.permit(2)
    assert budget.candidate_check_count == 3
    assert not budget.permit(3)
    assert budget.candidate_check_count == 3
    assert not budget.permit(1)
    assert not budget.permit(0)
    assert budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED


@pytest.mark.parametrize(
    "now,allowed", ((nextafter(110.0, -inf), True), (110.0, False), (120.0, False))
)
def test_search_deadline_is_inclusive_and_refusal_never_consumes_a_candidate(now, allowed):
    budget = runtime([now])
    assert budget.permit() is allowed
    assert budget.candidate_check_count == int(allowed)
    assert budget.stop_reason is (None if allowed else SearchStopReason.SEARCH_TIME_LIMIT_REACHED)


def test_search_probes_detect_time_without_counting_and_do_not_revive_after_stop():
    now = [100.0]
    budget = runtime(now)
    assert budget.consume_candidate_check()
    now[0] = 110.0
    assert not budget.allows_search()
    now[0] = 100.0  # A changed test clock cannot revive a terminal search state.
    assert not budget.permit(0) and not budget.permit()
    assert budget.candidate_check_count == 1
    assert budget.stop_reason is SearchStopReason.SEARCH_TIME_LIMIT_REACHED


@pytest.mark.parametrize(
    "reason",
    (
        SearchStopReason.CANDIDATE_LIMIT_REACHED,
        SearchStopReason.SEARCH_TIME_LIMIT_REACHED,
        SearchStopReason.LOCAL_SEARCH_COMPLETE,
    ),
)
def test_search_stop_keeps_finalization_window_but_hard_deadline_is_terminal(reason):
    now = [111.0]
    budget = runtime(now, stop_reason=reason)
    assert budget.must_stop and not budget.allows_search()
    assert budget.allows_finalization()
    assert budget.stop_reason is reason
    now[0] = nextafter(112.0, -inf)
    assert budget.allows_finalization()
    now[0] = 112.0
    assert not budget.allows_finalization()
    assert budget.stop_reason is SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
    now[0] = 100.0
    assert not budget.allows_finalization() and not budget.allows_search()
    assert budget.candidate_check_count == 0


def test_budget_does_not_infer_natural_completion_from_successful_probes():
    budget = runtime([100.0])
    assert budget.allows_search() and budget.allows_finalization()
    assert budget.stop_reason is None and not budget.must_stop
    budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert budget.must_stop and not budget.permit()
    assert budget.allows_finalization()
    assert budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


def test_policy_uses_original_entry_time_without_reading_clock_during_construction():
    reads = []

    def clock():
        reads.append(True)
        return 180.0

    configured = policy()
    budget = SolveRuntimeBudget.from_policy(configured, 10.0, clock=clock)
    assert reads == []
    assert budget.started_at_monotonic == 10.0
    assert budget.search_deadline_monotonic == 180.0
    assert budget.final_deadline_monotonic == 190.0
    assert budget.candidate_check_limit == 100000 and budget.candidate_check_count == 0
    assert budget.cancellation is None and budget.stop_reason is None
    assert not budget.allows_search()
    assert reads == [True]
    assert budget.allows_finalization()
    assert configured == policy()


def test_policy_deadlines_do_not_depend_on_caller_decimal_context():
    configured = policy(
        total_time_limit_seconds=Decimal("180.125"),
        finalization_reserve_seconds=Decimal("10.025"),
    )
    expected = SolveRuntimeBudget.from_policy(configured, 100.25, clock=lambda: 101.0)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_UP
        ctx.traps[Inexact] = True
        actual = SolveRuntimeBudget.from_policy(configured, 100.25, clock=lambda: 101.0)
        assert actual.permit()
    assert actual.search_deadline_monotonic == expected.search_deadline_monotonic
    assert actual.final_deadline_monotonic == expected.final_deadline_monotonic
    assert actual.candidate_check_count == 1


@pytest.mark.parametrize("count", (-1, True, 1.0, Decimal(1), "1"))
def test_invalid_permit_count_fails_without_changing_state(count):
    budget = runtime([100.0])
    with pytest.raises(ValueError):
        budget.permit(count)
    assert budget.candidate_check_count == 0 and budget.stop_reason is None


@pytest.mark.parametrize(
    "changes",
    (
        {"candidate_check_limit": -1},
        {"candidate_check_limit": True},
        {"candidate_check_count": -1},
        {"candidate_check_count": 3},
        {"candidate_check_count": 1.0},
        {"stop_reason": "local_search_complete"},
        {"clock": None},
        {"started_at_monotonic": True},
        {"started_at_monotonic": Decimal(100)},
        {"started_at_monotonic": float("nan")},
        {"search_deadline_monotonic": inf},
        {"final_deadline_monotonic": -inf},
        {"search_deadline_monotonic": 100.0},
        {"final_deadline_monotonic": 110.0},
    ),
)
def test_invalid_constructor_state_is_rejected(changes):
    with pytest.raises(ValueError):
        runtime([100.0], **changes)


@pytest.mark.parametrize("now", (float("nan"), inf, -inf, True, Decimal(100), "100"))
def test_invalid_clock_values_are_not_accepted_as_permission(now):
    budget = runtime([now])
    with pytest.raises(ValueError):
        budget.permit(0)
    assert budget.candidate_check_count == 0 and budget.stop_reason is None


def test_builtin_integer_clock_values_are_valid_and_not_confused_with_boolean():
    budget = runtime([100], started_at_monotonic=100)
    assert budget.permit(0)


@pytest.mark.parametrize(
    "total,reserve,start",
    (
        ("1e309", "1", 100.0),
        ("1e308", "1e307", 1e308),
        ("180", "10", 1e20),
        ("1e-500", "1e-501", 0.0),
    ),
)
def test_policy_rejects_nonfinite_or_collapsed_float_deadlines(total, reserve, start):
    configured = policy(
        total_time_limit_seconds=Decimal(total), finalization_reserve_seconds=Decimal(reserve)
    )
    with pytest.raises(ValueError):
        SolveRuntimeBudget.from_policy(configured, start, clock=lambda: start)


def test_from_policy_requires_the_existing_typed_policy():
    with pytest.raises(ValueError):
        SolveRuntimeBudget.from_policy({}, 100.0)
