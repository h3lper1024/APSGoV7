"""Compiled rule decisions use one integer task representation."""

from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.api.request import QualityCriterionSpec, RuleDefinitionSpec
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import RULE_REGISTRY, load_rule_set
from apsgo_scheduler.core._numeric_rules import (
    _RULE_KINDS,
    NumericMetricKind,
    NumericReason,
    NumericRuleKind,
    NumericRuleProgram,
    NumericRuleResult,
    NumericSplitReason,
    NumericViolation,
    evaluate_numeric_chain,
    evaluate_numeric_edge,
    evaluate_numeric_split,
    evaluate_numeric_static_plan_rules,
)
from apsgo_scheduler.core._numeric_state import NumericPlan, NumericTask
from apsgo_scheduler.core._numeric_units import SEVERITY_SCALE, NumericValueError
from apsgo_scheduler.core.contracts import RuleScope, fingerprint
from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput
from tests.app.test_input_normalizer import make_order, make_prototype, make_request, make_spec

D = Decimal


def definition(rule_id, rule_type, scope, parameters, version='1'):
    return RuleDefinitionSpec(rule_id, rule_type, rule_id, scope, True, version, parameters)


def numeric_spec():
    rules = (
        definition('synthetic_width', 'SyntheticWidthLimitRule', RuleScope.EDGE,
                   {'maximum_increase': D('50')}),
        definition('soft_hard', 'SoftHardConnectionRule', RuleScope.EDGE, {
            'virtual_sphc_allows_bridge': True,
            'transition_material_breaks_soft_hard': True,
            'missing_grade_policy': 'fallback_same_hot_roll_grade',
        }),
        definition('temperature', 'TemperatureOverlapRule', RuleScope.EDGE, {
            'min_overlap': D('10'), 'ignore_temperature': False,
            'virtual_temperature_adaptive': True,
        }),
        definition('thickness', 'ThicknessTransitionRule', RuleScope.EDGE, {
            'basis': 'thinner', 'fallback_tolerance': D('0.1'),
            'ranges': ({'min': None, 'max': None, 'include_min': True,
                        'include_max': True, 'tolerance': D('0.1'),
                        'calculation_mode': 'absolute'},),
        }),
        definition('width', 'WidthTransitionRule', RuleScope.EDGE,
                   {'max_reverse_width': D('20'), 'virtual_width_tolerance': D('200')}),
        definition('synthetic_priority', 'SyntheticNodePriorityRule', RuleScope.NODE,
                   {'attribute': 'priority'}),
        definition('strategic', 'StrategicCustomerPriorityRule', RuleScope.NODE,
                   {'contains_any': ('宝马',), 'rank': 0, 'default_rank': 1}),
        definition('surface', 'HighSurfaceRunCountRule', RuleScope.CHAIN,
                   {'surface_grades': ('FC', 'FD'), 'max_run_count': 2}),
        definition('narrow', 'ContinuousNarrowSteelWeightRule', RuleScope.CHAIN,
                   {'grade_class': 'IF钢', 'width_upper_exclusive': D('1400'),
                    'max_real_weight': D('100')}),
        definition('same', 'SameSpecContinuousRealWeightRule', RuleScope.CHAIN,
                   {'group_by_fields': ('grade',),
                    'max_real_weight': D('100')}),
        definition('chain_weight', 'ChainWeightRangeRule', RuleScope.CHAIN,
                   {'min_weight': D('200'), 'max_weight': D('1000'), 'target_weight': D('300')}),
        definition('virtual_run', 'ConsecutiveVirtualMaterialRule', RuleScope.CHAIN,
                   {'max_count': 2}),
        definition('reverse_count', 'ReverseWidthCountRule', RuleScope.CHAIN,
                   {'max_count': 1}),
        definition('consecutive_reverse', 'ConsecutiveReverseWidthRule', RuleScope.CHAIN, {}),
        definition('late_period', 'LateOriginalPeriodMoveRule', RuleScope.PLAN, {}),
        definition('virtual_ratio', 'VirtualOutputRatioRule', RuleScope.PLAN,
                   {'max_ratio': D('0.05')}),
        definition('inter_chain', 'InterChainWidthGapRule', RuleScope.PLAN, {}),
        definition('future_fill', 'FutureFillWeightTargetRule', RuleScope.PLAN,
                   {'future_fill_weight_target': D('250')}),
        definition('split', 'ControlledOrderSplitRule', RuleScope.ACTION_ELIGIBILITY, {
            'grade_class': 'IF钢', 'width_upper_exclusive': D('1400'),
            'maximum_piece_weight': D('500'), 'minimum_piece_weight': D('1'),
            'maximum_accepted_source_count': 10000, 'maximum_separator_node_count': 2,
            'maximum_separator_weight': D('40'),
            'allowed_modes': ('same_period_split', 'future_borrow_return'),
        }),
        definition('delivery', 'DeliveryDuePerformanceRule', RuleScope.PLAN, {
            'include_backlog_clearance': True, 'score_time_unit': 'second',
        }, version='3'),
    )
    quality_keys = (
        'prohibited_violation_count',
        'prohibited_violation_severity',
        'underweight_chain_count',
        'underweight_total_gap',
        'old_backlog_last_completion_hours',
        'delivery_wait_tardiness_tonne_hours',
        'inter_chain_width_gap',
        'generated_virtual_weight',
        'chain_count',
    )
    quality = tuple(
        QualityCriterionSpec(key, key, 'minimize', 'sum', 'exact_decimal')
        for key in quality_keys
    )
    return make_spec(rules=rules, quality_spec=quality)


