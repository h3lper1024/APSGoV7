"""A chain cut jointly places intact parts without creating order partitions."""

from dataclasses import replace
from decimal import Decimal
from itertools import permutations

import pytest

from apsgo_scheduler.core import width_optimization
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import SchedulePlan, VirtualPurpose
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.width_optimization import (
    _cut_recipes,
    _scan_width_batch,
    _scan_width_families,
    _try_chain_cut,
)
from tests.core.search.test_chain_order import chain, search_case
from tests.core.search.test_complete_candidate_lifecycle import Stop
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.search.test_width_optimization_baseline import width_case
from tests.core.search.test_width_optimization_blocks import block_case
from tests.core.search.test_width_optimization_guard import bind_rules
from tests.core.search.test_width_optimization_nodes import nodes_by_id, split_case

D = Decimal
CUT = "width_chain_cut"


def run_cut(state, context, wanted):
    recipe = next(item for item in _cut_recipes(state, context) if item == wanted)
    before = context.factory.budget.candidate_check_count
    accepted, exhausted = _scan_width_batch(state, context, iter((recipe,)), _try_chain_cut, 1)
    assert not exhausted
    assert context.factory.budget.candidate_check_count == before + 1
    return accepted


def forbid_virtual_creation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("cutting a chain must not create bridges or new order pieces")

    monkeypatch.setattr(VirtualFactory, "bridge", forbidden)
    monkeypatch.setattr(VirtualFactory, "materialize", forbidden)


def test_actual_cut_scan_improves_with_joint_placement_and_more_chains(monkeypatch):
    state, context = width_case()
    old = state.current_plan
    forbid_virtual_creation(monkeypatch)
    assert _scan_width_families(state, context, (_cut_recipes,), _try_chain_cut) is state
    assert state.current_evaluation.quality_key == (0, D(0), 0, D(0), D(400), D(0), 3)
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == nodes_by_id(old)
    assert [item.nodes for item in state.current_plan.chains] == [
        old.chains[0].nodes[:2],
        old.chains[1].nodes,
        old.chains[0].nodes[2:],
    ]
    assert state.current_plan.chains[1] is old.chains[1]
    assert state.current_evaluation == evaluate_plan(
        state.current_plan, context.factory.cache.rule_set, context.factory.cache.context
    )
    assert context.factory.budget.candidate_check_count == 32
    assert context.complete_candidate_evaluation_count == state.accepted_move_count == 1
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert state.virtual_sequence == state.split_sequence == 0
    assert state.accepted_same_period_split_count == state.accepted_future_borrow_return_count == 0
    (trace,) = context.accepted_move_traces
    assert trace.action_name == CUT and trace.affected_chain_ids == ("A",)
    assert trace.candidate_check_count == 8


def test_cut_in_original_location_is_rejected_before_joint_placement_is_accepted(monkeypatch):
    state, context = width_case()
    before = fingerprint(state)
    forbid_virtual_creation(monkeypatch)
    assert not run_cut(state, context, (CUT, 0, 2, 0, 1))
    assert fingerprint(state) == before
    assert context.complete_candidate_evaluation_count == 0
    assert context.accepted_move_traces == ()
    assert run_cut(state, context, (CUT, 0, 2, 0, 2))
    assert state.current_evaluation.quality_key[4] == 400
    assert context.factory.budget.candidate_check_count == 2
    assert context.complete_candidate_evaluation_count == 1


