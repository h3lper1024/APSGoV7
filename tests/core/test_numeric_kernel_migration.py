"""Frozen same-standard reference; not a second production evaluator."""
from itertools import permutations
from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest

from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan
from apsgo_scheduler.core._numeric_state import NumericPlan
from tests.core import numeric_migration_reference_evaluation as reference
from tests.core.test_numeric_construction import construction_case
from tests.core.test_numeric_incremental_evaluation import _assert_same


def assert_kernel_matches(task, rules, quality, plan):
    from apsgo_scheduler.core._numeric_kernel import (
        task_columns, rule_tables, evaluate_kernel, CAPACITY,
    )
    from apsgo_scheduler.core._numeric_evaluation import NumericObjective
    expected = reference.evaluate_numeric_plan(task, rules, quality, plan)
    order = np.array([list(NumericObjective).index(o) for o in quality.objectives], dtype=np.int64)
    args = (task_columns(task), rule_tables(rules.rules), plan.node_rows,
            plan.chain_offsets, plan.chain_periods, order)
    actual = evaluate_kernel(*args)
    assert actual.status[0] == 0, actual.status
    np.testing.assert_array_equal(actual.quality, expected.quality_key)
    np.testing.assert_array_equal(actual.ends, expected.delivery.node_end_ms)
    np.testing.assert_array_equal(actual.completion, expected.delivery.original_completion_ms)
    np.testing.assert_array_equal(actual.waits, expected.delivery.wait_seconds)
    np.testing.assert_array_equal(actual.late, expected.delivery.newly_late)
    for i, name in enumerate(("total_weight", "duration_ms", "real_weight", "virtual_weight", "borrowed_weight")):
        np.testing.assert_array_equal(actual.facts[:, i], getattr(expected.chain_facts, name))
    insufficient = evaluate_kernel(*args, True, 0, 0)
    if actual.counts[0] or actual.counts[1]:
        assert insufficient.status[0] == CAPACITY
    detail = evaluate_kernel(*args, True, int(actual.counts[0]), int(actual.counts[1]))
    assert detail.status[0] == 0
    np.testing.assert_array_equal(actual.quality, detail.quality)
    violations = [(v.rule_index, int(v.reason), v.chain_index, v.start_position,
                   v.end_position, v.severity, int(v.prohibited)) for v in expected.violations]
    assert detail.violations.tolist() == [list(v) for v in violations]
    metrics = [(m.rule_index, int(m.kind), c, m.numerator, m.denominator)
               for c, result in enumerate(expected.chain_results) for m in result.metrics]
    metrics += [(m.rule_index, int(m.kind), -1, m.numerator, m.denominator) for m in expected.plan_result.metrics]
    metrics += [(m.rule_index, int(m.kind), -2, m.numerator, m.denominator) for m in expected.node_metrics]
    assert detail.metrics.tolist() == [list(m) for m in metrics]
    assert evaluate_kernel.nopython_signatures
    from apsgo_scheduler.core._numeric_evaluation import materialize_numeric_evaluation
    _assert_same(materialize_numeric_evaluation(task, rules, quality, plan), expected)


def test_frozen_reference_complete_candidate_corpus():
    task, rules, quality = construction_case(
        weights=("100", "210", "350"), widths=("1000", "1010", "900"),
        due_dates=("2026-05-31", "2026-06-01", "2026-06-01"),
        duration_hours=("1", "24", "2"),
    )
    for rows in permutations(range(3)):
        for offsets in ((0, 3), (0, 1, 3), (0, 2, 3), (0, 1, 2, 3)):
            count = len(offsets) - 1
            plan = NumericPlan.build(task, rows, offsets, tuple(range(count)), (0,) * count)
            _assert_same(evaluate_numeric_plan(task, rules, quality, plan),
                         reference.evaluate_numeric_plan(task, rules, quality, plan))
            assert_kernel_matches(task, rules, quality, plan)


def test_native_all_rules_runs_virtual_endpoints_late_period_and_priorities():
    from tests.app.test_input_normalizer import make_order
    from tests.core.test_numeric_rules import attributes, build
    from tests.core.test_numeric_evaluation import numeric_quality_spec
    from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
    from apsgo_scheduler.core._numeric_rules import NumericRuleProgram
    from tests.core.numeric_reference_resources import extend_resource_workspace, virtual_node
    from apsgo_scheduler.core.model import VirtualPurpose
    orders = tuple(make_order(
        i, weight=Decimal("300"), width=Decimal(w), source_period="P0",
        thickness=Decimal("1") + Decimal(i) / 10,
        rule_attributes=attributes(soft_hard_class="hard" if i == 2 else "soft"),
    ) for i, w in enumerate(("1000", "1010", "1030", "900")))
    ruleset, task = build(orders, numeric_quality_spec())
    rules = NumericRuleProgram.compile(task, ruleset)
    quality = NumericQualityProgram.compile(task, rules, ruleset)
    for rows in permutations(range(4)):
        plan = NumericPlan.build(task, rows, (0, 4), (1,), (1,))
        assert_kernel_matches(task, rules, quality, plan)
    workspace = extend_resource_workspace(task, rules, quality, tuple(
        virtual_node(task, 0, 0, 2, purpose=VirtualPurpose.EDGE_BRIDGE, sequence=i+1)
        for i in range(3)
    ))
    plan = NumericPlan.build(workspace.task, (0, *workspace.rows, 2, 1, 3),
                             (0, 5, 7), (1, 2), (0, 1))
    assert_kernel_matches(workspace.task, workspace.program, workspace.quality, plan)