def build(orders, spec=None, prototypes=None):
    spec = numeric_spec() if spec is None else spec
    changes = {'rule_set_spec': spec, 'orders': tuple(orders)}
    if prototypes is not None:
        changes['virtual_prototypes'] = tuple(prototypes)
    request = make_request(**changes)
    timing = DeliveryTimingInput(
        '2026-06-01T00:00:00+08:00',
        tuple(OrderTimingInput(order.source_order_id, '2026-06-01', D('1'))
              for order in request.orders),
        {prototype.prototype_id: D('0.1') for prototype in request.virtual_prototypes},
    )
    request = replace(request, delivery_timing=timing)
    rules = load_rule_set(spec)
    problem = normalize_input(request, rules)
    return rules, NumericTask.build(problem, rules, timing)


def attributes(**changes):
    return {
        'surface_grade': 'FC', 'grade_class': 'IF钢', 'hot_roll_grade': 'SPHC',
        'soft_hard_class': 'soft', 'customer_name': '普通客户', 'priority': 4,
    } | changes


def test_compiler_covers_registry_without_skips():
    assert set(_RULE_KINDS) == set(RULE_REGISTRY.values())
    rules, task = build((make_order(0, rule_attributes=attributes()),
                         make_order(1, rule_attributes=attributes())))
    program = NumericRuleProgram.compile(task, rules)
    assert len(program.rules) == len(rules.rules) == len(NumericRuleKind) == 20
    assert {rule.kind for rule in program.rules} == set(NumericRuleKind)
    assert program.fingerprint == NumericRuleProgram.compile(task, rules).fingerprint
    expected = lambda identity: fingerprint(
        {
            "compiler": 1,
            "task": identity,
            "rule_set": rules.fingerprint,
            "rules": tuple(
                (
                    rule.index,
                    rule.rule_id,
                    int(rule.kind),
                    rule.scope.value,
                    rule.values,
                    rule.flags,
                    rule.bands,
                )
                for rule in program.rules
            ),
        }
    )
    assert program.fingerprint == expected(task.fingerprint)
    changed = replace(task, fingerprint="another-task")
    assert program.rebind(changed).fingerprint == expected(changed.fingerprint)
    with pytest.raises(NumericValueError, match='does not match'):
        NumericRuleProgram.compile(task, replace(rules, fingerprint='another-rule-set'))


