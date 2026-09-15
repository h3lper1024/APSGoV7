"""Numerical views never change domain precision, identity or acceptance."""

from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest

from apsgo_scheduler.core._search_numeric import PlanNumericView
from apsgo_scheduler.core._candidate_edit import CandidateEdit
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core.model import Chain, SchedulePlan
from tests.core.graph.test_construction_order import node
from tests.core.search.test_complete_candidate_lifecycle import setup, merged, attempt
from tests.core.search.test_whole_chain_neighborhood import make_search
from tests.core.test_model_contracts import lineage
from tests.core.search.test_width_optimization_baseline import width_case
from apsgo_scheduler.core import width_optimization


def test_columns_positions_and_exact_values():
    state, context = make_search((Chain("one", (
        node("a", width=None), node("b", width="1e999", weight="1.000000000000000000000000000001")), "period"),))
    before = fingerprint(state)
    view = context.computation_view(state)
    columns = view.catalog.columns
    assert columns.physical.dtype == np.float64 and columns.physical.flags.c_contiguous
    assert not columns.present[0, 0] and columns.present[0, 1]
    assert not columns.finite[0].any()
    assert columns.weights[1] == Decimal("1.000000000000000000000000000001")
    assert view.offsets.tolist() == [0, 2]
    assert view.node_rows.tolist() == [0, 1]
    assert view.node_positions == {"a": (0, 0), "b": (0, 1)}
    assert view.source_positions["b"] == ((0, 1),)
    assert context.computation_view(state) is view and context.numeric_view_build_count == 1
    assert fingerprint(state) == before
    for array in (columns.physical, columns.present, columns.finite, columns.roles, view.node_rows, view.offsets):
        with pytest.raises(ValueError):
            array.flat[0] = 1
    with pytest.raises(TypeError):
        view.node_positions["a"] = (9, 9)
    for row in (-1, True, 2, 2**100):
        with pytest.raises(ValueError):
            view.node_at_row(row)


def test_rejection_and_precommit_failure_keep_original_view(monkeypatch):
    state, context = setup()
    view = context.computation_view(state)
    before = fingerprint(state)
    assert not attempt(state, context, state.current_plan.chains)
    assert context._numeric_view is view and fingerprint(state) == before
    def fail(*args):
        raise RuntimeError("view allocation failed")
    monkeypatch.setattr(type(context), "prepare_numeric_view", fail)
    with pytest.raises(RuntimeError, match="allocation failed"):
        attempt(state, context, merged(state))
    assert context._numeric_view is view and fingerprint(state) == before


def test_acceptance_and_changed_context_invalidate_by_actual_plan():
    state, context = setup()
    view = context.computation_view(state)
    assert attempt(state, context, merged(state))
    accepted = context.computation_view(state)
    assert accepted.plan is state.current_plan and accepted.generation == 1
    assert accepted.catalog is view.catalog and not view.matches(state, context.factory.cache)
    original = state.current_plan
    first = original.chains[-1]
    state.current_plan = SchedulePlan((replace(first, nodes=tuple(reversed(first.nodes))), *original.chains[:-1]))
    changed = context.computation_view(state)
    assert changed.plan is state.current_plan and changed is not accepted
    context.factory = replace(context.factory, cache=replace(context.factory.cache))
    assert context.computation_view(state).catalog is not changed.catalog


def test_all_split_fragments_and_temporary_rows_are_isolated():
    state, context = setup((("parent",),))
    view = context.computation_view(state)
    parent = state.current_plan.chains[0].nodes[0]
    ancestry = lineage(parent_node_id=parent.node_id, parent_source_order_id=parent.source_order_id,
                       source_resource_id=parent.source_resource_id, source_period=parent.source_period,
                       origin_assigned_period=parent.source_period, target_assigned_period=parent.source_period,
                       parent_weight=parent.weight)
    pieces = tuple(replace(parent, node_id=f"piece-{i}", weight=parent.weight / 2,
                           split_lineage=replace(ancestry, piece_index=i)) for i in (1, 2))
    plan = SchedulePlan((Chain("left", pieces[:1], "period"), Chain("right", pieces[1:], "period")))
    candidate = PlanNumericView.build(view.catalog, plan, 1)
    assert candidate.source_positions[parent.source_order_id] == ((0, 0), (1, 0))
    assert candidate.dynamic.nodes == pieces
    assert tuple(candidate.node_at_row(int(row)) for row in candidate.node_rows) == pieces
    assert view.dynamic.nodes == () and context._numeric_view is view
    assert view.source_positions[parent.source_order_id] == ((0, 0),)


