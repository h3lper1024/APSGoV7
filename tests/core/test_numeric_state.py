"""Authoritative arrays are numeric and preserve all original-order positions."""

from dataclasses import fields, replace
from decimal import Decimal

import numpy as np
import pytest

from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core._numeric_state import (
    NumericNodeColumns,
    NumericPlan,
    NumericSplitGroup,
    NumericSplitGroups,
    NumericTask,
    readonly,
)
from apsgo_scheduler.core._numeric_units import NumericValueError, allocate_piece_milliseconds
from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.delivery_timing import OrderTimingInput
from apsgo_scheduler.core.rules.concrete import (
    HighSurfaceRunCountRule,
    SameSpecContinuousRealWeightRule,
)
from tests.core.test_delivery_timing import timed_request


def task():
    request = timed_request()
    rules = load_rule_set(request.rule_set_spec)
    return request, NumericTask.build(
        normalize_input(request, rules), rules, request.delivery_timing
    )


def test_numeric_columns_shape_types_readonly_and_units():
    request, value = task()
    assert value.units.weight >= 100
    for field in fields(value.nodes):
        column = getattr(value.nodes, field.name)
        assert column.dtype.kind in ("i", "b")
        assert not column.flags.writeable and column.flags.c_contiguous
        assert column.shape[0] == len(request.orders) + len(request.virtual_prototypes)
    assert value.nodes.duration_ms[: len(request.orders)].tolist() == [36000000] * len(
        request.orders
    )
    assert value.originals.due_ms.tolist() == [86400000] * len(request.orders)
    assert not value.originals.old_backlog.any()
    assert value.fingerprint == task()[1].fingerprint
    assert not hasattr(value.nodes, "nodes") and not hasattr(value.nodes, "weights")
    assert np.shares_memory(value.originals.weight, value.nodes.weight)
    assert np.shares_memory(value.originals.duration_ms, value.nodes.duration_ms)
    assert all(
        getattr(value.split_groups, f.name).shape == (0,) for f in fields(value.split_groups)
    )


def test_actual_original_duration_used_not_derived_rate():
    request, _ = task()
    original = request.delivery_timing.orders[0]
    timing = replace(
        request.delivery_timing,
        orders=(
            replace(original, duration_hours=Decimal("0.00000125")),
            *request.delivery_timing.orders[1:],
        ),
    )
    request = replace(request, delivery_timing=timing)
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    result = NumericTask.build(problem, rules, timing)
    assert result.originals.duration_ms[0] == 5
    with pytest.raises(NumericValueError, match="original total durations"):
        NumericTask.build(problem, rules)
    wrong = replace(
        timing, orders=(OrderTimingInput("missing", "2026-06-01", Decimal(1)), *timing.orders[1:])
    )
    with pytest.raises(NumericValueError, match="exactly once"):
        NumericTask.build(problem, rules, wrong)


def test_plan_positions_use_stable_identity_not_chain_index():
    request, value = task()
    assert len(request.orders) == 2
    plan = NumericPlan.build(value, [1, 0], [0, 1, 2], [71, 91], [0, 0], generation=7)
    assert plan.row_to_chain[:2].tolist() == [1, 0]
    assert plan.row_to_position[:2].tolist() == [0, 0]
    assert plan.source_piece_offsets.tolist() == [0, 1, 2]
    assert plan.source_piece_rows.tolist() == [0, 1]
    assert plan.source_last_position.tolist() == [1, 0]
    assert plan.chain_ids.tolist() == [71, 91]
    assert (
        plan.fingerprint
        == NumericPlan.build(value, [1, 0], [0, 1, 2], [71, 91], [0, 0], generation=8).fingerprint
    )
    assert replace(plan, chain_ids=readonly([71, 92], np.int64)).fingerprint != plan.fingerprint
    assert (
        replace(plan, source_last_position=readonly([0, 1], np.int64)).fingerprint
        != plan.fingerprint
    )
    assert np.all(plan.row_to_chain[value.prototype_rows] == -1)
    assert plan.generation == 7


@pytest.mark.parametrize(
    "rows,offsets,ids,periods",
    [
        ([0, 0], [0, 1, 2], [0, 1], [0, 0]),
        ([-1, 1], [0, 2], [0], [0]),
        ([0, 100], [0, 2], [0], [0]),
        ([0], [0, 1], [0], [0]),
        ([0, 1], [0, 0, 2], [0, 1], [0, 0]),
        ([0, 1], [0, 1, 2], [7, 7], [0, 0]),
        ([0, 1], [0, 2], [0], [-1]),
        ([0.0, 1.0], [0, 2], [0], [0]),
        ([0, 1], [0, 2], [False], [0]),
    ],
)
def test_invalid_layout_rejected_before_indexing(rows, offsets, ids, periods):
    _, value = task()
    with pytest.raises(NumericValueError):
        NumericPlan.build(value, rows, offsets, ids, periods)


