"""Single-node stage starts from the verified whole-stage fixture, not another search."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import SearchStopReason, SolverPolicy, fingerprint, sum_weights
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    SearchState,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.neighborhoods import SearchContext, improve_real_node_relocation
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.construction.test_initial_solution_reference_stage import initial_stage

D = Decimal
BASE = Path(__file__).parents[2] / "baselines/gqga4"
WHOLE_END_FINGERPRINT = "196bc6f5c071569fe8ffd9bd98ef4ed8ea324047be8b23c36587180a7f65a64f"

# Verified independently against the pinned original with ONLY its existing carrier
# wildcard enabled. The original twelve-action evidence remains unchanged on disk.
# Fields: cumulative check, donor index, target index, node ID, position, underweight count/gap.
CARRIER_FREE_TRACE = (
    (3801, 1, 10, "0030118934-000010", 19, 3, "913.00"),
    (5304, 6, 11, "0030122760-000020", 8, 3, "894.65"),
    (5954, 2, 11, "0005000563-000240", 9, 3, "878.65"),
    (7768, 6, 11, "0005000563-000220", 9, 3, "860.43"),
    (9747, 6, 11, "0030122480-000070", 10, 3, "752.43"),
    (11890, 6, 11, "0005000563-000170", 10, 2, "746.74"),
    (14543, 10, 12, "0030124734-000010", 0, 2, "722.74"),
    (17460, 10, 12, "0002002185-000020", 4, 2, "699.74"),
    (19874, 7, 12, "0030124440-000040", 5, 2, "675.74"),
    (26447, 2, 21, "0030122898-000020", 8, 2, "656.48"),
    (34161, 6, 21, "0002002053-000050", 7, 2, "642.81"),
    (42085, 6, 21, "0030125147-000030", 9, 2, "532.81"),
    (50875, 8, 21, "0002002184-000200", 14, 2, "441.47"),
    (60028, 11, 21, "0005000563-000220", 13, 2, "423.25"),
    (69349, 11, 21, "0005000563-000240", 14, 2, "407.25"),
)


@pytest.fixture(scope="module")
def relocation_start():
    frozen = json.loads((BASE / "reference_stage_expectations.json").read_text(), parse_float=D)
    _, problem, cache, _, initial = initial_stage.__wrapped__()
    chain_ids = [chain.chain_id for chain in initial.candidate.plan.chains]
    nodes = {node.node_id: node for node in problem.nodes}
    prototypes = {item.prototype_id: item for item in problem.virtual_prototypes}
    accepted_virtual_ids = []
    actions = tuple(
        item
        for item in frozen["search_rounds"][0]["accepted_actions"]
        if item["phase"] == "whole_chain"
    )
    for action in actions:
        donor_index, target_index = action["donor_index"], action["target_index"]
        merged_id = chain_ids[target_index]
        chain_ids = [
            identity
            for index, identity in enumerate(chain_ids)
            if index not in (donor_index, target_index)
        ] + [merged_id]
        for chain in action["plan"]:
            for node_id in chain["node_ids"]:
                if node_id in nodes:
                    continue
                raw = frozen["node_catalog"][node_id]
                proto = prototypes[raw["virtual_prototype_id"]]
                accepted_virtual_ids.append(node_id)
                sequence = len(accepted_virtual_ids)
                nodes[node_id] = Node(
                    node_id=f"virtual-{sequence:06d}",
                    source_order_id=None,
                    source_resource_id=None,
                    source_period=None,
                    weight=proto.unit_weight,
                    width=proto.width,
                    thickness=proto.thickness,
                    min_temperature=None
                    if raw["min_soak_temp"] is None
                    else D(str(raw["min_soak_temp"])),
                    max_temperature=None
                    if raw["max_soak_temp"] is None
                    else D(str(raw["max_soak_temp"])),
                    grade=proto.grade,
                    material_role=MaterialRole.GENERATED_VIRTUAL,
                    rule_attributes=proto.rule_attributes,
                    virtual_lineage=VirtualLineage(
                        proto.prototype_id, VirtualPurpose.EDGE_BRIDGE, None, sequence
                    ),
                )
    # This is fixture-only reconstruction of an independently verified previous-stage result.
    plan = SchedulePlan(
        tuple(
            Chain(
                chain_id,
                tuple(nodes[node_id] for node_id in raw["node_ids"]),
                raw["assigned_period"],
            )
            for chain_id, raw in zip(chain_ids, actions[-1]["plan"])
        )
    )
    assert len(accepted_virtual_ids) == 7 and len(plan.chains) == 22
    assert fingerprint(plan) == WHOLE_END_FINGERPRINT
    evaluation = evaluate_plan(plan, cache.rule_set, cache.context)
    assert evaluation.quality_key == (1, D("670.3"), 4, D("996.37"), 22, D(140))
    policy = SolverPolicy(
        **json.loads((BASE / "gqga4_solver_policy.json").read_text(), parse_float=D)
    )
    # Historical fill/split/audit oracles inherit this allowance, not the current formal budget.
    policy = replace(policy, candidate_check_limit=frozen["parameters"]["candidate_check_budget"])
    return frozen, cache, plan, evaluation, policy


def test_historical_stage_policy_keeps_frozen_candidate_budget(relocation_start):
    frozen, _, _, _, policy = relocation_start
    formal = SolverPolicy(
        **json.loads((BASE / "gqga4_solver_policy.json").read_text(), parse_float=D)
    )
    assert formal.candidate_check_limit == 200000
    assert policy.candidate_check_limit == frozen["parameters"]["candidate_check_budget"] == 100000
    assert policy == replace(formal, candidate_check_limit=100000)


def start_relocation(start, *, limit=None):
    _, cache, plan, evaluation, policy = start
    if limit is not None:
        policy = replace(policy, candidate_check_limit=limit)
    runtime = SolveRuntimeBudget.from_policy(policy, 0, clock=lambda: 1.0)
    runtime.candidate_check_count = 2539
    state = SearchState(plan, evaluation, accepted_move_count=9, virtual_sequence=7)
    return state, SearchContext(VirtualFactory(cache, runtime), policy)


@pytest.fixture(scope="module")
def single_stage(relocation_start):
    state, context = start_relocation(relocation_start)
    snapshots = []
    original = neighborhoods.try_complete_candidate

    def capture(current, current_context, chains, **kwargs):
        accepted = original(current, current_context, chains, **kwargs)
        if accepted:
            snapshots.append(current.current_plan)
        return accepted

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(neighborhoods, "try_complete_candidate", capture)
        assert improve_real_node_relocation(state, context) is state
    assert fingerprint(relocation_start[2]) == WHOLE_END_FINGERPRINT
    return state, context, tuple(snapshots)


def test_all_fifteen_moves_match_the_independently_verified_carrier_free_trace(
    relocation_start, single_stage
):
    _, cache, expected_plan, initial_evaluation, _ = relocation_start
    state, context, snapshots = single_stage
    assert len(snapshots) == len(context.accepted_move_traces) == len(CARRIER_FREE_TRACE) == 15
    previous_quality = initial_evaluation.quality_key
    for sequence, (record, trace, actual_plan) in enumerate(
        zip(CARRIER_FREE_TRACE, context.accepted_move_traces, snapshots), 10
    ):
        check, donor_index, target_index, node_id, position, count, gap = record
        donor, target = expected_plan.chains[donor_index], expected_plan.chains[target_index]
        moved = next(node for node in donor.nodes if node.node_id == node_id)
        assert moved.material_role is not MaterialRole.GENERATED_VIRTUAL
        chains = list(expected_plan.chains)
        changes = (
            (donor_index, tuple(node for node in donor.nodes if node.node_id != node_id)),
            (target_index, target.nodes[:position] + (moved,) + target.nodes[position:]),
        )
        for index, nodes in changes:
            period = min(
                (
                    node.source_period
                    for node in nodes
                    if node.material_role is not MaterialRole.GENERATED_VIRTUAL
                ),
                key=cache.context.period_index.__getitem__,
            )
            chains[index] = replace(chains[index], nodes=nodes, assigned_period=period)
        expected_plan = SchedulePlan(tuple(chains))
        assert actual_plan == expected_plan, f"first differing accepted relocation: {sequence - 9}"
        assert trace.sequence == sequence and trace.candidate_check_count == check
        assert trace.action_name == "real_node_relocation"
        assert trace.affected_chain_ids == (donor.chain_id, target.chain_id)
        assert trace.quality_before == previous_quality
        assert trace.quality_after == (1, D("670.3"), count, D(gap), 22, D(140))
        previous_quality = trace.quality_after
    assert state.current_plan == expected_plan
    assert (
        fingerprint(context.accepted_move_traces)
        == "b600ba4c408648b4445d22a664f4b570a97bc41a0447032f26679acbb1d3cdb0"
    )


def test_stage_local_and_cumulative_counts_stay_distinct_and_no_material_is_created(
    relocation_start, single_stage
):
    _, _, initial_plan, _, _ = relocation_start
    state, context, _ = single_stage
    assert context.factory.budget.stop_reason is None
    assert context.factory.budget.candidate_check_count == 81823
    assert context.factory.budget.candidate_check_count - 2539 == 79284
    assert context.complete_candidate_evaluation_count == 24  # This context starts at zero.
    assert len(context.accepted_move_traces) == 15
    assert state.accepted_move_count == 9 + 15
    assert state.virtual_sequence == 7 and state.split_sequence == 0
    assert state.current_evaluation.quality_key == (1, D("670.3"), 2, D("407.25"), 22, D(140))
    assert (
        fingerprint(state.current_plan)
        == "27a71417e7ced252fead81b4009abefd536b57dc012c726cec8f3087d2edf75c"
    )
    before = {node.node_id: node for chain in initial_plan.chains for node in chain.nodes}
    after = tuple(node for chain in state.current_plan.chains for node in chain.nodes)
    assert len(after) == len(before) == 538
    assert {node.node_id for node in after} == set(before)
    assert all(node is before[node.node_id] for node in after)
    assert sum_weights(node.weight for node in after) == D("29473.91")
    assert tuple(chain.chain_id for chain in state.current_plan.chains) == tuple(
        chain.chain_id for chain in initial_plan.chains
    )


def test_first_reference_difference_is_the_approved_carrier_filter_removal(
    relocation_start, single_stage
):
    frozen, cache, _, _, _ = relocation_start
    _, context, snapshots = single_stage
    original_moves = tuple(
        item
        for item in frozen["search_rounds"][0]["accepted_actions"]
        if item["phase"] == "single_order"
    )
    assert tuple(item["candidate_checks"] for item in original_moves) == (
        3802,
        5305,
        5955,
        7780,
        9771,
        12436,
        15379,
        21579,
        28925,
        36512,
        44965,
        53783,
    )
    original = original_moves[0]
    assert original["position"] == 20
    assert original["moved_node"]["node_id"] == "0030118934-000010"
    assert original["quality"][:6] == [1, D("670.3"), 3, D(913), 22, D(140)]
    first = context.accepted_move_traces[0]
    assert first.candidate_check_count == 3801
    left, moved, right = snapshots[0].chains[10].nodes[18:21]
    assert (left.node_id, moved.node_id, right.node_id) == (
        "0002002202-000230",
        "0030118934-000010",
        "0002002180-000060",
    )
    assert (left.width, moved.width, right.width) == (D(1020), D(910), D(921))
    assert right.rule_attributes["hot_roll_grade"] == "SPHETI-3"
    historical = json.loads((BASE / "inputs/rule_context.json").read_text())
    assert historical["reverse_width_carrier_grades"] == ["SPHC"]
    assert cache.allows(left, moved) and cache.allows(moved, right)
    assert first.quality_after == tuple(original["quality"][:6])
    # The original rejects position 19 in complete chain evaluation, not its edge predicate.
    assert original_moves[-1]["quality"][:6] == [1, D("670.3"), 2, D("548.32"), 22, D(140)]


@pytest.mark.parametrize("limit,accepted", ((3800, 0), (3801, 1)))
def test_budget_boundary_before_and_at_first_current_improvement(relocation_start, limit, accepted):
    state, context = start_relocation(relocation_start, limit=limit)
    improve_real_node_relocation(state, context)
    assert context.factory.budget.candidate_check_count == limit
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert state.accepted_move_count == 9 + accepted
    assert len(context.accepted_move_traces) == accepted
    assert state.virtual_sequence == 7 and state.split_sequence == 0
    if accepted:
        assert context.accepted_move_traces[0].candidate_check_count == 3801
        assert state.current_evaluation.quality_key == (1, D("670.3"), 3, D(913), 22, D(140))
    else:
        assert state.current_plan is relocation_start[2]
        assert state.current_evaluation is relocation_start[3]
