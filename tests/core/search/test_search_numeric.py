"""Numerical views never change domain precision, identity or acceptance."""

from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest

from apsgo_scheduler.core._search_numeric import PlanNumericView
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core.model import Chain, SchedulePlan
from tests.core.graph.test_construction_order import node
from tests.core.search.test_complete_candidate_lifecycle import setup, merged, attempt
from tests.core.search.test_whole_chain_neighborhood import make_search
from tests.core.test_model_contracts import lineage


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
