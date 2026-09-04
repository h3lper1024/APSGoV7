"""Width-search counterexamples, not evidence of a new candidate generator.

The design sample uses real weight, reverse-width and boundary-width rules.
It is a synthetic search fixture, not the complete GQGA4 rule set or acceptance run.
"""

from dataclasses import replace
from decimal import Decimal

from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import SchedulePlan
from apsgo_scheduler.core.neighborhoods import (
    improve_real_node_relocation,
    run_local_search,
    try_complete_candidate,
)
from tests.core.graph.test_construction_order import node
from tests.core.rules.test_inter_chain_width_gap import with_gap
from tests.core.rules.test_width_transition import rule as width_rule
from tests.core.search.test_chain_order import search_case
from tests.core.search.test_whole_chain_neighborhood import chain
from tests.core.test_quality_key import ruleset

D = Decimal


def width_case():
    active = with_gap(ruleset((width_rule(),)))
    old = active.quality_spec
    quality = (*old[:4], old[6], old[5], old[4])
    active = replace(active, quality_spec=quality, fingerprint=fingerprint((active.rules, quality)))
    chains = (
        chain(
            "A",
            node("a-head", width="1600", weight="500"),
            node("a-middle", width="1200", weight="500"),
            node("a-tail", width="800", weight="700"),
        ),
        chain(
            "B",
            node("b-head", width="1500", weight="500"),
            node("b-tail", width="900", weight="300"),
        ),
    )
    return search_case(chains, active=active)


def assert_feasible_quality(plan, context, gap, count):
    evaluation = evaluate_plan(plan, context.factory.cache.rule_set, context.factory.cache.context)
    assert evaluation.violations == ()
    assert evaluation.quality_key == (0, D(0), 0, D(0), D(gap), D(0), count)
    assert all(D(700) <= item.total_weight <= D(2000) for item in plan.chains)
    return evaluation


def assert_preserved_nodes(before, after):
    old = tuple(node for chain in before.chains for node in chain.nodes)
    new = tuple(node for chain in after.chains for node in chain.nodes)
    assert len(new) == len({item.node_id for item in new}) == len(old) == 5
    assert sorted(new, key=lambda item: item.node_id) == sorted(old, key=lambda item: item.node_id)
    assert sum((item.weight for item in new), D(0)) == D(2500)


def test_legacy_fixed_search_cannot_generate_a_width_improvement_from_the_design_sample():
    state, context = width_case()
    before = fingerprint(state)
    assert_feasible_quality(state.current_plan, context, 700, 2)

    # The legacy sequence remains a separately testable stage after V7 adds its new phase.
    assert run_local_search(state, context) is state

    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert context.factory.budget.candidate_check_count < context.factory.budget.candidate_check_limit
    assert context.complete_candidate_evaluation_count > 0
    assert_feasible_quality(state.current_plan, context, 700, 2)


def test_manual_cut_requires_joint_placement_and_accepts_more_chains_without_splitting_orders():
    state, context = width_case()
    before = state.current_plan
    a, b = before.chains
    prefix = replace(a, nodes=a.nodes[:2])
    suffix = replace(a, chain_id="A-suffix-test", nodes=a.nodes[2:])
    cut_only = SchedulePlan((prefix, suffix, b))
    joint = SchedulePlan((prefix, b, suffix))
    assert_feasible_quality(before, context, 700, 2)
    assert_feasible_quality(cut_only, context, 1100, 3)
    assert_feasible_quality(joint, context, 400, 3)
    assert_preserved_nodes(before, cut_only)
    assert_preserved_nodes(before, joint)

    unchanged = fingerprint(state)
    assert context.factory.budget.consume_candidate_check()
    assert not try_complete_candidate(
        state,
        context,
        cut_only.chains,
        affected_chain_ids=("A",),
        virtual_sequence=0,
        action_name="manual_cut_baseline_probe",
    )
    assert fingerprint(state) == unchanged
    assert context.accepted_move_traces == ()

    assert context.factory.budget.consume_candidate_check()
    assert try_complete_candidate(
        state,
        context,
        joint.chains,
        affected_chain_ids=("A",),
        virtual_sequence=0,
        action_name="manual_cut_joint_placement_baseline_probe",
    )
    assert state.current_plan == joint
    assert state.current_evaluation == assert_feasible_quality(joint, context, 400, 3)
    assert state.virtual_sequence == state.split_sequence == 0
    assert state.accepted_same_period_split_count == state.accepted_future_borrow_return_count == 0
    assert state.accepted_move_count == len(context.accepted_move_traces) == 1
    trace = context.accepted_move_traces[0]
    assert trace.quality_before == (0, D(0), 0, D(0), D(700), D(0), 2)
    assert trace.quality_after == (0, D(0), 0, D(0), D(400), D(0), 3)
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 2
    )


def test_zero_underweight_blocks_legacy_relocation_but_not_an_improving_complete_candidate():
    state, context = width_case()
    before = state.current_plan
    unchanged = fingerprint(state)
    assert_feasible_quality(before, context, 700, 2)

    assert improve_real_node_relocation(state, context) is state
    assert fingerprint(state) == unchanged
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 0
    )
    assert context.accepted_move_traces == ()
    assert context.factory.budget.stop_reason is None

    a, b = before.chains
    candidate = SchedulePlan(
        (replace(a, nodes=a.nodes[:-1]), replace(b, nodes=(*b.nodes, a.nodes[-1])))
    )
    assert_feasible_quality(candidate, context, 300, 2)
    assert_preserved_nodes(before, candidate)
    assert context.factory.budget.consume_candidate_check()
    assert try_complete_candidate(
        state,
        context,
        candidate.chains,
        affected_chain_ids=("A", "B"),
        virtual_sequence=0,
        action_name="manual_relocation_baseline_probe",
    )
    assert state.current_plan == candidate
    assert state.current_evaluation == assert_feasible_quality(candidate, context, 300, 2)
    assert state.virtual_sequence == state.split_sequence == 0
    assert state.accepted_same_period_split_count == state.accepted_future_borrow_return_count == 0
    assert state.accepted_move_count == len(context.accepted_move_traces) == 1
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 1
    )
