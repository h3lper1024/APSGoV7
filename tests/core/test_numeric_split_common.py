"""Split preparation/charge/validation preserves the original transactional order."""

from dataclasses import replace

import pytest

from apsgo_scheduler.core import _numeric_search as search
from apsgo_scheduler.core import _numeric_candidate_kernel as candidate
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan
from apsgo_scheduler.core._numeric_units import NumericValueError
from tests.core.test_numeric_private_split import split_case
from tests.core.test_numeric_candidate_kernel import descriptor, A
from tests.core.test_numeric_construction import budget


def state_for_split(period="P0", weight="600"):
    workspace, program, quality, decision = split_case(weight, period)
    state = search.NumericSearchState(workspace.task, program, quality, workspace.plan,
        evaluate_numeric_plan(workspace.task, program, quality, workspace.plan))
    return state, workspace, decision


@pytest.mark.parametrize("period", ("P0", "P1"))
def test_split_modes_and_unique_replay_use_no_old_object_candidate_path(period, monkeypatch):
    state, _, _ = state_for_split(period)

    def forbidden(*args, **kwargs):
        raise AssertionError("split or replay returned to old object preparation")

    for name in ("_layout", "_prepare_numeric_split", "_try_prepared_candidate",
                 "extend_resource_workspace", "split_piece_node", "choose_split_separator"):
        assert not hasattr(search, name)
    runtime = budget(candidate_limit=1000)
    search.improve_numeric_controlled_split(state, runtime, pair_scan_slack_weight=0)
    assert state.split_sequence == state.replay_count == 1
    assert int(state.task.split_groups.mode[0]) == (0 if period == "P0" else 1)
    assert state.accepted_moves[0].action is A.CONTROLLED_ORDER_SPLIT


@pytest.mark.parametrize("limit", (0, 10))
def test_private_preparation_precedes_charge_but_evaluation_follows_charge(limit, monkeypatch):
    state, _, _ = state_for_split()
    runtime = budget(candidate_limit=limit)
    events = []
    retries = []
    original_prepare = candidate.prepare_candidate_attempt
    original_evaluate = candidate.evaluate_numeric_view

    def prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        if args[3].values[0, candidate.ACTION] == candidate.SPLIT:
            if result.status == candidate.CAPACITY:
                retries.append((runtime.candidate_check_count, result.prepared))
            else:
                events.append(("prepare", runtime.candidate_check_count, result.prepared))
        return result

    def evaluate(*args, **kwargs):
        events.append(("evaluate", runtime.candidate_check_count, True))
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(candidate, "prepare_candidate_attempt", prepare)
    monkeypatch.setattr(candidate, "evaluate_numeric_view", evaluate)
    search.improve_numeric_controlled_split(state, runtime, pair_scan_slack_weight=0)
    assert retries == [(0, False)]
    assert events[0] == ("prepare", 0, True)
    if limit:
        assert events[1] == ("evaluate", 1, True)
        assert state.split_sequence == 1
    else:
        assert events == [("prepare", 0, True)]
        assert state.split_sequence == state.complete_candidate_evaluation_count == state.replay_count == 0


def test_split_period_rejection_keeps_the_original_charge_without_evaluation(monkeypatch):
    state, _, _ = state_for_split()
    runtime = budget(candidate_limit=100)
    monkeypatch.setattr(candidate, "_split_periods_match", lambda *args: False)
    search.improve_numeric_controlled_split(state, runtime, pair_scan_slack_weight=0)
    assert runtime.candidate_check_count == 1
    assert state.complete_candidate_evaluation_count == state.accepted_move_count == state.replay_count == 0


def test_infeasible_piece_preparation_consumes_no_candidate_quota():
    state, _, _ = state_for_split(weight="500.001")
    runtime = budget(candidate_limit=100)
    search.improve_numeric_controlled_split(state, runtime, pair_scan_slack_weight=0)
    assert runtime.candidate_check_count == state.complete_candidate_evaluation_count == state.replay_count == 0


def test_preparation_cannot_be_consumed_or_resumed_with_stale_policy_or_view():
    state, workspace, decision = state_for_split()
    values = descriptor(workspace, A.CONTROLLED_ORDER_SPLIT, target=11, node_row=0)
    policy = candidate.CandidateCheckPolicy()
    pending = candidate.prepare_candidate_attempt(workspace, state.program, state.quality,
        values, 0, policy, split_decision=decision)
    assert pending.prepared and pending.summary is None
    runtime = budget(candidate_limit=10)
    runtime.consume_candidate_check()
    with pytest.raises(NumericValueError, match="unfinished"):
        search.consume_candidate_result(state, runtime, workspace, pending)
    assert state.complete_candidate_evaluation_count == 0
    with pytest.raises(NumericValueError, match="unfinished"):
        candidate.compute_candidate_attempt(workspace, state.program, state.quality, values, 0,
            replace(policy, maximum_bridge_nodes=0), preparation=pending)
    workspace.reset()
    with pytest.raises(NumericValueError, match="expired"):
        candidate.compute_candidate_attempt(workspace, state.program, state.quality, values, 0,
            policy, preparation=pending)
