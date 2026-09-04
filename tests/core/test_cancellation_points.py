"""Cancellation protocol checks; no search, audit or release pipeline is implemented here.

The public release prohibition is independently covered by the existing
tests/api/test_result_status_matrix.py terminal-reason cases.
"""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from apsgo_scheduler.core import evaluation
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    SearchStopReason,
    SolverPolicy,
)


class Clock:
    def __init__(self, value=100.0):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class Token:
    def __init__(self, value=False):
        self.value = value
        self.calls = 0

    def is_cancelled(self):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def make_budget(limit=3):
    policy = SolverPolicy(
        1,
        Decimal(10),
        Decimal(2),
        limit,
        CONSTRUCTION_ORDER_KEY,
        NUMERIC_SEMANTICS_KEY,
        Decimal(40),
    )
    clock, token = Clock(), Token()
    current = SolveRuntimeBudget.from_policy(policy, 100.0, token, clock=clock)
    return current, clock, token


@pytest.mark.parametrize("method", ["allows_search", "consume_candidate_check"])
def test_cancellation_is_checked_before_expired_search_time(method):
    current, clock, token = make_budget()
    token.value = True
    clock.value = 120.0
    clock_calls = clock.calls
    assert getattr(current, method)() is False
    assert current.stop_reason is SearchStopReason.USER_CANCELLED
    assert current.candidate_check_count == 0
    assert clock.calls == clock_calls
    assert current.must_stop is True


def test_exceeding_candidate_limit_precedes_cancellation_and_time_checks():
    current, clock, token = make_budget(limit=0)
    token.value = True
    clock.value = 120.0
    token_calls, clock_calls = token.calls, clock.calls
    assert current.permit(1) is False
    assert current.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert current.candidate_check_count == 0
    assert (token.calls, clock.calls) == (token_calls, clock_calls)
    # Search's frozen ordering does not authorize publication after a later cancellation.
    assert current.allows_finalization() is False
    assert current.stop_reason is SearchStopReason.USER_CANCELLED


def test_permit_zero_still_observes_cancel_when_candidate_limit_is_zero():
    current, _, token = make_budget(limit=0)
    token.value = True
    assert current.permit(0) is False
    assert current.stop_reason is SearchStopReason.USER_CANCELLED
    assert current.candidate_check_count == 0


def test_caller_safe_point_between_candidate_and_evaluation_keeps_consumed_check(monkeypatch):
    current, _, token = make_budget(limit=1)
    evaluator = Mock(side_effect=AssertionError("evaluation must not run after cancellation"))
    monkeypatch.setattr(evaluation, "evaluate_plan", evaluator)
    assert current.consume_candidate_check() is True
    assert current.candidate_check_count == 1
    # A later neighborhood must make this zero-cost check after constructing its candidate.
    token.value = True
    if current.allows_search():
        evaluation.evaluate_plan(None, None, None)
    evaluator.assert_not_called()
    assert current.candidate_check_count == 1
    assert current.stop_reason is SearchStopReason.USER_CANCELLED
    assert current.allows_finalization() is False


@pytest.mark.parametrize(
    "initial_reason",
    [
        SearchStopReason.CANDIDATE_LIMIT_REACHED,
        SearchStopReason.SEARCH_TIME_LIMIT_REACHED,
        SearchStopReason.LOCAL_SEARCH_COMPLETE,
    ],
)
@pytest.mark.parametrize("new_stop", ["cancellation", "hard_deadline"])
def test_finalization_detects_new_fatal_stop_after_search_has_stopped(initial_reason, new_stop):
    current, clock, token = make_budget()
    current.stop_reason = initial_reason
    assert current.allows_search() is False
    assert current.allows_finalization() is True
    assert current.stop_reason is initial_reason
    if new_stop == "cancellation":
        token.value = True
        expected = SearchStopReason.USER_CANCELLED
    else:
        clock.value = current.final_deadline_monotonic
        expected = SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
    assert current.allows_finalization() is False
    assert current.stop_reason is expected
    assert current.candidate_check_count == 0
    assert current.must_stop is True


def test_finalization_checks_cancellation_before_simultaneous_hard_deadline():
    current, clock, token = make_budget()
    current.stop_reason = SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    token.value = True
    clock.value = current.final_deadline_monotonic
    before = clock.calls
    assert current.allows_finalization() is False
    assert current.stop_reason is SearchStopReason.USER_CANCELLED
    assert clock.calls == before


