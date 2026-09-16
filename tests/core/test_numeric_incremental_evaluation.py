"""Incremental numeric evaluation must be identical to authoritative full evaluation."""

from dataclasses import replace

import numpy as np
import pytest

import apsgo_scheduler.core._numeric_evaluation as evaluation_module
from apsgo_scheduler.core._numeric_evaluation import (
    evaluate_numeric_candidate,
    evaluate_numeric_plan,
)
from apsgo_scheduler.core._numeric_resources import extend_resource_workspace, virtual_node
from apsgo_scheduler.core._numeric_state import NumericPlan, readonly
from apsgo_scheduler.core._numeric_units import NumericValueError
from apsgo_scheduler.core.model import VirtualPurpose
from tests.core.test_numeric_construction import construction_case


def _assert_same(left, right):
    assert left.chain_results == right.chain_results
    assert left.plan_result == right.plan_result
    assert left.node_metrics == right.node_metrics
    assert left.violations == right.violations
    assert (
        left.scheduled_real_weight,
        left.generated_virtual_weight,
        left.borrowed_future_weight,
    ) == (
        right.scheduled_real_weight,
        right.generated_virtual_weight,
        right.borrowed_future_weight,
    )
    for name in (
        "node_end_ms",
        "original_completion_ms",
        "newly_late",
        "wait_seconds",
    ):
        np.testing.assert_array_equal(getattr(left.delivery, name), getattr(right.delivery, name))
    assert (
        left.delivery.newly_late_weight,
        left.delivery.old_backlog_last_completion_seconds,
        left.delivery.wait_burden_weight_seconds,
    ) == (
        right.delivery.newly_late_weight,
        right.delivery.old_backlog_last_completion_seconds,
        right.delivery.wait_burden_weight_seconds,
    )
    np.testing.assert_array_equal(left.quality_key, right.quality_key)
    for name in (
        "total_weight",
        "duration_ms",
        "real_weight",
        "virtual_weight",
        "borrowed_weight",
    ):
        np.testing.assert_array_equal(
            getattr(left.chain_facts, name), getattr(right.chain_facts, name)
        )


def _incremental(task, program, quality, plan, previous_plan, previous_evaluation):
    return evaluate_numeric_candidate(
        task,
        program,
        quality,
        plan,
        task,
        program,
        quality,
        previous_plan,
        previous_evaluation,
    )


def test_chain_reorder_reuses_unchanged_chain_results_and_matches_full(monkeypatch):
    task, program, quality = construction_case(
        weights=("100", "100", "100"), widths=("1000", "900", "800")
    )
    previous_plan = NumericPlan.build(task, (0, 1, 2), (0, 1, 3), (10, 20), (0, 0))
    previous = evaluate_numeric_plan(task, program, quality, previous_plan)
    candidate = NumericPlan.build(task, (1, 2, 0), (0, 2, 3), (20, 10), (0, 0), generation=1)
    summary = evaluation_module.summarize_numeric_candidate(
        task, program, quality, candidate, task, program, quality, previous_plan, previous
    )
    incremental = _incremental(task, program, quality, candidate, previous_plan, previous)
    assert summary.counts[2] == 0
    full = evaluate_numeric_plan(task, program, quality, candidate)
    _assert_same(incremental, full)


def test_delivery_clock_vectorization_keeps_half_up_second_rounding():
    task, program, quality = construction_case(
        weights=("100",) * 3,
        widths=("1000", "900", "800"),
        due_dates=("2026-05-31",) * 3,
        duration_hours=("0.00041666666666666667",) * 3,
    )
    plan = NumericPlan.build(task, (0, 1, 2), (0, 3), (10,), (0,))

    result = evaluate_numeric_plan(task, program, quality, plan)

    assert result.delivery.node_end_ms.tolist() == [1500, 3000, 4500]
    assert result.delivery.wait_seconds.tolist() == [2, 3, 5]
    assert result.delivery.old_backlog_last_completion_seconds == 5


def test_only_changed_chains_are_recomputed_and_result_matches_full(monkeypatch):
    task, program, quality = construction_case(
        weights=("100", "100", "100", "100"),
        widths=("1000", "900", "800", "700"),
    )
    previous_plan = NumericPlan.build(task, (0, 1, 2, 3), (0, 2, 3, 4), (10, 20, 30), (0, 0, 0))
    previous = evaluate_numeric_plan(task, program, quality, previous_plan)
    candidate = NumericPlan.build(
        task, (0, 1, 2, 3), (0, 1, 3, 4), (10, 20, 30), (0, 0, 0), generation=1
    )
    summary = evaluation_module.summarize_numeric_candidate(
        task, program, quality, candidate, task, program, quality, previous_plan, previous
    )
    incremental = _incremental(task, program, quality, candidate, previous_plan, previous)
    assert summary.counts[2] == 2
    full = evaluate_numeric_plan(task, program, quality, candidate)
    assert full.kernel_result.counts[2] == 3
    _assert_same(incremental, full)


def test_changed_task_uses_full_new_numeric_evaluation(monkeypatch):
    task, program, quality = construction_case()
    plan = NumericPlan.build(task, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    previous_task = replace(task)
    previous = evaluate_numeric_plan(previous_task, program, quality, plan)
    summary = evaluation_module.summarize_numeric_candidate(
        task, program, quality, plan, previous_task, program, quality, plan, previous
    )
    result = evaluate_numeric_candidate(
        task,
        program,
        quality,
        plan,
        previous_task,
        program,
        quality,
        plan,
        previous,
    )
    assert summary.counts[2] == 2
    _assert_same(result, evaluate_numeric_plan(task, program, quality, plan))


def test_append_only_resource_extension_reuses_unchanged_chains_and_matches_full(
    monkeypatch,
):
    task, program, quality = construction_case(
        weights=("100", "100", "100"), widths=("1000", "900", "800")
    )
    previous_plan = NumericPlan.build(
        task, (0, 1, 2), (0, 2, 3), (10, 20), (0, 0)
    )
    previous = evaluate_numeric_plan(task, program, quality, previous_plan)
    extension = extend_resource_workspace(
        task,
        program,
        quality,
        (
            virtual_node(
                task,
                0,
                0,
                1,
                purpose=VirtualPurpose.EDGE_BRIDGE,
                sequence=1,
            ),
        ),
    )
    added = extension.rows[0]
    candidate = NumericPlan.build(
        extension.task,
        (0, added, 1, 2),
        (0, 3, 4),
        (10, 20),
        (0, 0),
        generation=1,
    )
    summary = evaluation_module.summarize_numeric_candidate(
        extension.task, extension.program, extension.quality, candidate,
        task, program, quality, previous_plan, previous,
    )
    incremental = evaluate_numeric_candidate(
        extension.task,
        extension.program,
        extension.quality,
        candidate,
        task,
        program,
        quality,
        previous_plan,
        previous,
    )
    assert summary.counts[2] == 1
    full = evaluate_numeric_plan(
        extension.task, extension.program, extension.quality, candidate
    )
    assert full.kernel_result.counts[2] == 2
    _assert_same(incremental, full)


def test_reuse_rejects_previous_delivery_with_wrong_node_count():
    task, program, quality = construction_case()
    plan = NumericPlan.build(task, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    previous = evaluate_numeric_plan(task, program, quality, plan)
    malformed = replace(
        previous,
        delivery=replace(previous.delivery, node_end_ms=readonly([0], np.int64)),
    )
    with pytest.raises(NumericValueError, match="matching previous evaluation"):
        _incremental(task, program, quality, plan, plan, malformed)
