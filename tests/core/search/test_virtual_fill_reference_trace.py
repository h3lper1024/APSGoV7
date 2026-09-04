"""Portable filling-stage oracle, preserving the original nineteen-action baseline."""

from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint, sum_weights
from apsgo_scheduler.core.model import (
    MaterialRole,
    Node,
    SchedulePlan,
    SearchState,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.neighborhoods import SearchContext, improve_virtual_weight_fill
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.search.test_single_node_reference_trace import relocation_start, single_stage
from tests.core.search.test_whole_chain_reference_trace import reference_signature, target_signature

D = Decimal
SINGLE_END_FINGERPRINT = "27a71417e7ced252fead81b4009abefd536b57dc012c726cec8f3087d2edf75c"

# The pinned original, changing ONLY its previously cancelled carrier filter to
# the existing {'*'} option. Each row was checked against the target independently.
# check, target index, position, prototype suffix, gap, temperature bounds, full signature
CARRIER_FREE_FILL_TRACE = (
    (
        81848,
        12,
        0,
        "1500x1.5",
        "387.25",
        795,
        815,
        "987533840346fe73e3b92c62060305c588cbcdf7d62d92e49d104989d662262b",
    ),
    (
        81872,
        12,
        0,
        "1500x1.2",
        "367.25",
        795,
        815,
        "3c0898f8f03d3663dda1e1a7c1d99776f6de035fc38398c393e5250d0d929c95",
    ),
    (
        81979,
        12,
        3,
        "1500x2",
        "347.25",
        795,
        815,
        "d1730e074076bff1f0ac5bd0efe891bec4885ef7de8f2e6f061bc9ed948e4f0d",
    ),
    (
        82085,
        12,
        3,
        "1500x1.5",
        "327.25",
        795,
        815,
        "e8e3a1a2edb05c1b0b4c12e3ea461579a4f9090e815fa4a26ed42ac9d6acefb0",
    ),
    (
        82264,
        12,
        6,
        "1250x2",
        "307.25",
        795,
        815,
        "eae60596f4fad79bfd557b8e198767b7d4fc9e9fad459b42a153c3f648377658",
    ),
    (
        82443,
        12,
        6,
        "1250x2",
        "287.25",
        795,
        815,
        "9828eb14f86eb0692eb14e2a5c072c6f8080924509e73da77392c06939585075",
    ),
    (
        82991,
        12,
        20,
        "1000x2",
        "267.25",
        770,
        810,
        "41179e3c364eb246eb0d330cfde777ea9c82add71f9671ab986cfc562e94df52",
    ),
    (
        83539,
        12,
        20,
        "1000x2",
        "247.25",
        770,
        810,
        "774988f5adb80abe12af283d318a1ed843030c46a8a4dfde63bfb7bf626c423f",
    ),
    (
        84204,
        21,
        1,
        "1250x2",
        "227.25",
        760,
        780,
        "93f5bbdbd00b088b19df29a67b32a4d644578adbedb0d818742a46c4b4e8533f",
    ),
    (
        84869,
        21,
        1,
        "1250x2",
        "207.25",
        760,
        780,
        "d7f9999a82201b4f493263ad8d492e0c81bcd7b34c0301eef652a873fc19e9a1",
    ),
    (
        85792,
        21,
        11,
        "1000x1",
        "194.42",
        830,
        850,
        "18d46ce2899259deabef32471d324a58971bd546728706192f8a2d9a2662cc14",
    ),
)


@pytest.fixture(scope="module")
def fill_start():
    previous_start = relocation_start.__wrapped__()
    state, context, _ = single_stage.__wrapped__(previous_start)
    assert fingerprint(state.current_plan) == SINGLE_END_FINGERPRINT
    assert context.factory.budget.candidate_check_count == 81823
    assert state.accepted_move_count == 24 and state.virtual_sequence == 7
    return previous_start[0], context, state.current_plan, state.current_evaluation


def start_filling(start, *, limit=None):
    _, previous_context, plan, evaluation = start
    policy = previous_context.policy
    if limit is not None:
        policy = replace(policy, candidate_check_limit=limit)
    budget = SolveRuntimeBudget.from_policy(policy, 0, clock=lambda: 1.0)
    budget.candidate_check_count = 81823
    state = SearchState(plan, evaluation, accepted_move_count=24, virtual_sequence=7)
    context = SearchContext(VirtualFactory(previous_context.factory.cache, budget), policy)
    return state, context


@pytest.fixture(scope="module")
def fill_stage(fill_start):
    state, context = start_filling(fill_start)
    snapshots = []
    original = neighborhoods.try_complete_candidate

    def capture(current, current_context, chains, **kwargs):
        accepted = original(current, current_context, chains, **kwargs)
        if accepted:
            snapshots.append(current.current_plan)
        return accepted

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(neighborhoods, "try_complete_candidate", capture)
        assert improve_virtual_weight_fill(state, context) is state
    assert fingerprint(fill_start[2]) == SINGLE_END_FINGERPRINT
    return state, context, tuple(snapshots)


def test_all_eleven_fillers_match_the_independent_carrier_free_original(fill_start, fill_stage):
    _, previous_context, expected_plan, previous_evaluation = fill_start
    state, context, snapshots = fill_stage
    prototypes = {
        item.prototype_id: item
        for item in previous_context.factory.cache.problem.virtual_prototypes
    }
    assert len(snapshots) == len(context.accepted_move_traces) == len(CARRIER_FREE_FILL_TRACE) == 11
    previous_quality = previous_evaluation.quality_key
    for offset, (record, trace, actual_plan) in enumerate(
        zip(CARRIER_FREE_FILL_TRACE, context.accepted_move_traces, snapshots), 1
    ):
        check, target_index, position, suffix, gap, lower, upper, signature = record
        prototype = prototypes[f"virtual_sphc:{suffix}"]
        target = expected_plan.chains[target_index]
        sequence = 7 + offset
        filler = Node(
            node_id=f"virtual-{sequence:06d}",
            source_order_id=None,
            source_resource_id=None,
            source_period=None,
            weight=prototype.unit_weight,
            width=prototype.width,
            thickness=prototype.thickness,
            min_temperature=D(lower),
            max_temperature=D(upper),
            grade=prototype.grade,
            material_role=MaterialRole.GENERATED_VIRTUAL,
            rule_attributes=prototype.rule_attributes,
            virtual_lineage=VirtualLineage(
                prototype.prototype_id, VirtualPurpose.WEIGHT_FILL, None, sequence
            ),
        )
        chains = list(expected_plan.chains)
        chains[target_index] = replace(
            target, nodes=target.nodes[:position] + (filler,) + target.nodes[position:]
        )
        expected_plan = SchedulePlan(tuple(chains))
        assert actual_plan == expected_plan, f"first differing fill: {offset}"
        assert fingerprint(target_signature(actual_plan)) == signature
        assert filler.weight == D(20)
        assert trace.sequence == 24 + offset and trace.candidate_check_count == check
        assert trace.action_name == "virtual_weight_fill"
        assert trace.affected_chain_ids == (target.chain_id,)
        assert trace.quality_before == previous_quality
        assert trace.quality_after == (
            1,
            D("670.3"),
            1 if offset == 11 else 2,
            D(gap),
            22,
            D(140 + 20 * offset),
        )
        previous_quality = trace.quality_after
    assert state.current_plan == expected_plan
    assert fingerprint(context.accepted_move_traces) == (
        "6c8f43ac2226a090c052a2ecf857be75fc82cfebb8aa1b857af25e12dfbaad70"
    )


def test_local_counts_and_material_identity_remain_distinct_from_previous_stages(
    fill_start, fill_stage
):
    state, context, _ = fill_stage
    assert context.factory.budget.stop_reason is None
    assert context.factory.budget.candidate_check_count == 86413
    assert context.factory.budget.candidate_check_count - 81823 == 4590
    assert context.complete_candidate_evaluation_count == 494
    assert len(context.accepted_move_traces) == 11
    assert state.accepted_move_count == 24 + 11
    assert state.virtual_sequence == 7 + 11 and state.split_sequence == 0
    assert state.current_evaluation.quality_key == (1, D("670.3"), 1, D("194.42"), 22, D(360))
    assert fingerprint(state.current_plan) == (
        "cef0f1b44f54e46bab0a64871e5aecb74c489078b937c37494df989af74cec86"
    )
    before = {node.node_id: node for chain in fill_start[2].chains for node in chain.nodes}
    after = {node.node_id: node for chain in state.current_plan.chains for node in chain.nodes}
    assert len(before) == 538 and len(after) == 549
    assert all(after[node_id] is node for node_id, node in before.items())
    assert sum_weights(node.weight for node in after.values()) == D("29693.91")
    assert tuple(chain.chain_id for chain in state.current_plan.chains) == tuple(
        chain.chain_id for chain in fill_start[2].chains
    )


def test_original_nineteen_action_evidence_is_not_replaced_by_the_current_eleven(fill_start):
    frozen = fill_start[0]
    original = frozen["search_rounds"][0]
    fills = tuple(item for item in original["accepted_actions"] if item["phase"] == "virtual_fill")
    assert tuple(item["candidate_checks"] for item in fills) == (
        65619,
        65643,
        65750,
        65856,
        66035,
        66214,
        66474,
        66734,
        67309,
        67884,
        68576,
        69268,
        70041,
        71046,
        72051,
        73137,
        74222,
        75389,
        76556,
    )
    assert tuple(
        item["candidate_checks"]
        for item in original["phase_boundaries"]
        if item["phase"] == "virtual_fill"
    ) == (65594, 77204)
    assert original["final"]["quality"] == [
        1,
        D("670.3"),
        1,
        D("185.49"),
        22,
        D(520),
        D("23460.88"),
    ]
    assert fingerprint(reference_signature(original["final"]["plan"], frozen["node_catalog"])) == (
        "006f13c7f55c1dfe14ab6a1de5e1c9e6373f1652153b9042a1dbb5bf1c8a77fb"
    )
    # Current upstream relocation changed the starting plan; a smaller initial gap
    # does not guarantee a smaller gap after deterministic virtual filling.
    assert D("194.42") > original["final"]["quality"][3]


@pytest.mark.parametrize("limit,accepted", ((81847, 0), (81848, 1)))
def test_budget_boundary_keeps_only_accepted_virtual_numbers(fill_start, limit, accepted):
    state, context = start_filling(fill_start, limit=limit)
    improve_virtual_weight_fill(state, context)
    assert context.factory.budget.candidate_check_count == limit
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert state.accepted_move_count == 24 + accepted
    assert len(context.accepted_move_traces) == accepted
    assert state.virtual_sequence == 7 + accepted and state.split_sequence == 0
    if accepted:
        assert context.accepted_move_traces[0].candidate_check_count == 81848
        assert state.current_evaluation.quality_key == (1, D("670.3"), 2, D("387.25"), 22, D(160))
        assert state.current_plan.chains[12].nodes[0].node_id == "virtual-000008"
    else:
        assert state.current_plan is fill_start[2]
        assert state.current_evaluation is fill_start[3]
