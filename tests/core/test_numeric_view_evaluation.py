"""Formal and base/private candidates use one rule and scoring implementation."""

from tests.core import numeric_reference_resources as reference_resources
from dataclasses import replace

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_evaluation as evaluation
from apsgo_scheduler.core import _numeric_resources as resources
from apsgo_scheduler.core import _numeric_chain_ops as chains
from apsgo_scheduler.core._numeric_state import NumericCandidateWorkspace, NumericPlan, OK
from apsgo_scheduler.core._numeric_units import NumericValueError
from tests.core.test_numeric_construction import construction_case
from tests.core.test_numeric_private_resources import case
from tests.core.test_numeric_private_split import split_case, formal_split


def assert_same(actual, expected, *, detail=False):
    for name in ("status", "quality", "facts", "scores", "hits", "totals", "ends",
                 "completion", "late", "waits", "event_counts"):
        np.testing.assert_array_equal(getattr(actual, name), getattr(expected, name), err_msg=name)
    np.testing.assert_array_equal(actual.counts[:2], expected.counts[:2])
    if detail:
        for name, size in zip(("violations", "metrics"), actual.counts[:2]):
            np.testing.assert_array_equal(getattr(actual, name)[:size], getattr(expected, name)[:size], err_msg=name)


def set_changed_chain(workspace, rows, *, period=0):
    workspace.changed_rows[:len(rows)] = rows
    workspace.changed_count = len(rows)
    workspace.chain_count = 1
    workspace.starts[0], workspace.stops[0], workspace.private[0] = 0, len(rows), True
    workspace.ids[0], workspace.periods[0] = 10, period


def test_formal_zero_change_and_two_changed_chains_reuse_the_same_native_rules():
    task, program, quality = construction_case(weights=("100", "200", "300", "100"),
                                               widths=("1000", "990", "980", "970"))
    plan = NumericPlan.build(task, (0, 1, 2, 3), (0, 2, 3, 4), (10, 20, 30), (0, 0, 0))
    previous = evaluation.evaluate_numeric_plan(task, program, quality, plan)
    workspace = NumericCandidateWorkspace.allocate(task, plan, changed_capacity=16,
        chain_capacity=4, node_capacity=4, group_capacity=1, event_capacity=4)
    for detail in (False, True):
        actual = evaluation.evaluate_numeric_view(workspace, program, quality,
                                                  previous_evaluation=previous, detail=detail)
        assert_same(actual, previous.kernel_result, detail=detail)
        if not detail:
            assert actual.counts[2] == 0
    status, workspace.changed_count = chains.move_span(workspace.view(), 0, 1, 1, 2, 0, 0)
    assert status == OK
    target = NumericPlan.build(task, (0, 1, 2, 3), (0, 1, 3, 4), (10, 20, 30), (0, 0, 0))
    expected = evaluation.evaluate_numeric_plan(task, program, quality, target).kernel_result
    for detail in (False, True):
        actual = evaluation.evaluate_numeric_view(workspace, program, quality,
                                                  previous_evaluation=previous, detail=detail)
        assert_same(actual, expected, detail=detail)
        if not detail:
            assert actual.counts[2] == 2


@pytest.mark.parametrize("widths,prototypes", ((('1000', '400'), ('800', '600')),
                                              (('1000', '600'), ('800',))))
def test_virtual_private_tail_matches_materialized_summary_and_every_detail(widths, prototypes):
    workspace, program, quality = case(widths, prototypes)
    formal = reference_resources.choose_virtual_bridge(workspace.task, program, quality, 0, 1,
                                               max_nodes=2, first_sequence=1)
    status, connected, rows = resources.prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=2, first_sequence=1)
    assert status == OK and connected
    order = (0, *rows, 1)
    set_changed_chain(workspace, order)
    plan = NumericPlan.build(formal.task, order, (0, len(order)), (10,), (0,))
    expected = evaluation.evaluate_numeric_plan(formal.task, formal.program, formal.quality, plan).kernel_result
    for detail in (False, True):
        actual = evaluation.evaluate_numeric_view(workspace, program, quality, detail=detail)
        assert_same(actual, expected, detail=detail)