def test_prototypes_cannot_be_scheduled_without_generated_identity():
    request, value = task()
    assert len(request.virtual_prototypes) > 0
    with pytest.raises(NumericValueError, match="prototype templates"):
        NumericPlan.build(value, [0, 1, 2], [0, 3], [0], [0])


def test_all_split_pieces_remain_indexed_and_conserve():
    _, value = task()
    count = value.nodes.weight.size
    parent_weight = int(value.nodes.weight[0])
    first = parent_weight // 2
    piece_weights = [first, parent_weight - first]
    durations = allocate_piece_milliseconds(
        parent_weight, int(value.nodes.duration_ms[0]), piece_weights, "durations"
    )
    columns = {}
    for field in fields(value.nodes):
        original = getattr(value.nodes, field.name)
        added = original[[0, 0]].copy()
        if field.name == "weight":
            added[:] = piece_weights
        elif field.name == "duration_ms":
            added[:] = durations
        elif field.name == "piece_index":
            added[:] = [0, 1]
        elif field.name == "piece_count":
            added[:] = 2
        elif field.name == "split_group":
            added[:] = 0
        columns[field.name] = readonly(np.concatenate((original, added)), original.dtype)
    group = NumericSplitGroup(
        0,
        0,
        int(value.nodes.resource[0]),
        parent_weight,
        int(value.nodes.duration_ms[0]),
        int(value.nodes.source_period[0]),
        0,
        0,
        0,
        0,
        1,
    )
    altered = replace(
        value,
        nodes=NumericNodeColumns(**columns),
        split_groups=NumericSplitGroups(
            *(
                readonly([getattr(group, field.name)], np.int64)
                for field in fields(NumericSplitGroups)
            )
        ),
        node_ids=value.node_ids + ("split-piece-1", "split-piece-2"),
    )
    plan = NumericPlan.build(altered, [count, 1, count + 1], [0, 2, 3], [55, 12], [0, 0])
    assert plan.source_piece_rows.tolist() == [count, count + 1, 1]
    assert plan.source_piece_offsets.tolist() == [0, 2, 3]
    assert plan.source_last_position.tolist() == [2, 1]
    assert plan.row_to_chain[0] == -1
    assert value.nodes.weight.size == count


def test_physical_thresholds_participate_in_unit_selection():
    request, _ = task()
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    changed = []
    for rule in rules.rules:
        if type(rule).__name__ == "SyntheticWidthLimitRule":
            rule = replace(
                rule, parameters={**rule.parameters, "maximum_increase": Decimal("0.125")}
            )
        changed.append(rule)
    rules = replace(rules, rules=tuple(changed))
    value = NumericTask.build(problem, rules, request.delivery_timing)
    assert value.units.width == 1000


def test_configured_classification_normalization_and_empty_surface():
    request, _ = task()
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    nodes = (
        replace(
            problem.nodes[0],
            grade=" dc01 ",
            rule_attributes={**problem.nodes[0].rule_attributes, "surface_grade": "fc"},
        ),
        replace(
            problem.nodes[1],
            grade="DC01",
            rule_attributes={**problem.nodes[1].rule_attributes, "surface_grade": None},
        ),
    )
    problem = replace(problem, nodes=nodes)
    extra = (
        HighSurfaceRunCountRule(
            "surface",
            "surface",
            RuleScope.CHAIN,
            True,
            "1",
            {"surface_grades": (" fc ",), "max_run_count": 5},
        ),
        SameSpecContinuousRealWeightRule(
            "same",
            "same",
            RuleScope.CHAIN,
            True,
            "1",
            {"group_by_fields": ("grade",), "max_real_weight": Decimal(100)},
        ),
    )
    rules = replace(rules, rules=rules.rules + extra)
    value = NumericTask.build(problem, rules, request.delivery_timing)
    assert value.surface_matches[-1, :2].tolist() == [True, False]
    assert value.same_spec_groups[-1, :2].tolist() == [0, 0]
    assert value.nodes.grade[0] == value.nodes.grade[1]


def test_temperature_difference_overflow_rejected_before_native_subtraction():
    request, _ = task()
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    changed = replace(
        problem.nodes[0],
        min_temperature=Decimal("-9223372036854775808"),
        max_temperature=Decimal("9223372036854775807"),
    )
    with pytest.raises(NumericValueError, match="temperature.difference_bound"):
        NumericTask.build(
            replace(problem, nodes=(changed, *problem.nodes[1:])), rules, request.delivery_timing
        )
