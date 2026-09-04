"""Portable whole-chain stage comparison; original seven-level evidence is unchanged."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import SolverPolicy, fingerprint, sum_weights
from apsgo_scheduler.core.model import MaterialRole, SearchState
from apsgo_scheduler.core.neighborhoods import SearchContext, improve_whole_chain
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.construction.test_initial_solution_reference_stage import initial_stage

D = Decimal
BASE = Path(__file__).parents[2] / "baselines/gqga4"


def target_signature(plan):
    def semantic(node):
        if node.material_role is not MaterialRole.GENERATED_VIRTUAL:
            return node.node_id
        return (
            "virtual",
            node.virtual_lineage.prototype_id,
            node.weight,
            node.width,
            node.thickness,
            node.min_temperature,
            node.max_temperature,
        )

    return tuple(
        (chain.assigned_period, tuple(semantic(node) for node in chain.nodes))
        for chain in plan.chains
    )


def reference_signature(plan, catalog):
    def semantic(node_id):
        item = catalog[node_id]
        if not item["virtual_prototype_id"]:
            return node_id
        return (
            "virtual",
            item["virtual_prototype_id"],
            D(item["weight"]),
            *(
                None if item[key] is None else D(str(item[key]))
                for key in ("width", "thickness", "min_soak_temp", "max_soak_temp")
            ),
        )

    return tuple(
        (chain["assigned_period"], tuple(semantic(node) for node in chain["node_ids"]))
        for chain in plan
    )


@pytest.fixture(scope="module")
def whole_stage():
    frozen = json.loads((BASE / "reference_stage_expectations.json").read_text(), parse_float=D)
    _, problem, cache, _, initial = initial_stage.__wrapped__()
    problem_before = fingerprint(problem)
    policy = SolverPolicy(
        **json.loads((BASE / "gqga4_solver_policy.json").read_text(), parse_float=D)
    )
    runtime = SolveRuntimeBudget.from_policy(policy, 0, clock=lambda: 1.0)
    context = SearchContext(VirtualFactory(cache, runtime), policy)
    state = SearchState(initial.candidate.plan, initial.candidate.search_evaluation)
    snapshots = []
    original = neighborhoods.try_complete_candidate

    def capture(current, current_context, chains, **kwargs):
        accepted = original(current, current_context, chains, **kwargs)
        if accepted:
            snapshots.append(current.current_plan)
        return accepted

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(neighborhoods, "try_complete_candidate", capture)
        assert improve_whole_chain(state, context) is state
    assert fingerprint(problem) == problem_before
    return frozen, initial, state, context, tuple(snapshots)


def test_every_accepted_action_and_complete_plan_matches_frozen_reference_semantics(whole_stage):
    frozen, _, state, context, snapshots = whole_stage
    reference = tuple(
        item
        for item in frozen["search_rounds"][0]["accepted_actions"]
        if item["phase"] == "whole_chain"
    )
    assert len(reference) == len(snapshots) == state.accepted_move_count == 9
    assert tuple(item.candidate_check_count for item in context.accepted_move_traces) == (
        85,
        86,
        147,
        199,
        223,
        263,
        343,
        412,
        934,
    )
    for index, (actual, plan, expected) in enumerate(
        zip(context.accepted_move_traces, snapshots, reference), 1
    ):
        assert actual.sequence == index
        assert actual.candidate_check_count == expected["candidate_checks"]
        assert actual.quality_before == tuple(expected["quality_before"][:6])
        assert actual.quality_after == tuple(expected["quality"][:6])
        # Private/accepted virtual numbering changed by design; physical decisions did not.
        assert target_signature(plan) == reference_signature(
            expected["plan"], frozen["node_catalog"]
        ), f"first differing accepted whole-chain plan: {index}"


def test_whole_stage_exhausts_naturally_with_reference_counts_and_conserved_input(whole_stage):
    frozen, _, state, context, _ = whole_stage
    boundary = next(
        item
        for item in frozen["search_rounds"][0]["phase_boundaries"]
        if item["phase"] == "whole_chain" and item["event"] == "end"
    )
    assert context.factory.budget.stop_reason is None
    assert context.factory.budget.candidate_check_count == boundary["candidate_checks"] == 2539
    assert context.complete_candidate_evaluation_count == 366
    assert state.current_evaluation.quality_key == (1, D("670.3"), 4, D("996.37"), 22, D(140))
    assert state.current_evaluation.quality_key == tuple(boundary["quality"][:6])
    assert len(boundary["quality"]) == 7
    assert (
        state.current_evaluation.metrics["borrowed_future_weight"]
        == boundary["quality"][6]
        == D("23460.88")
    )
    scheduled = tuple(node for chain in state.current_plan.chains for node in chain.nodes)
    real = tuple(
        node for node in scheduled if node.material_role is not MaterialRole.GENERATED_VIRTUAL
    )
    virtual = tuple(
        node for node in scheduled if node.material_role is MaterialRole.GENERATED_VIRTUAL
    )
    original = {node.node_id: node for node in context.factory.cache.problem.nodes}
    assert len(real) == len(original) == 531
    assert {node.node_id for node in real} == set(original)
    assert all(node is original[node.node_id] for node in real)
    assert sum_weights(node.weight for node in real) == D("29333.91")
    assert sum_weights(node.weight for node in virtual) == D(140)
    assert len(virtual) == state.virtual_sequence == 7
    assert tuple(sorted(node.virtual_lineage.accepted_sequence for node in virtual)) == tuple(
        range(1, 8)
    )
    assert state.split_sequence == 0 and all(node.split_lineage is None for node in scheduled)


@pytest.mark.parametrize("limit,accepted_count", ((84, 0), (85, 1)))
def test_candidate_budget_boundary_before_and_at_first_reference_improvement(
    whole_stage, limit, accepted_count
):
    _, initial, _, completed_context, _ = whole_stage
    policy = replace(completed_context.policy, candidate_check_limit=limit)
    runtime = SolveRuntimeBudget.from_policy(policy, 0, clock=lambda: 1.0)
    context = SearchContext(VirtualFactory(completed_context.factory.cache, runtime), policy)
    state = SearchState(initial.candidate.plan, initial.candidate.search_evaluation)
    improve_whole_chain(state, context)
    assert runtime.candidate_check_count == limit
    assert runtime.stop_reason.value == "candidate_limit_reached"
    assert state.accepted_move_count == accepted_count
    assert len(state.current_plan.chains) == 31 - accepted_count
    assert context.complete_candidate_evaluation_count == 12 + accepted_count
    if not accepted_count:
        assert state.current_plan is initial.candidate.plan
        assert state.current_evaluation is initial.candidate.search_evaluation
        assert context.accepted_move_traces == ()
    else:
        assert context.accepted_move_traces[0].candidate_check_count == 85
        assert state.current_evaluation.quality_key == (2, D("170.3"), 14, D("3424.93"), 30, D(0))