def test_recipe_stream_keeps_all_distinct_raw_slots_without_business_checks(monkeypatch):
    state, context = width_case()
    before = fingerprint(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("raw cut recipes must not build or reject candidate parts")

    for name in ("_normalize_chain", "_has_real", "_weight_rejects", "_width_improves"):
        monkeypatch.setattr(width_optimization, name, forbidden)
    forbid_virtual_creation(monkeypatch)
    recipes = tuple(_cut_recipes(state, context))
    assert recipes == tuple(
        (CUT, source, cut, prefix, suffix)
        for source, cut in ((0, 1), (0, 2), (1, 1))
        for prefix, suffix in permutations(range(3), 2)
    )
    assert len(recipes) == len(set(recipes)) == 18
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before


def test_single_node_chains_have_no_cut_descriptions_or_debit():
    _, base = width_case()
    state, context = search_case((chain("A"), chain("B")), active=base.factory.cache.rule_set)
    assert tuple(_cut_recipes(state, context)) == ()
    _scan_width_families(state, context, (_cut_recipes,), _try_chain_cut)
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


def test_same_period_suffix_can_precede_prefix_without_reversing_either_part():
    state, context = width_case()
    a, b = state.current_plan.chains
    b = replace(b, nodes=(replace(b.first_node, width=D(1800)), b.last_node))
    state, context = search_case((a, b), active=context.factory.cache.rule_set)
    old = nodes_by_id(state.current_plan)
    assert state.current_evaluation.quality_key[4] == 1000
    assert run_cut(state, context, (CUT, 0, 2, 2, 1))
    assert [item.nodes for item in state.current_plan.chains] == [b.nodes, a.nodes[2:], a.nodes[:2]]
    assert state.current_evaluation.quality_key[4] == 900
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == old


def future_suffix_case():
    state, context = width_case()
    periods = ("Z-first", "M-empty", "A-later")
    a, b = state.current_plan.chains
    a = replace(
        a,
        assigned_period=periods[0],
        nodes=tuple(
            replace(item, source_period=periods[-1] if index == 2 else periods[0])
            for index, item in enumerate(a.nodes)
        ),
    )
    b = replace(
        b,
        assigned_period=periods[0],
        nodes=tuple(replace(item, source_period=periods[0]) for item in b.nodes),
    )
    return search_case((a, b), active=context.factory.cache.rule_set, periods=periods)


def test_future_only_suffix_uses_source_period_and_illegal_raw_slots_are_not_grouped(monkeypatch):
    state, context = future_suffix_case()
    old = state.current_plan
    before = fingerprint(state)
    original = width_optimization._width_improves
    calls = []

    def record(*args):
        calls.append(context.factory.budget.candidate_check_count)
        return original(*args)

    monkeypatch.setattr(width_optimization, "_width_improves", record)
    # Sorting this illegal late/early/early placement would hide it as the valid one below.
    assert not run_cut(state, context, (CUT, 0, 2, 1, 0))
    assert fingerprint(state) == before
    assert calls == [] and context.complete_candidate_evaluation_count == 0
    assert run_cut(state, context, (CUT, 0, 2, 0, 2))
    assert calls == [2]
    assert [item.assigned_period for item in state.current_plan.chains] == [
        "Z-first",
        "Z-first",
        "A-later",
    ]
    assert [item.nodes for item in state.current_plan.chains] == [
        old.chains[0].nodes[:2],
        old.chains[1].nodes,
        old.chains[0].nodes[2:],
    ]
    assert state.current_evaluation.quality_key[4] == 400
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == nodes_by_id(old)


def virtual_cut_case(position="interior"):
    state, context = block_case(((1600, 700), (1300, 500), (800, 700)), ((1500, 500), (900, 300)))
    factory = context.factory
    catalog = (prototype("bridge", width="800" if position == "tail" else "1500"),)
    problem = replace(factory.cache.problem, virtual_prototypes=catalog)
    evaluation_context = replace(factory.cache.context, virtual_prototype_ids=("bridge",))
    context.factory = replace(
        factory, cache=RuleEdgeDecisionCache(problem, factory.cache.rule_set, evaluation_context)
    )
    a, b = state.current_plan.chains
    left, right = (a.last_node, a.last_node) if position == "tail" else a.nodes[:2]
    virtual = context.factory.materialize(
        catalog[0], left, right, purpose=VirtualPurpose.EDGE_BRIDGE, sequence=1
    )
    index = {"head": 0, "interior": 1, "tail": len(a.nodes)}[position]
    a = replace(a, nodes=(*a.nodes[:index], virtual, *a.nodes[index:]))
    state.current_plan = SchedulePlan((a, b))
    state.virtual_sequence = 1
    bind_rules(state, context, context.factory.cache.rule_set)
    assert state.current_evaluation.violations == ()
    return state, context


def test_old_virtual_is_retained_as_actual_part_endpoint_without_new_material(monkeypatch):
    state, context = virtual_cut_case()
    old = state.current_plan
    forbid_virtual_creation(monkeypatch)
    assert run_cut(state, context, (CUT, 0, 2, 0, 2))
    assert state.current_plan.chains[0].last_node is old.chains[0].nodes[1]
    assert state.current_plan.chains[0].total_weight == 720
    assert state.current_evaluation.quality_key == (0, D(0), 0, D(0), D(400), D(20), 3)
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == nodes_by_id(old)
    assert state.virtual_sequence == 1 and state.split_sequence == 0


@pytest.mark.parametrize("position,cut", (("head", 1), ("tail", 3)))
def test_pure_virtual_part_is_rejected_after_one_debit_before_normalization(
    monkeypatch, position, cut
):
    state, context = virtual_cut_case(position)
    before = fingerprint(state)

    def forbidden(*args):
        raise AssertionError("a part without real material must not be normalized")

    monkeypatch.setattr(width_optimization, "_normalize_chain", forbidden)
    assert not run_cut(state, context, (CUT, 0, cut, 0, 2))
    assert fingerprint(state) == before
    assert context.complete_candidate_evaluation_count == 0
    assert context.accepted_move_traces == ()


def test_underweight_part_is_not_topped_up_and_checks_happen_after_debit(monkeypatch):
    state, context = width_case()
    before = fingerprint(state)
    original = width_optimization._weight_rejects
    weights = []

    def record(part, bound, **options):
        assert bound.factory.budget.candidate_check_count == 1
        weights.append(part.total_weight)
        return original(part, bound, **options)

    forbid_virtual_creation(monkeypatch)
    monkeypatch.setattr(width_optimization, "_weight_rejects", record)
    assert not run_cut(state, context, (CUT, 0, 1, 0, 2))
    assert weights == [D(500)]
    assert fingerprint(state) == before
    assert context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize("collisions", (0, 1, 2))
def test_suffix_id_is_deterministic_and_avoids_existing_chain_ids(collisions):
    outcomes = []
    for _ in range(2):
        state, context = width_case()
        a, b = state.current_plan.chains
        suffix_id = "width-cut-" + fingerprint(
            (CUT, a.chain_id, 2, tuple(n.node_id for n in a.nodes))
        )
        chains = [a, replace(b, chain_id=suffix_id) if collisions else b]
        if collisions == 2:
            chains.append(chain("_" + suffix_id, width=900))
        state, context = search_case(chains, active=context.factory.cache.rule_set)
        old = nodes_by_id(state.current_plan)
        assert run_cut(state, context, (CUT, 0, 2, 0, len(chains)))
        assert state.current_plan.chains[0].chain_id == a.chain_id
        assert state.current_plan.chains[-1].chain_id == "_" * collisions + suffix_id
        assert nodes_by_id(state.current_plan) == old
        assert len({item.chain_id for item in state.current_plan.chains}) == len(chains) + 1
        assert state.virtual_sequence == state.split_sequence == 0
        outcomes.append(fingerprint(state.current_plan))
    assert outcomes[0] == outcomes[1]


def test_existing_order_pieces_keep_lineage_and_period_locks_without_new_split(monkeypatch):
    state, context = split_case()
    old = nodes_by_id(state.current_plan)
    forbid_virtual_creation(monkeypatch)
    assert run_cut(state, context, (CUT, 1, 2, 2, 3))
    assert state.current_evaluation.quality_key[4] == 1200
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == old
    for item in state.current_plan.chains:
        for member in item.nodes:
            if member.split_lineage is not None:
                assert item.assigned_period == member.split_lineage.target_assigned_period
    assert state.split_sequence == state.accepted_same_period_split_count == 1
    assert state.accepted_future_borrow_return_count == state.virtual_sequence == 0
    assert context.accepted_move_traces[0].affected_chain_ids == ("A",)


@pytest.mark.parametrize("kind", ("cancel", "time", "error"))
def test_interruption_while_building_cut_parts_never_publishes_partial_plan(monkeypatch, kind):
    state, context = width_case()
    stop = Stop()
    context.factory.budget.cancellation = stop
    before = fingerprint(state)
    original = width_optimization._normalize_chain

    def interrupted(*args):
        assert context.factory.budget.candidate_check_count == 1
        result = original(*args)
        if kind == "error":
            raise RuntimeError("deliberate cut normalization failure")
        if kind == "time":
            context.factory.budget.clock = lambda: context.factory.budget.search_deadline_monotonic
        else:
            stop.active = True
        return result

    monkeypatch.setattr(width_optimization, "_normalize_chain", interrupted)
    if kind == "error":
        with pytest.raises(RuntimeError, match="deliberate cut normalization failure"):
            run_cut(state, context, (CUT, 0, 2, 0, 2))
    else:
        assert not run_cut(state, context, (CUT, 0, 2, 0, 2))
        assert context.factory.budget.stop_reason is (
            SearchStopReason.SEARCH_TIME_LIMIT_REACHED
            if kind == "time"
            else SearchStopReason.USER_CANCELLED
        )
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()


@pytest.mark.parametrize("reason", tuple(SearchStopReason))
def test_stopped_cut_builder_does_no_construction_and_never_clears_reason(monkeypatch, reason):
    state, context = width_case()
    before = fingerprint(state)
    context.factory.budget.stop_reason = reason

    def forbidden(*args):
        raise AssertionError("stopped cut must not begin construction")

    monkeypatch.setattr(width_optimization, "_has_real", forbidden)
    assert not _try_chain_cut(state, context, (CUT, 0, 2, 0, 2))
    assert context.factory.budget.stop_reason is reason
    assert context.factory.budget.candidate_check_count == 0
    assert fingerprint(state) == before


def test_last_check_is_available_but_no_cut_construction_occurs_after_quota(monkeypatch):
    state, context = width_case()
    context.factory.budget.candidate_check_limit = 1
    context.policy = replace(context.policy, candidate_check_limit=1)
    assert run_cut(state, context, (CUT, 0, 2, 0, 2))
    before = fingerprint(state)

    def forbidden(*args):
        raise AssertionError("no remaining check means no next candidate construction")

    assert _scan_width_batch(state, context, iter(((CUT, 0, 1, 0, 2),)), forbidden, 1) == (
        False,
        False,
    )
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 1
    assert fingerprint(state) == before
