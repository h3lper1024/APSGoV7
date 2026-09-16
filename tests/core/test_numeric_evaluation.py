"""Full numeric evaluation uses the authoritative integer clock and quality order."""

from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest

from apsgo_scheduler.api.request import QualityCriterionSpec
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import (
    RuleSetLoadError,
    fingerprint_rule_set_spec,
    load_rule_set,
)
from apsgo_scheduler.core._numeric_evaluation import (
    NumericDeliveryEvaluation,
    NumericObjective,
    NumericPlanEvaluation,
    NumericQualityProgram,
    evaluate_numeric_candidate,
    evaluate_numeric_overlay_candidate,
    evaluate_numeric_plan,
    preview_numeric_chain_order_quality,
)
from apsgo_scheduler.core._numeric_rules import NumericRuleProgram
from apsgo_scheduler.core._numeric_state import (
    NumericPlan,
    NumericPlanOverlay,
    NumericTask,
    readonly,
)
from apsgo_scheduler.core._numeric_units import NumericValueError
from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput
from tests.app.test_input_normalizer import make_order, make_request
from tests.core.test_numeric_rules import attributes, build, numeric_spec

D = Decimal


def numeric_quality_spec():
    spec = numeric_spec()
    named = {
        "prohibited_violation_count",
        "generated_virtual_weight",
        "chain_count",
    }
    projections = {
        "prohibited_violation_severity": "severity_round_6_half_up_per_violation",
        "underweight_total_gap": "underweight_gap_round_2_half_up_per_chain",
        "old_backlog_last_completion_hours": "delivery_second_half_up",
        "delivery_wait_tardiness_tonne_hours": "delivery_second_half_up",
    }
    quality = tuple(
        QualityCriterionSpec(
            key,
            key,
            "minimize",
            "named_value" if key in named else "sum",
            projections.get(key, "integer_exact_v1"),
        )
        for key in (
            "prohibited_violation_count",
            "prohibited_violation_severity",
            "underweight_chain_count",
            "underweight_total_gap",
            "old_backlog_last_completion_hours",
            "delivery_wait_tardiness_tonne_hours",
            "inter_chain_width_gap",
            "generated_virtual_weight",
            "chain_count",
        )
    )
    changed = replace(spec, quality_spec=quality)
    return replace(changed, fingerprint=fingerprint_rule_set_spec(changed))