@pytest.mark.parametrize("policy", range(5))
def test_native_soft_policy_and_relative_thickness_configuration(policy):
    from apsgo_scheduler.core._numeric_rules import NumericRuleKind, NumericThicknessBand
    from apsgo_scheduler.core._numeric_state import readonly
    task, rules, quality = construction_case(weights=("100", "100"), widths=("1000", "1025"))
    n = replace(task.nodes, thickness=readonly((100, 112, *task.nodes.thickness[2:]), np.int64),
                soft_hard_class=readonly((-9, -9, *task.nodes.soft_hard_class[2:]), np.int64))
    task = replace(task, nodes=n)
    changed = tuple(
        replace(r, values=(policy, -9, -10)) if r.kind == NumericRuleKind.SOFT_HARD else
        replace(r, values=(1, 0, 1), bands=(
            NumericThicknessBand(0, 112, False, True, False, True, True, 1, 10),
            NumericThicknessBand(112, 0, True, False, False, False, False, 100, 1),
        )) if r.kind == NumericRuleKind.THICKNESS else r
        for r in rules.rules
    )
    rules = replace(rules, rules=changed)
    plan = NumericPlan.build(task, (0, 1), (0, 2), (1,), (0,))
    assert_kernel_matches(task, rules, quality, plan)


def test_native_status_and_checked_arithmetic_do_not_wrap():
    from apsgo_scheduler.core._numeric_kernel import (
        _add, _sub, _mul, _round, NUMERIC_ERROR, INVALID, CANCELLED,
        task_columns, rule_tables, evaluate_kernel,
    )
    for function, a, b in ((_add, 2**63-1, 1), (_sub, -2**63, 1),
                            (_mul, 2**62, 4), (_mul, -2**63, -1)):
        status = np.zeros(5, np.int64)
        function(a, b, status)
        assert status[0] == NUMERIC_ERROR
    for value, expected in ((1500, 2), (2500, 3), (4500, 5)):
        assert _round(value, 1000, np.zeros(5, np.int64)) == expected
    exact = np.zeros(5, np.int64)
    assert _mul(-(2**62), 2, exact) == -(2**63)
    assert exact[0] == 0
    task, rules, quality = construction_case()
    args = (task_columns(task), rule_tables(rules.rules), np.array([0, 1]),
            np.array([0, 2]), np.array([0]), np.arange(9))
    assert evaluate_kernel(*args, cancelled=True).status[0] == CANCELLED
    invalid = (*args[:3], np.array([0, 3]), *args[4:])
    assert evaluate_kernel(*invalid).status[0] == INVALID


def test_native_missing_fields_and_parameter_branches_match_reference():
    from apsgo_scheduler.core._numeric_rules import NumericRuleKind
    from apsgo_scheduler.core._numeric_state import readonly
    task, rules, quality = construction_case(
        weights=("100.005", "200.004", "999.991"),
        widths=("1000", "1040", "900"),
        temperatures=(("700", "710"), ("800", "810"), ("700", "800")),
    )
    plan = NumericPlan.build(task, (0, 1, 2), (0, 3), (1,), (0,))
    for field in range(4):
        present = task.nodes.present.copy()
        present[1, field] = False
        altered = replace(task, nodes=replace(task.nodes, present=readonly(present, np.bool_)))
        assert_kernel_matches(altered, rules, quality, plan)
    for ignore, adaptive in ((False, False), (True, False), (True, True)):
        changed = tuple(
            replace(rule, flags=(ignore, adaptive)) if rule.kind == NumericRuleKind.TEMPERATURE else
            replace(rule, values=(0, 1)) if rule.kind == NumericRuleKind.VIRTUAL_RATIO else rule
            for rule in rules.rules
        )
        assert_kernel_matches(task, replace(rules, rules=changed), quality, plan)


def test_summary_has_zero_detail_objects_and_reuses_only_unchanged_chains(monkeypatch):
    import apsgo_scheduler.core._numeric_evaluation as e
    from apsgo_scheduler.core._numeric_state import NumericPlanOverlay
    task, rules, quality = construction_case(
        weights=("100",) * 4, widths=("1000", "900", "800", "700"),
    )
    old = NumericPlan.build(task, (0, 1, 2, 3), (0, 2, 3, 4), (10, 20, 30), (0, 0, 0))
    previous = e.materialize_numeric_evaluation(task, rules, quality, old)
    overlay = NumericPlanOverlay.build(task, old, ((0,), (1, 2), (3,)), (10, 20, 30), (0, 0, 0))
    expected = reference.evaluate_numeric_overlay_candidate(
        task, rules, quality, overlay, task, rules, quality, old, previous,
    )
    def forbidden(*args, **kwargs):
        raise AssertionError("summary constructed a detail object")
    with monkeypatch.context() as m:
        for cls in (e.NumericPlanEvaluation, e.NumericRuleResult, e.NumericViolation, e.NumericMetric):
            m.setattr(cls, "__post_init__", forbidden)
        summary = e.summarize_numeric_candidate(
            task, rules, quality, overlay, task, rules, quality, old, previous,
        )
    assert summary.counts[2] == 2
    actual = e.materialize_numeric_evaluation(task, rules, quality, overlay, summary)
    _assert_same(actual, expected)
    # Plan rules with a node subject must not leak into cached chain scores.
    shifted = NumericPlan.build(task, (0, 1, 2, 3), (0, 2, 3, 4), (10, 20, 30), (1, 1, 1))
    p = e.materialize_numeric_evaluation(task, rules, quality, shifted)
    summary = e.summarize_numeric_candidate(
        task, rules, quality, shifted, task, rules, quality, shifted, p)
    assert summary.counts[2] == 0
    _assert_same(e.materialize_numeric_evaluation(task, rules, quality, shifted, summary),
                 reference.evaluate_numeric_plan(task, rules, quality, shifted))
