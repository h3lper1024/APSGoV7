"""Contiguous blocks retain their order and can improve beyond single-node edits."""

from dataclasses import replace
from decimal import Decimal
from itertools import groupby

import pytest

from apsgo_scheduler.core import width_optimization
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.model import Chain, SchedulePlan, VirtualPurpose
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.width_optimization import (
    _block_recipes,
    _node_recipes,
    _scan_width_batch,
    _scan_width_families,
    _try_segment_edit,
)
from tests.core.graph.test_construction_order import node
from tests.core.search.test_chain_order import chain, search_case
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.search.test_width_optimization_baseline import width_case
from tests.core.search.test_width_optimization_guard import bind_rules
from tests.core.search.test_width_optimization_nodes import nodes_by_id, period_case

D = Decimal
MOVE = "width_block_move"
EXCHANGE = "width_block_exchange"


def block_case(left, right):
    _, base = width_case()
    chains = tuple(
        Chain(
            name,
            tuple(
                node(f"{name}-{i}", width=str(width), weight=str(weight))
                for i, (width, weight) in enumerate(values)
            ),
            "period",
        )
        for name, values in (("A", left), ("B", right))
    )
    state, context = search_case(chains, active=base.factory.cache.rule_set)
    assert state.current_evaluation.violations == ()
    return state, context


def exchange_case():
    return block_case(
        ((1600, 350), (800, 350)), ((1500, 200), (1300, 175), (1200, 175), (700, 150))
    )


def run_block(state, context, wanted):
    recipe = next(item for item in _block_recipes(state, context) if item == wanted)
    before = context.factory.budget.candidate_check_count
    accepted, exhausted = _scan_width_batch(state, context, iter((recipe,)), _try_segment_edit, 1)
    assert not exhausted
    assert context.factory.budget.candidate_check_count == before + 1
    return accepted


def test_node_only_search_cannot_reach_the_required_two_node_block_exchange():
    state, context = exchange_case()
    before = fingerprint(state)
    _scan_width_families(state, context, (_node_recipes,), _try_segment_edit)
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()
    assert context.complete_candidate_evaluation_count == 0
    assert context.factory.budget.candidate_check_count == 30
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


def test_actual_block_family_finds_one_vs_two_exchange_and_preserves_segment_order():
    state, context = exchange_case()
    original = nodes_by_id(state.current_plan)
    _scan_width_families(state, context, (_block_recipes,), _try_segment_edit)
    assert state.current_evaluation.quality_key == (0, D(0), 0, D(0), D(300), D(0), 2)
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == original
    assert [tuple(node.node_id for node in chain.nodes) for chain in state.current_plan.chains] == [
        ("A-0", "B-1", "B-2"),
        ("B-0", "A-1", "B-3"),
    ]
    assert [chain.total_weight for chain in state.current_plan.chains] == [D(700), D(700)]
    assert context.factory.budget.candidate_check_count == 42
    assert context.complete_candidate_evaluation_count == state.accepted_move_count == 1
    assert context.accepted_move_traces[0].action_name == EXCHANGE
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


def test_actual_two_vs_one_exchange_is_not_limited_to_a_single_node_left_side():
    state, context = block_case(
        ((1600, 200), (1400, 175), (1200, 175), (800, 150)), ((1500, 350), (700, 350))
    )
    before = nodes_by_id(state.current_plan)
    assert run_block(state, context, (EXCHANGE, 0, 1, 1, 3, 0, 1))
    assert state.current_evaluation.quality_key[4] == 600
    assert nodes_by_id(state.current_plan) == before
    assert [tuple(node.width for node in chain.nodes) for chain in state.current_plan.chains] == [
        (D(1600), D(1500), D(800)),
        (D(1400), D(1200), D(700)),
    ]
    assert state.current_evaluation.violations == ()


def test_actual_intact_block_move_updates_both_chains_without_reversal():
    state, context = block_case(
        ((1600, 500), (1200, 500), (900, 350), (800, 350)), ((1500, 500), (1000, 300))
    )
    before = nodes_by_id(state.current_plan)
    moved = state.current_plan.chains[0].nodes[2:]
    assert run_block(state, context, (MOVE, 0, 1, 2, 4, 2, 2))
    assert state.current_plan.chains[1].nodes[-2:] == moved
    assert state.current_evaluation.quality_key[4] == 300
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == before
    assert context.accepted_move_traces[0].action_name == MOVE
    assert state.virtual_sequence == state.split_sequence == 0