def evaluation_case(*, first_due="2026-05-31"):
    orders = (
        make_order(
            0,
            weight=D("100"),
            width=D("1000"),
            source_period="P0",
            rule_attributes=attributes(),
        ),
        make_order(
            1,
            weight=D("100"),
            width=D("900"),
            source_period="P0",
            rule_attributes=attributes(),
        ),
    )
    spec = numeric_quality_spec()
    request = make_request(rule_set_spec=spec, orders=orders)
    timing = DeliveryTimingInput(
        "2026-06-01T00:00:00+08:00",
        (
            OrderTimingInput(orders[0].source_order_id, first_due, D("1")),
            OrderTimingInput(orders[1].source_order_id, "2026-06-01", D("23")),
        ),
        {prototype.prototype_id: D("0.1") for prototype in request.virtual_prototypes},
    )
    request = replace(request, delivery_timing=timing)
    rules = load_rule_set(spec)
    task = NumericTask.build(normalize_input(request, rules), rules, timing)
    program = NumericRuleProgram.compile(task, rules)
    quality = NumericQualityProgram.compile(task, program, rules)
    plan = NumericPlan.build(task, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    return rules, task, program, quality, plan


def test_full_numeric_evaluation_uses_milliseconds_original_completion_and_nine_levels():
    _, task, program, quality, plan = evaluation_case()
    result = evaluate_numeric_plan(task, program, quality, plan)

    assert quality.objectives == tuple(NumericObjective)
    assert result.delivery.node_end_ms.tolist() == [3_600_000, 86_400_000]
    assert result.delivery.original_completion_ms.tolist() == [3_600_000, 86_400_000]
    assert result.delivery.newly_late.tolist() == [False, False]
    assert result.delivery.wait_seconds.tolist() == [3600, 0]
    assert result.quality_key.tolist() == [
        0,
        0,
        2,
        20_000,
        3600,
        36_000_000,
        100 * task.units.width,
        0,
        2,
    ]
    assert result.scheduled_real_weight == 200 * task.units.weight
    assert result.generated_virtual_weight == result.borrowed_future_weight == 0


def test_new_numeric_quality_compiler_rejects_old_or_incomplete_declarations():
    rules, task, program, quality, _ = evaluation_case()
    with pytest.raises(NumericValueError, match="all ordered unique"):
        replace(quality, objectives=quality.objectives[:-1])

    old_rules, old_task = build(
        (
            make_order(0, rule_attributes=attributes()),
            make_order(1, rule_attributes=attributes()),
        )
    )
    old_program = NumericRuleProgram.compile(old_task, old_rules)
    with pytest.raises(NumericValueError, match="new numeric standard"):
        NumericQualityProgram.compile(old_task, old_program, old_rules)


def test_delivery_scores_are_zero_without_backlog_or_lateness():
    _, task, program, quality, plan = evaluation_case(first_due="2026-06-01")
    result = evaluate_numeric_plan(task, program, quality, plan)
    assert not result.delivery.newly_late.any()
    assert result.delivery.wait_seconds.tolist() == [0, 0]
    assert result.quality_key[4:6].tolist() == [0, 0]


def test_chain_order_preview_matches_complete_candidate_evaluation():
    _, task, program, quality, plan = evaluation_case()
    current = evaluate_numeric_plan(task, program, quality, plan)
    candidate = NumericPlan.build(
        task, (1, 0), (0, 1, 2), (20, 10), (0, 0), generation=1
    )
    complete = evaluate_numeric_candidate(
        task, program, quality, candidate, task, program, quality, plan, current
    )

    assert preview_numeric_chain_order_quality(
        task, program, quality, plan, current, (1, 0)
    ) == tuple(int(value) for value in complete.quality_key)


def test_candidate_overlay_matches_formal_candidate_evaluation():
    _, task, program, quality, plan = evaluation_case()
    current = evaluate_numeric_plan(task, program, quality, plan)
    overlay = NumericPlanOverlay.build(
        task,
        plan,
        (readonly((1,), np.int64), readonly((0,), np.int64)),
        (20, 10),
        (0, 0),
    )
    preview = evaluate_numeric_overlay_candidate(
        task, program, quality, overlay, task, program, quality, plan, current
    )
    candidate = NumericPlan.build(
        task, (1, 0), (0, 1, 2), (20, 10), (0, 0), generation=1
    )
    formal = evaluate_numeric_candidate(
        task, program, quality, candidate, task, program, quality, plan, current
    )

    assert np.array_equal(preview.quality_key, formal.quality_key)
    assert preview.violations == formal.violations
    assert preview.chain_results == formal.chain_results
    assert preview.plan_result == formal.plan_result
    assert np.array_equal(
        preview.delivery.original_completion_ms,
        formal.delivery.original_completion_ms,
    )
    assert np.array_equal(preview.chain_facts.total_weight, formal.chain_facts.total_weight)


def test_numeric_evaluation_rejects_cross_task_identity_and_malformed_arrays():
    _, task, program, quality, plan = evaluation_case()
    with pytest.raises(NumericValueError, match="matching task"):
        evaluate_numeric_plan(task, program, quality, replace(plan, task_fingerprint="other"))
    with pytest.raises(NumericValueError, match="read-only delivery"):
        NumericDeliveryEvaluation(
            [], readonly([], np.int64), readonly([], np.bool_), readonly([], np.int64), 0, 0, 0
        )
    result = evaluate_numeric_plan(task, program, quality, plan)
    with pytest.raises(NumericValueError, match="quality vector"):
        replace(result, quality_key=readonly(result.quality_key[:-1], np.int64))


def test_quality_projection_names_cannot_be_attached_to_other_metrics():
    spec = numeric_quality_spec()
    bad = replace(
        spec,
        quality_spec=(
            replace(spec.quality_spec[0], numeric_projection="delivery_second_half_up"),
            *spec.quality_spec[1:],
        ),
    )
    bad = replace(bad, fingerprint=fingerprint_rule_set_spec(bad))
    with pytest.raises(RuleSetLoadError, match="delivery_second_half_up"):
        load_rule_set(bad)


def test_numeric_plan_evaluation_requires_exactly_nine_quality_values():
    _, task, program, quality, plan = evaluation_case()
    result = evaluate_numeric_plan(task, program, quality, plan)
    with pytest.raises(NumericValueError, match="quality vector"):
        NumericPlanEvaluation(
            result.task_fingerprint,
            result.rule_program_fingerprint,
            result.quality_program_fingerprint,
            result.plan_generation,
            result.plan_fingerprint,
            result.chain_results,
            result.plan_result,
            result.node_metrics,
            result.violations,
            result.delivery,
            result.scheduled_real_weight,
            result.generated_virtual_weight,
            result.borrowed_future_weight,
            readonly([0], np.int64),
            result.chain_facts,
        )
