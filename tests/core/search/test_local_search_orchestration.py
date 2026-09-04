"""Fixed search orchestration reuses the existing stages, state and allowance."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import SearchStopReason, SolverPolicy, fingerprint
from apsgo_scheduler.core.model import SearchState, VirtualPurpose
from apsgo_scheduler.core.neighborhoods import SearchContext, run_local_search
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.construction.test_initial_solution_reference_stage import initial_stage
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.search.test_single_node_reference_trace import CARRIER_FREE_TRACE
from tests.core.search.test_virtual_fill_reference_trace import CARRIER_FREE_FILL_TRACE
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.search.test_virtual_weight_fill import fill_case
from tests.core.search.test_whole_chain_neighborhood import chain

D = Decimal
STAGES = (
    "improve_whole_chain",
    "improve_real_node_relocation",
    "improve_virtual_weight_fill",
)


def test_fixed_calls_preserve_existing_state_trace_and_shared_budget(monkeypatch):
    token = SimpleNamespace(is_cancelled=lambda: False)
    runtime = budget(candidate_check_limit=100, cancellation=token)
    state, context = fill_case(
        (chain("T", node("t", weight="30")),),
        (prototype("p"),),
        runtime=runtime,
    )
    neighborhoods.improve_virtual_weight_fill(state, context)
    assert state.accepted_move_count == state.virtual_sequence == 1
    assert context.complete_candidate_evaluation_count == 1
    assert runtime.permit(6) and runtime.candidate_check_count == 7
    previous_trace = context.accepted_move_traces
    previous_state = fingerprint(state)
    timing = (
        runtime.started_at_monotonic,
        runtime.search_deadline_monotonic,
        runtime.final_deadline_monotonic,
        runtime.clock,
    )
    calls = []

    for name in STAGES:

        def stage(current, current_context, *, stage_name=name):
            assert current is state and current_context is context
            assert current_context.factory.budget is runtime
            assert runtime.cancellation is token and runtime.stop_reason is None
            assert runtime.candidate_check_count == 7 + len(calls)
            assert current_context.accepted_move_traces is previous_trace
            assert current_context.complete_candidate_evaluation_count == 1
            assert fingerprint(current) == previous_state
            calls.append(stage_name)
            assert runtime.consume_candidate_check()
            return current

        monkeypatch.setattr(neighborhoods, name, stage)

    assert run_local_search(state, context) is state
    assert tuple(calls) == STAGES
    assert runtime.candidate_check_count == 10 and runtime.candidate_check_limit == 100
    assert runtime.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert context.accepted_move_traces is previous_trace
    assert context.complete_candidate_evaluation_count == 1
    assert fingerprint(state) == previous_state
    assert timing == (
        runtime.started_at_monotonic,
        runtime.search_deadline_monotonic,
        runtime.final_deadline_monotonic,
        runtime.clock,
    )


def test_actual_filling_improvement_does_not_restart_earlier_neighborhoods(monkeypatch):
    state, context = fill_case((chain("T", node("t", weight="30")),), (prototype("p"),))
    calls = []

    for name in STAGES:
        original = getattr(neighborhoods, name)

        def stage(current, current_context, *, stage_name=name, function=original):
            assert current is state and current_context is context
            before = current.accepted_move_count
            result = function(current, current_context)
            calls.append((stage_name, before, current.accepted_move_count))
            assert result is current and current_context.factory.budget.stop_reason is None
            return result

        monkeypatch.setattr(neighborhoods, name, stage)

    assert run_local_search(state, context) is state
    assert calls == [(STAGES[0], 0, 0), (STAGES[1], 0, 0), (STAGES[2], 0, 1)]
    assert state.current_plan.chains[0].total_weight == D(50)
    assert state.virtual_sequence == 1 and state.split_sequence == 0
    assert (
        state.current_plan.chains[0].nodes[0].virtual_lineage.purpose is VirtualPurpose.WEIGHT_FILL
    )
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 1
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


@pytest.fixture(scope="module")
def complete_gqga4_search():
    _, problem, cache, _, initial = initial_stage.__wrapped__()
    assert len(initial.candidate.plan.chains) == 31
    before = fingerprint(problem)
    policy = SolverPolicy(
        **json.loads(
            (Path(__file__).parents[2] / "baselines/gqga4/gqga4_solver_policy.json").read_text(),
            parse_float=D,
        )
    )
    runtime = SolveRuntimeBudget.from_policy(policy, 0, clock=lambda: 1.0)
    context = SearchContext(VirtualFactory(cache, runtime), policy)
    state = SearchState(initial.candidate.plan, initial.candidate.search_evaluation)
    boundaries = []

    def record(name, event):
        boundaries.append(
            (
                name,
                event,
                runtime.candidate_check_count,
                context.complete_candidate_evaluation_count,
                state.accepted_move_count,
                state.virtual_sequence,
            )
        )

    with pytest.MonkeyPatch.context() as patch:
        for name in STAGES:
            original = getattr(neighborhoods, name)

            def stage(current, current_context, *, stage_name=name, function=original):
                assert current is state and current_context is context
                assert current_context.factory.budget is runtime
                record(stage_name, "start")
                result = function(current, current_context)
                record(stage_name, "end")
                assert result is state and runtime.stop_reason is None
                return result

            patch.setattr(neighborhoods, name, stage)
        assert run_local_search(state, context) is state
    assert fingerprint(problem) == before
    return state, context, tuple(boundaries)


def test_actual_gqga4_three_stage_boundaries_keep_one_cumulative_allowance(complete_gqga4_search):
    state, context, boundaries = complete_gqga4_search
    assert boundaries == (
        (STAGES[0], "start", 0, 0, 0, 0),
        (STAGES[0], "end", 2539, 366, 9, 7),
        (STAGES[1], "start", 2539, 366, 9, 7),
        (STAGES[1], "end", 81823, 390, 24, 7),
        (STAGES[2], "start", 81823, 390, 24, 7),
        (STAGES[2], "end", 86413, 884, 35, 18),
    )
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert context.factory.budget.candidate_check_count == 86413
    assert context.complete_candidate_evaluation_count == 884
    assert state.accepted_move_count == 35 and state.virtual_sequence == 18
    assert state.split_sequence == 0
    assert state.current_evaluation.quality_key == (1, D("670.3"), 1, D("194.42"), 22, D(360))
    assert fingerprint(state.current_plan) == (
        "cef0f1b44f54e46bab0a64871e5aecb74c489078b937c37494df989af74cec86"
    )


def test_actual_gqga4_all_thirty_five_moves_preserve_prior_stage_oracles(complete_gqga4_search):
    _, context, _ = complete_gqga4_search
    traces = context.accepted_move_traces
    assert len(traces) == 35
    assert tuple(item.sequence for item in traces) == tuple(range(1, 36))
    assert tuple(item.candidate_check_count for item in traces) == (
        85,
        86,
        147,
        199,
        223,
        263,
        343,
        412,
        934,
        *(item[0] for item in CARRIER_FREE_TRACE),
        *(item[0] for item in CARRIER_FREE_FILL_TRACE),
    )
    assert all(item.action_name.startswith("whole_chain_") for item in traces[:9])
    assert all(item.action_name == "real_node_relocation" for item in traces[9:24])
    assert all(item.action_name == "virtual_weight_fill" for item in traces[24:])
    assert all(
        right.quality_before == left.quality_after for left, right in zip(traces, traces[1:])
    )
    assert fingerprint(traces) == (
        "98836d09c4e96c1dbb4f1b1652f707e48c96692627a3dc8fea4090d2b16faf09"
    )
