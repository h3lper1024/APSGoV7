"""The local-search coordinator distinguishes natural completion from interruption."""

from decimal import Decimal

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.neighborhoods import run_local_search
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.search.test_single_real_node_relocation import Stop
from tests.core.search.test_whole_chain_neighborhood import chain, make_search

PHASE_NAMES = (
    "improve_whole_chain",
    "improve_real_node_relocation",
    "improve_virtual_weight_fill",
)


def setup(*, node_count=1, limit=10, runtime=None):
    return make_search(
        tuple(chain(f"C-{index}", node(f"n-{index}")) for index in range(node_count)),
        chain_count=True,
        limit=limit,
        runtime=runtime,
    )


def install_phases(monkeypatch, hook):
    visited = []
    for index, name in enumerate(PHASE_NAMES):

        def phase(state, context, index=index):
            visited.append(index)
            hook(index, state, context)
            return state

        monkeypatch.setattr(neighborhoods, name, phase)
    return visited


@pytest.mark.parametrize("reason", tuple(SearchStopReason))
def test_existing_stop_reason_is_sticky_without_entering_a_phase(reason, monkeypatch):
    runtime = budget(candidate_check_limit=10, candidate_check_count=2, stop_reason=reason)
    state, context = setup(runtime=runtime)
    before = fingerprint(state)

    def forbidden(*args):
        raise AssertionError("an already stopped search must not be rearmed")

    visited = install_phases(monkeypatch, forbidden)
    assert run_local_search(state, context) is state
    assert visited == []
    assert runtime.stop_reason is reason
    assert runtime.candidate_check_count == 2
    assert fingerprint(state) == before
    assert context.complete_candidate_evaluation_count == 0
    assert context.accepted_move_traces == ()


@pytest.mark.parametrize("node_count,limit,expected_checks", ((1, 0, 0), (2, 1, 1)))
def test_zero_or_exactly_used_allowance_can_finish_when_no_further_candidate_is_needed(
    node_count,
    limit,
    expected_checks,
):
    state, context = setup(node_count=node_count, limit=limit)
    assert run_local_search(state, context) is state
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert context.factory.budget.candidate_check_count == expected_checks
    assert context.complete_candidate_evaluation_count == expected_checks
    assert state.accepted_move_count == expected_checks
    assert len(state.current_plan.chains) == 1
    assert state.virtual_sequence == 0


def test_repeating_a_completed_search_does_not_reset_reason_counts_or_accepted_history(monkeypatch):
    state, context = setup(node_count=2, limit=1)
    run_local_search(state, context)
    before, traces = fingerprint(state), context.accepted_move_traces

    def forbidden(*args):
        raise AssertionError("a second search requires an explicit outer lifecycle transition")

    visited = install_phases(monkeypatch, forbidden)
    assert run_local_search(state, context) is state
    assert visited == []
    assert fingerprint(state) == before and context.accepted_move_traces == traces
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 1
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


@pytest.mark.parametrize("phase_index", range(3))
@pytest.mark.parametrize("stop_kind", ("candidate", "cancel", "time"))
def test_each_phase_boundary_including_the_final_probe_observes_a_real_stop(
    phase_index,
    stop_kind,
    monkeypatch,
):
    stop = Stop()
    runtime = budget(
        candidate_check_limit=phase_index + 1 if stop_kind == "candidate" else 10,
        cancellation=stop if stop_kind == "cancel" else None,
        clock=lambda: 100.0 if stop.active and stop_kind == "time" else 1.0,
    )
    state, context = setup(runtime=runtime)
    before = fingerprint(state)

    def stop_in_phase(index, received_state, received_context):
        assert received_state is state and received_context is context
        assert runtime.consume_candidate_check()
        if index == phase_index:
            if stop_kind == "candidate":
                assert not runtime.consume_candidate_check()
            else:
                # The phase returns without a further check; the coordinator must notice.
                stop.active = True

    visited = install_phases(monkeypatch, stop_in_phase)
    assert run_local_search(state, context) is state
    assert visited == list(range(phase_index + 1))
    assert runtime.candidate_check_count == phase_index + 1
    assert (
        runtime.stop_reason
        is {
            "candidate": SearchStopReason.CANDIDATE_LIMIT_REACHED,
            "cancel": SearchStopReason.USER_CANCELLED,
            "time": SearchStopReason.SEARCH_TIME_LIMIT_REACHED,
        }[stop_kind]
    )
    assert fingerprint(state) == before
    assert context.complete_candidate_evaluation_count == 0
    assert context.accepted_move_traces == ()