def test_numeric_rule_records_reject_mutable_or_cross_task_state():
    with pytest.raises(NumericValueError, match='immutable numeric'):
        NumericRuleResult(violations=[])
    with pytest.raises(NumericValueError, match='boolean disposition'):
        NumericViolation(0, NumericReason.SOFT_HARD, 0, 0, 1, 1, prohibited=1)
    rules, task = build((make_order(0, rule_attributes=attributes()),))
    plan = NumericPlan.build(task, (0,), (0, 1), (1,), (0,))
    changed = replace(task, fingerprint='another-task')
    with pytest.raises(NumericValueError, match='matching numeric task'):
        evaluate_numeric_chain(changed, replace(NumericRuleProgram.compile(task, rules),
                                                task_fingerprint=changed.fingerprint), plan, 0)


def test_edge_rules_use_exact_thresholds_and_physical_severity():
    orders = (
        make_order(0, width=D('1000'), thickness=D('1.00'),
                   min_temperature=D('700'), max_temperature=D('710'),
                   rule_attributes=attributes()),
        make_order(1, width=D('1025'), thickness=D('1.15'),
                   min_temperature=D('705'), max_temperature=D('714'),
                   rule_attributes=attributes(soft_hard_class='hard')),
    )
    rules, task = build(orders)
    result = evaluate_numeric_edge(task, NumericRuleProgram.compile(task, rules), 0, 1,
                                   chain_index=3, position=7)
    assert [item.reason for item in result.violations] == [
        NumericReason.SOFT_HARD, NumericReason.TEMPERATURE,
        NumericReason.THICKNESS, NumericReason.WIDTH_TRANSITION,
    ]
    assert {item.severity for item in result.violations} == {SEVERITY_SCALE}
    metric = next(item for item in result.metrics
                  if item.kind is NumericMetricKind.SYNTHETIC_WIDTH_INCREASE)
    assert metric.numerator == 25 * task.units.width
    equal = replace(orders[1], width=D('1020'), thickness=D('1.10'),
                    min_temperature=D('700'), rule_attributes=attributes())
    rules, task = build((orders[0], equal))
    assert evaluate_numeric_edge(task, NumericRuleProgram.compile(task, rules), 0, 1).violations == ()


def test_virtual_edge_uses_absolute_width_limit_and_adaptive_temperature():
    orders = (make_order(0, width=D('1000'), rule_attributes=attributes()),)
    prototypes = (make_prototype(width=D('1200')), make_prototype(1, width=D('1201')))
    complete = numeric_spec()
    spec = make_spec(rules=tuple(rule for rule in complete.rules
                                 if rule.rule_type != 'SyntheticWidthLimitRule'),
                     quality_spec=complete.quality_spec)
    rules, task = build(orders, spec, prototypes)
    program = NumericRuleProgram.compile(task, rules)
    assert evaluate_numeric_edge(task, program, 0, 1).violations == ()
    result = evaluate_numeric_edge(task, program, 0, 2)
    assert [item.reason for item in result.violations] == [NumericReason.WIDTH_TRANSITION]


def test_chain_rules_preserve_runs_retained_baseline_and_weight_boundaries():
    widths = ('1000', '1010', '1020', '1000')
    orders = tuple(make_order(i, weight=D('60'), width=D(width), source_period='P0',
                              rule_attributes=attributes()) for i, width in enumerate(widths))
    rules, task = build(orders)
    plan = NumericPlan.build(task, range(4), (0, 4), (90,), (0,))
    result = evaluate_numeric_chain(task, NumericRuleProgram.compile(task, rules), plan, 0)
    reasons = [item.reason for item in result.violations]
    assert reasons.count(NumericReason.HIGH_SURFACE_RUN) == 1
    assert reasons.count(NumericReason.NARROW_WEIGHT_RUN) == 1
    assert reasons.count(NumericReason.SAME_SPEC_WEIGHT_RUN) == 1
    assert reasons.count(NumericReason.REVERSE_WIDTH_COUNT) == 1
    assert reasons.count(NumericReason.CONSECUTIVE_REVERSE_WIDTH) == 1
    assert NumericReason.CHAIN_UNDERWEIGHT not in reasons
    values = {item.kind: item.numerator for item in result.metrics}
    assert values[NumericMetricKind.HIGH_SURFACE_RUN_MAX] == 4
    assert values[NumericMetricKind.NARROW_WEIGHT_RUN_MAX] == 240 * task.units.weight
    assert values[NumericMetricKind.REVERSE_WIDTH_COUNT] == 2
    short = NumericPlan.build(task, range(4), (0, 1, 2, 3, 4), (1, 2, 3, 4), (0, 0, 0, 0))
    underweight = evaluate_numeric_chain(task, NumericRuleProgram.compile(task, rules), short, 0)
    allowed = next(item for item in underweight.violations
                   if item.reason is NumericReason.CHAIN_UNDERWEIGHT)
    assert not allowed.prohibited