def original_slots(node, target, positions, context):
    """Frozen pre-refactor cursor, including its three cancellation polls per slot."""
    cache, budget = context.factory.cache, context.factory.budget
    deferred = []
    for slot in positions:
        if not budget.allows_search():
            return
        left = slot == 0 or cache.allows(target[slot - 1], node)
        if not budget.allows_search():
            return
        direct = left and (slot == len(target) or cache.allows(node, target[slot]))
        if not budget.allows_search():
            return
        if direct:
            yield slot
        else:
            deferred.append(slot)
    for slot in deferred:
        if not budget.allows_search():
            return
        yield slot


@pytest.mark.parametrize("warm", (False, True))
def test_cached_batch_keeps_direct_deferred_order_and_cancel_polls(warm, monkeypatch):
    from apsgo_scheduler.core.budget import SolveRuntimeBudget
    from apsgo_scheduler.core._search_numeric import direct_insertion_flags

    for stop_at in range(1, 15):
        results = []
        for query in (original_slots, width_optimization._direct_insertion_slots):
            state, context = width_case()
            inserted = state.current_plan.chains[0].nodes[1]
            target = state.current_plan.chains[1].nodes
            cache = context.factory.cache
            if warm:
                for item in target:
                    cache.allows(item, inserted)
                    cache.allows(inserted, item)
            entries = cache.entry_count
            direct_insertion_flags(cache, inserted, target)
            assert cache.entry_count == entries  # no speculative evaluation of missing edges
            calls = []
            def poll(self):
                calls.append(1)
                return len(calls) < stop_at
            with monkeypatch.context() as patch:
                patch.setattr(SolveRuntimeBudget, "allows_search", poll)
                result = tuple(query(inserted, target, (2, 0, 1), context))
            results.append((result, len(calls)))
            assert all(type(part) is int for key in cache._entries for part in key)
        assert results[0] == results[1]


def test_bound_recipes_restore_same_candidate_and_validate_stale_or_false_anchors():
    state, context = width_case()
    view = context.computation_view(state)
    generators = (width_optimization._node_recipes, width_optimization._block_recipes,
                  width_optimization._order_recipes)
    for generator in generators:
        for recipe in generator(state, context):
            edit = CandidateEdit.bind(view, recipe, 1, "width_optimization")
            assert edit.restore(state, context.factory.cache, 1, "width_optimization") == recipe
            with pytest.raises(ValueError, match="stale"):
                edit.restore(state, context.factory.cache, 2, "width_optimization")
            with pytest.raises(ValueError, match="differ"):
                replace(edit, anchors=()).restore(state, context.factory.cache, 1, "width_optimization")
            outcomes = []
            for described in (False, True):
                current, bound = width_case()
                bound.factory.budget.consume_candidate_check()
                handler = (width_optimization._try_chain_order if recipe[0] == "width_chain_order_relocation"
                           else width_optimization._try_segment_edit)
                accepted = (width_optimization._try_width_recipe(current, bound, recipe) if described
                            else handler(current, bound, recipe))
                outcomes.append((accepted, fingerprint(current), bound.accepted_move_traces,
                                 bound.complete_candidate_evaluation_count, bound.factory.budget.candidate_check_count))
            assert outcomes[0] == outcomes[1]
    edit = CandidateEdit.bind(view, ("width_chain_order_relocation", 0, 1), 1, "width_optimization")
    state.current_plan = SchedulePlan(tuple(reversed(state.current_plan.chains)))
    with pytest.raises(ValueError, match="stale"):
        edit.restore(state, context.factory.cache, 1, "width_optimization")


@pytest.mark.parametrize("recipe", (
    ("width_node_move", 0, 0, 0, 1, 1, 1),
    ("width_node_move", 0, 1, 0, 99, 0, 0),
    ("width_node_move", 0, 1, 0, 1, 0, 1),
    ("width_node_exchange", 0, 1, 0, 1, 0, 0),
    ("width_block_move", -1, 1, 0, 1, 0, 0),
    ("width_chain_order_relocation", 0, 0),
    ("width_chain_order_relocation", 0, 2**100),
    ("delivery_intra_move", 0, 1, 1),
))
def test_invalid_recipe_ranges_are_not_trusted(recipe):
    state, context = width_case()
    with pytest.raises(ValueError):
        CandidateEdit.bind(context.computation_view(state), recipe, 1, "width_optimization")