@pytest.mark.parametrize("phase_index", range(3))
def test_phase_errors_propagate_and_never_claim_natural_completion(phase_index, monkeypatch):
    state, context = setup()
    before = fingerprint(state)

    def fail_in_phase(index, received_state, received_context):
        assert received_state is state and received_context is context
        assert context.factory.budget.consume_candidate_check()
        if index == phase_index:
            raise RuntimeError("synthetic phase failure")

    visited = install_phases(monkeypatch, fail_in_phase)
    with pytest.raises(RuntimeError, match="synthetic phase failure"):
        run_local_search(state, context)
    assert visited == list(range(phase_index + 1))
    assert context.factory.budget.candidate_check_count == phase_index + 1
    assert context.factory.budget.stop_reason is None
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()


@pytest.mark.parametrize("stop_kind", ("candidate", "cancel", "time"))
def test_real_search_keeps_accepted_candidate_when_the_next_work_is_interrupted(
    stop_kind,
    monkeypatch,
):
    stop = Stop()
    runtime = budget(
        candidate_check_limit=1 if stop_kind == "candidate" else 100,
        cancellation=stop if stop_kind == "cancel" else None,
        clock=lambda: 100.0 if stop.active and stop_kind == "time" else 1.0,
    )
    state, context = setup(node_count=3, runtime=runtime)
    initial_quality = state.current_evaluation.quality_key
    input_before = fingerprint(context.factory.cache.problem)
    original = neighborhoods.try_complete_candidate

    def accept_then_signal(*args, **kwargs):
        accepted = original(*args, **kwargs)
        if accepted and stop_kind != "candidate":
            stop.active = True
        return accepted

    def later_phase_must_not_run(*args):
        raise AssertionError("a truncated whole-chain phase must not enter later neighborhoods")

    monkeypatch.setattr(neighborhoods, "try_complete_candidate", accept_then_signal)
    monkeypatch.setattr(neighborhoods, "improve_real_node_relocation", later_phase_must_not_run)
    monkeypatch.setattr(neighborhoods, "improve_virtual_weight_fill", later_phase_must_not_run)
    assert run_local_search(state, context) is state
    assert (
        runtime.stop_reason
        is {
            "candidate": SearchStopReason.CANDIDATE_LIMIT_REACHED,
            "cancel": SearchStopReason.USER_CANCELLED,
            "time": SearchStopReason.SEARCH_TIME_LIMIT_REACHED,
        }[stop_kind]
    )
    assert state.accepted_move_count == runtime.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 1
    assert state.current_evaluation.quality_key == (0, Decimal(0), 2)
    assert state.current_evaluation.quality_key < initial_quality
    assert len(state.current_plan.chains) == 2 and state.virtual_sequence == 0
    assert sorted(item.node_id for chain in state.current_plan.chains for item in chain.nodes) == [
        "n-0",
        "n-1",
        "n-2",
    ]
    assert state.current_evaluation == evaluate_plan(
        state.current_plan,
        context.factory.cache.rule_set,
        context.factory.cache.context,
    )
    (trace,) = context.accepted_move_traces
    assert trace.candidate_check_count == 1
    assert trace.quality_before == initial_quality
    assert trace.quality_after == state.current_evaluation.quality_key
    assert fingerprint(context.factory.cache.problem) == input_before


def test_error_in_a_later_phase_preserves_the_previously_accepted_complete_plan(monkeypatch):
    state, context = setup(node_count=2)

    def fail(*args):
        raise RuntimeError("later neighborhood failed")

    def forbidden(*args):
        raise AssertionError("failure must propagate before virtual filling")

    monkeypatch.setattr(neighborhoods, "improve_real_node_relocation", fail)
    monkeypatch.setattr(neighborhoods, "improve_virtual_weight_fill", forbidden)
    with pytest.raises(RuntimeError, match="later neighborhood failed"):
        run_local_search(state, context)
    assert state.accepted_move_count == 1 and len(state.current_plan.chains) == 1
    assert context.complete_candidate_evaluation_count == 1
    assert context.factory.budget.candidate_check_count == 1
    assert context.factory.budget.stop_reason is None
    assert len(context.accepted_move_traces) == 1
    assert state.current_evaluation == evaluate_plan(
        state.current_plan,
        context.factory.cache.rule_set,
        context.factory.cache.context,
    )