@pytest.mark.parametrize("period", ("P0", "P1"))
def test_private_split_clock_original_completion_and_rule_details_match(period):
    workspace, program, quality, decision = split_case("600", period)
    formal = formal_split(workspace, program, quality, decision)
    status, created, rows = resources.prepare_private_split(workspace, 0, decision, 0, sequence=1)
    assert status == OK and created
    left, right = map(int, rows)
    formal = reference_resources.choose_split_separator(formal.task, formal.program, formal.quality,
                                               left, right, sequence=1, group_index=0)
    status, found, separator = resources.prepare_private_separator(workspace, program, left, right,
        sequence=1, group_index=0)
    assert status == OK and found
    order = (left, int(separator[0]), right)
    set_changed_chain(workspace, order, period=decision.target_period)
    plan = NumericPlan.build(formal.task, order, (0, len(order)), (10,), (decision.target_period,))
    expected = evaluation.evaluate_numeric_plan(formal.task, formal.program, formal.quality, plan).kernel_result
    assert_same(evaluation.evaluate_numeric_view(workspace, program, quality, detail=True), expected, detail=True)


def test_view_boundary_rejects_stale_cache_uninitialized_rows_and_unknown_period():
    workspace, program, quality = case()
    view = workspace.view()
    previous = evaluation.evaluate_numeric_plan(workspace.task, program, quality, workspace.plan)
    workspace.reset()
    with pytest.raises(NumericValueError, match="expired"):
        evaluation.evaluate_numeric_view(workspace, program, quality, view=view)
    with pytest.raises(NumericValueError, match="foreign"):
        evaluation.evaluate_numeric_view(workspace, program, quality,
            view=workspace.view()._replace(base_rows=workspace.plan.node_rows.copy()))
    with pytest.raises(NumericValueError, match="previous"):
        evaluation.evaluate_numeric_view(workspace, program, quality,
            previous_evaluation=replace(previous, plan_fingerprint="other"))
    workspace.private[0] = True
    with pytest.raises(NumericValueError, match="invalid numeric"):
        evaluation.evaluate_numeric_view(workspace, program, quality)
    workspace.reset()
    workspace.periods[0] = len(workspace.task.period_ids)
    with pytest.raises(NumericValueError, match="period"):
        evaluation.evaluate_numeric_view(workspace, program, quality)


def test_view_evaluation_does_not_flatten_or_create_formal_candidates(monkeypatch):
    workspace, program, quality = case()
    expected = evaluation.evaluate_numeric_plan(workspace.task, program, quality, workspace.plan).kernel_result

    def forbidden(*args, **kwargs):
        raise AssertionError("formal plan/task/flattening called by view evaluation")

    monkeypatch.setattr(evaluation, "_kernel_inputs", forbidden)
    monkeypatch.setattr(NumericPlan, "build", forbidden)
    monkeypatch.setattr(reference_resources, "extend_resource_workspace", forbidden)
    actual = evaluation.evaluate_numeric_view(workspace, program, quality, detail=True)
    assert_same(actual, expected, detail=True)
    from apsgo_scheduler.core._numeric_kernel import evaluate_view_kernel
    assert evaluate_view_kernel.nopython_signatures


def test_private_column_views_borrow_base_and_initialized_tail_arrays():
    from apsgo_scheduler.core._numeric_kernel import private_task_columns, task_columns
    workspace, program, _ = case()
    status, connected, _ = resources.prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=2, first_sequence=1)
    assert status == OK and connected
    columns = private_task_columns(task_columns(workspace.task), workspace.nodes,
                                    workspace.derived, workspace.node_count)
    assert np.shares_memory(columns.weight[0], workspace.task.nodes.weight)
    assert np.shares_memory(columns.weight[1], workspace.nodes.weight)
    assert columns.weight[1].size == workspace.node_count
    assert np.shares_memory(columns.present[1], workspace.nodes.present)
    assert np.shares_memory(columns.priority[0], workspace.task.priority) or columns.priority[0].size == 0