def test_block_recipes_are_lazy_shapes_in_length_order_and_never_whole_chains(monkeypatch):
    state, context = block_case(
        ((1600, 300), (1400, 300), (1200, 300), (800, 300)),
        ((1500, 300), (1300, 300), (1100, 300), (900, 300)),
    )
    before = fingerprint(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("block recipes must not perform candidate business work")

    monkeypatch.setattr(VirtualFactory, "bridge", forbidden)
    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", forbidden)
    monkeypatch.setattr(width_optimization, "_width_improves", forbidden)
    recipes = tuple(_block_recipes(state, context))
    assert recipes[:4] == (
        (MOVE, 0, 1, 0, 2, 0, 0),
        (EXCHANGE, 0, 1, 0, 1, 0, 2),
        (MOVE, 0, 1, 0, 2, 1, 1),
        (EXCHANGE, 0, 1, 0, 1, 1, 3),
    )
    moves = [item for item in recipes if item[0] == MOVE]
    exchanges = [item for item in recipes if item[0] == EXCHANGE]
    assert len(moves) == 50
    assert len(exchanges) == 65
    assert len(recipes) == len(set(recipes))
    lengths = [(item[4] - item[3], item[6] - item[5]) for item in exchanges]
    assert [pair for pair, _ in groupby(lengths)] == [
        (1, 2),
        (2, 1),
        (1, 3),
        (2, 2),
        (3, 1),
        (2, 3),
        (3, 2),
        (3, 3),
    ]
    for action, i, j, start, stop, other_start, other_stop in recipes:
        assert i != j and 0 <= start < stop <= 4 and stop - start < 4
        if action == MOVE:
            assert stop - start >= 2 and other_start == other_stop
        else:
            assert i < j and 0 <= other_start < other_stop <= 4 and other_stop - other_start < 4
            assert stop - start + other_stop - other_start >= 3
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before


def test_single_node_chains_do_not_generate_a_disguised_whole_chain_action():
    state, context = search_case((chain("A"), chain("B")))
    assert tuple(_block_recipes(state, context)) == ()
    before = fingerprint(state)
    _scan_width_families(state, context, (_block_recipes,), _try_segment_edit)
    assert fingerprint(state) == before
    assert context.factory.budget.candidate_check_count == 0
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


def virtual_block_case(count):
    state, context = period_case()
    factory = context.factory
    catalog = (prototype("bridge", width="1400"),)
    problem = replace(factory.cache.problem, virtual_prototypes=catalog)
    evaluation_context = replace(factory.cache.context, virtual_prototype_ids=("bridge",))
    context.factory = replace(
        factory, cache=RuleEdgeDecisionCache(problem, factory.cache.rule_set, evaluation_context)
    )
    a, b = state.current_plan.chains
    old = tuple(
        context.factory.materialize(
            catalog[0], a.nodes[0], a.nodes[1], purpose=VirtualPurpose.EDGE_BRIDGE, sequence=i
        )
        for i in range(1, count + 1)
    )
    state.current_plan = SchedulePlan((replace(a, nodes=(a.nodes[0], *old, *a.nodes[1:])), b))
    state.virtual_sequence = count
    bind_rules(state, context, context.factory.cache.rule_set)
    assert state.current_evaluation.violations == ()
    return state, context, old


@pytest.mark.parametrize("count", (1, 2))
def test_virtual_inclusive_real_block_moves_intact_and_normalizes_the_remaining_period(count):
    state, context, old_virtuals = virtual_block_case(count)
    before = nodes_by_id(state.current_plan)
    moved = state.current_plan.chains[0].nodes[1 : count + 2]
    assert run_block(state, context, (MOVE, 0, 1, 1, count + 2, 1, 1))
    assert tuple(chain.chain_id for chain in state.current_plan.chains) == ("B", "A")
    assert tuple(chain.assigned_period for chain in state.current_plan.chains) == (
        "Z-first",
        "A-later",
    )
    assert state.current_plan.chains[0].nodes[1 : count + 2] == moved
    assert state.current_evaluation.quality_key[4] == 500
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == before
    assert all(nodes_by_id(state.current_plan)[node.node_id] is node for node in old_virtuals)
    assert state.virtual_sequence == count


def test_virtual_only_block_is_rejected_after_one_candidate_check():
    state, context, _ = virtual_block_case(2)
    before = fingerprint(state)
    assert not run_block(state, context, (MOVE, 0, 1, 1, 3, 1, 1))
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()
    assert context.complete_candidate_evaluation_count == 0


def test_feasible_interior_block_move_without_width_gain_is_counted_without_bridging(monkeypatch):
    state, context = block_case(
        ((1600, 300), (1400, 300), (1200, 300), (1000, 300), (800, 300)),
        ((1500, 300), (1100, 300), (900, 300)),
    )
    before = fingerprint(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("same endpoint/period boundaries cannot justify bridge construction")

    monkeypatch.setattr(VirtualFactory, "bridge", forbidden)
    assert not run_block(state, context, (MOVE, 0, 1, 1, 3, 1, 1))
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()
    assert context.complete_candidate_evaluation_count == 0