def test_each_finalization_phase_rechecks_time_without_charging_candidate_budget():
    current, clock, _ = make_budget()
    clock.value = current.search_deadline_monotonic
    assert current.allows_search() is False
    assert current.stop_reason is SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    # These are phase entry checks, not implementations of the three audit/output stages.
    for moment in (108.0, 109.0, 109.999):
        clock.value = moment
        assert current.allows_finalization() is True
        assert current.stop_reason is SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    clock.value = 110.0
    assert current.allows_finalization() is False
    assert current.stop_reason is SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
    assert current.candidate_check_count == 0


@pytest.mark.parametrize("reason", tuple(SearchStopReason))
def test_no_search_terminal_state_is_revived_by_clock_or_token_reset(reason):
    current, clock, token = make_budget()
    current.stop_reason = reason
    clock.value, token.value = 100.0, False
    before = token.calls, clock.calls
    assert current.permit(0) is False
    assert current.permit(1) is False
    assert current.consume_candidate_check() is False
    assert current.allows_search() is False
    assert current.stop_reason is reason
    assert current.candidate_check_count == 0
    assert current.must_stop is True
    assert (token.calls, clock.calls) == before


@pytest.mark.parametrize(
    "reason",
    [
        SearchStopReason.USER_CANCELLED,
        SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
        SearchStopReason.INPUT_INVALID,
        SearchStopReason.SYSTEM_ERROR,
    ],
)
def test_fatal_finalization_state_never_revives_or_replaces_its_reason(reason):
    current, clock, token = make_budget()
    current.stop_reason = reason
    for clock_value, cancelled in ((100.0, False), (120.0, True)):
        clock.value, token.value = clock_value, cancelled
        before = token.calls, clock.calls
        assert current.allows_finalization() is False
        assert current.stop_reason is reason
        assert (token.calls, clock.calls) == before


@pytest.mark.parametrize("value", [None, 0, 1, "", "false", Decimal(0), object()])
@pytest.mark.parametrize("phase", ["search", "finalization"])
def test_non_boolean_cancellation_is_an_input_error_not_a_normal_stop(value, phase):
    current, _, token = make_budget()
    token.value = value
    initial_reason = None
    if phase == "finalization":
        initial_reason = SearchStopReason.CANDIDATE_LIMIT_REACHED
        current.stop_reason = initial_reason
    with pytest.raises(ValueError):
        if phase == "search":
            current.consume_candidate_check()
        else:
            current.allows_finalization()
    assert current.stop_reason is initial_reason
    assert current.candidate_check_count == 0


@pytest.mark.parametrize("failure", [RuntimeError("token failed"), ValueError("token failed")])
@pytest.mark.parametrize("phase", ["search", "finalization"])
def test_cancellation_provider_exceptions_propagate_without_fake_stop_reason(failure, phase):
    current, clock, token = make_budget()
    token.value = failure
    clock_calls = clock.calls
    check = current.consume_candidate_check if phase == "search" else current.allows_finalization
    with pytest.raises(type(failure)) as caught:
        check()
    assert caught.value is failure
    assert current.stop_reason is None
    assert current.candidate_check_count == 0
    assert clock.calls == clock_calls


def test_cancellation_priority_does_not_evaluate_a_failing_clock():
    current, clock, token = make_budget()
    clock.value = RuntimeError("unreachable clock")
    token.value = True
    assert current.allows_search() is False
    assert current.stop_reason is SearchStopReason.USER_CANCELLED


def test_missing_token_is_supported_without_becoming_natural_completion():
    current, _, _ = make_budget()
    current.cancellation = None
    assert current.allows_search() is True
    assert current.allows_finalization() is True
    assert current.stop_reason is None
    assert current.must_stop is False
    assert current.candidate_check_count == 0


def test_reading_must_stop_never_polls_or_invents_a_completion_reason():
    current, clock, token = make_budget()
    token.value = True
    clock.value = 120.0
    before = token.calls, clock.calls
    assert current.must_stop is False
    assert current.stop_reason is None
    assert (token.calls, clock.calls) == before
    assert current.allows_search() is False
    assert current.must_stop is True