def test_static_plan_rules_follow_actual_period_order_and_chain_boundaries():
    orders = (
        make_order(0, weight=D('100'), width=D('1000'), source_period='P0',
                   rule_attributes=attributes()),
        make_order(1, weight=D('100'), width=D('900'), source_period='P0',
                   rule_attributes=attributes()),
    )
    rules, task = build(orders)
    plan = NumericPlan.build(task, (0, 1), (0, 1, 2), (8, 9), (1, 1))
    result = evaluate_numeric_static_plan_rules(task, NumericRuleProgram.compile(task, rules), plan)
    assert [item.reason for item in result.violations] == [NumericReason.LATE_PERIOD] * 2
    values = {item.kind: item.numerator for item in result.metrics}
    assert values[NumericMetricKind.LATE_PERIOD_COUNT] == 2
    assert values[NumericMetricKind.INTER_CHAIN_WIDTH_GAP] == 100 * task.units.width
    assert values[NumericMetricKind.FUTURE_FILL_TOTAL_GAP] == 300 * task.units.weight
    ratio = next(item for item in result.metrics if item.kind is NumericMetricKind.VIRTUAL_RATIO)
    assert (ratio.numerator, ratio.denominator) == (0, 200 * task.units.weight)
    reversed_periods = NumericPlan.build(task, (0, 1), (0, 1, 2), (8, 9), (1, 0))
    with pytest.raises(NumericValueError, match='period order'):
        evaluate_numeric_static_plan_rules(task, NumericRuleProgram.compile(task, rules), reversed_periods)


def test_controlled_split_returns_numeric_authority_and_reasons():
    orders = (make_order(0, weight=D('600'), source_period='P0',
                         rule_attributes=attributes()),)
    rules, task = build(orders)
    program = NumericRuleProgram.compile(task, rules)
    allowed = evaluate_numeric_split(task, program, 0, 0, 0)
    assert allowed.eligible and allowed.reason is NumericSplitReason.SAME_PERIOD_SPLIT
    assert allowed.maximum_piece_weight == 500 * task.units.weight
    assert evaluate_numeric_split(task, program, 0, 1, 0).reason is NumericSplitReason.SOURCE_ALREADY_LATE
    assert evaluate_numeric_split(task, program, 0, 0, 10000).reason is NumericSplitReason.SOURCE_LIMIT


def test_relative_thickness_uses_exact_cross_multiplication_and_first_band():
    rule = definition('thickness', 'ThicknessTransitionRule', RuleScope.EDGE, {
        'basis': 'thicker', 'fallback_tolerance': D('0'),
        'ranges': (
            {'min': None, 'max': D('2'), 'include_min': True, 'include_max': True,
             'tolerance': D('0.1'), 'calculation_mode': 'relative'},
            {'min': None, 'max': None, 'include_min': True, 'include_max': True,
             'tolerance': D('10'), 'calculation_mode': 'absolute'},
        ),
    })
    spec = make_spec(rules=(rule,))
    for right, violated in ((D('1.1'), False), (D('1.12'), True)):
        orders = (make_order(0, thickness=D('1')), make_order(1, thickness=right))
        rules, task = build(orders, spec)
        result = evaluate_numeric_edge(task, NumericRuleProgram.compile(task, rules), 0, 1)
        assert bool(result.violations) is violated
