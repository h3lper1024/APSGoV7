"""Per-chain total-weight shortfalls are exact diagnostics, not new score levels."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from apsgo_scheduler.api.request import QualityCriterionSpec, RuleDefinitionSpec, RuleSetSpec
from apsgo_scheduler.app.rule_set_loader import (
    RULE_REGISTRY,
    RuleSetLoadError,
    fingerprint_rule_set_spec,
    load_rule_set,
)
from apsgo_scheduler.core.contracts import (
    ControlledSplitMode,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
    fingerprint,
)
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    NodeRuleSubject,
    PlanRuleSubject,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import ChainWeightRangeRule, FutureFillWeightTargetRule

D = Decimal
METRIC = "future_fill_total_gap"


def rule(**changes):
    return FutureFillWeightTargetRule(
        **(
            dict(
                rule_id="future_fill_weight_target",
                name="未来填充目标",
                scope=RuleScope.PLAN,
                enabled=True,
                version="1",
                parameters={"future_fill_weight_target": D(1200)},
            )
            | changes
        )
    )


def node(index, weight, role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "future",
        weight=D(weight),
        width=None,
        thickness=None,
        min_temperature=None,
        max_temperature=None,
        grade="",
        material_role=role,
        rule_attributes={},
        virtual_lineage=VirtualLineage("prototype", VirtualPurpose.WEIGHT_FILL, None, index + 1)
        if virtual
        else None,
    )


def subject(*weights):
    chains = tuple(
        Chain(f"chain-{index}", (node(index, weight),), "current")
        for index, weight in enumerate(weights)
    )
    return PlanRuleSubject("plan", SchedulePlan(chains), None)


def context(periods=("current", "future")):
    return RuleEvaluationContext(
        periods, {period: index for index, period in enumerate(periods)}, ()
    )


def specification(**changes):
    definition = RuleDefinitionSpec(
        "future_fill_weight_target",
        "FutureFillWeightTargetRule",
        "未来填充目标",
        RuleScope.PLAN,
        True,
        "1",
        {"future_fill_weight_target": D(1200)},
    )
    value = RuleSetSpec(
        "LINE",
        "PROCESS",
        "month",
        "1",
        (replace(definition, **changes),),
        (
            QualityCriterionSpec(
                "count", "prohibited_violation_count", "minimize", "count", "exact_decimal"
            ),
            QualityCriterionSpec(
                "severity", "prohibited_violation_severity", "minimize", "sum", "exact_decimal"
            ),
        ),
        frozenset(),
        "pending",
    )
    return replace(value, fingerprint=fingerprint_rule_set_spec(value))


def assert_gap(result, expected):
    assert result.violations == ()
    assert [(item.metric_key, item.value) for item in result.metrics] == [(METRIC, D(expected))]
    assert type(result.metrics[0].value) is Decimal


@pytest.mark.parametrize(
    "weight,gap",
    [
        ("699.999", "500.001"),
        ("700", "500"),
        ("1199.9999999", "0.0000001"),
        ("1200", "0"),
        ("1200.01", "0"),
        ("2000", "0"),
        ("2000.01", "0"),
    ],
)
def test_only_weight_below_target_produces_gap_without_epsilon_or_upper_limit_violation(
    weight, gap
):
    assert_gap(rule().evaluate(subject(weight), context()), gap)


@pytest.mark.parametrize("target,gap", [(0, 0), (500, 0), (1200, 200), (1500, 500)])
def test_target_is_explicitly_configurable_and_zero_is_valid(target, gap):
    current = rule(parameters={"future_fill_weight_target": D(target)})
    assert_gap(current.evaluate(subject(1000), context()), gap)


def test_chain_shortfalls_are_summed_after_clamping_and_cannot_offset_each_other():
    current = subject(1100, 1300, 600, 2000)
    assert_gap(rule().evaluate(current, context()), 700)
    reversed_plan = replace(current, plan=SchedulePlan(tuple(reversed(current.plan.chains))))
    assert rule().evaluate(reversed_plan, context()) == rule().evaluate(current, context())


def test_all_material_and_split_weights_count_independently_of_period_and_borrow_status():
    lineage = SplitLineage(
        "partition",
        "parent",
        "source",
        "resource",
        "future",
        "future",
        ControlledSplitMode.SAME_PERIOD_SPLIT,
        "future",
        1,
        D(200),
        1,
        2,
        "split-rule",
        "1",
        "decision",
        "authorized",
    )
    fragments = tuple(
        replace(
            node(index + 2, 100),
            source_order_id="source",
            source_resource_id="resource",
            split_lineage=replace(lineage, piece_index=index),
        )
        for index in (1, 2)
    )
    nodes = (
        replace(node(0, 200), source_period="current"),
        node(1, 300, MaterialRole.ACTUAL_TRANSITION),
        node(2, 400, MaterialRole.GENERATED_VIRTUAL),
        *fragments,
    )
    current = PlanRuleSubject("plan", SchedulePlan((Chain("chain", nodes, "current"),)), None)
    assert current.plan.chains[0].total_weight == D(1100)
    assert_gap(rule().evaluate(current, context()), 100)
    reassigned = replace(
        current, plan=SchedulePlan((replace(current.plan.chains[0], assigned_period="future"),))
    )
    assert rule().evaluate(reassigned, context(("future", "current"))) == rule().evaluate(
        current, context()
    )


def test_totals_differences_and_sum_remain_exact_under_low_decimal_precision():
    first = Chain("first", (node(0, "600"), node(1, "599.9999999")), "current")
    second = Chain("second", (node(2, "1199.9999998"),), "future")
    current = PlanRuleSubject("plan", SchedulePlan((first, second)), None)
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        assert_gap(rule().evaluate(current, context()), "0.0000003")


def test_finite_decimal_target_is_not_limited_to_float_range():
    current = rule(parameters={"future_fill_weight_target": D("1e400")})
    assert_gap(current.evaluate(subject(1), context()), D("9" * 400))


@pytest.mark.parametrize("invalid", [None, "1200", 1200, True, D("-1")])
def test_enabled_parameter_requires_finite_nonnegative_decimal_with_located_loading_error(invalid):
    values = {"future_fill_weight_target": invalid}
    with pytest.raises(ValueError):
        rule(parameters=values)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=values))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "future_fill_weight_target"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("values", [{}, {"future_fill_weight_target": D(1200), "extra": None}])
def test_enabled_parameter_shape_has_exactly_one_target(values):
    with pytest.raises(ValueError):
        rule(parameters=values)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=values))
    assert [item.code for item in caught.value.issues] == ["invalid_rule_parameters"]


@pytest.mark.parametrize("invalid", [D("NaN"), D("Infinity"), 1.5, {1: "value"}])
def test_generic_parameter_validation_still_applies_when_disabled(invalid):
    for enabled in (True, False):
        with pytest.raises(ValueError):
            rule(enabled=enabled, parameters={"future_fill_weight_target": invalid})
        with pytest.raises(ValueError):
            specification(enabled=enabled, parameters={"future_fill_weight_target": invalid})


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_and_scope_are_rejected_even_when_disabled(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0, 1000)), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.CHAIN)


def test_disabled_rule_emits_nothing_and_retains_only_frozen_configuration_identity():
    values = {"legacy": ["unknown-target"]}
    current = rule(enabled=False, parameters=values)
    disabled = specification(enabled=False, parameters=values)
    values["legacy"].clear()
    assert current.parameters["legacy"] == ("unknown-target",)
    assert current.required_fields() == current.metric_keys() == ()
    assert current.evaluate(
        replace(subject(1), resource_view=object()), object()
    ) == RuleContribution((), ())
    loaded = load_rule_set(disabled)
    assert loaded.rules == loaded.rules_for_scope(RuleScope.PLAN) == ()
    assert loaded.evaluate_plan(subject(1), context()) == RuleContribution((), ())
    assert disabled.fingerprint != specification(enabled=False).fingerprint


def test_registered_frozen_rule_is_dispatched_without_changing_quality_order_or_input():
    values = {"future_fill_weight_target": D(1200)}
    current = rule(parameters=values)
    current_subject, current_context = subject(1100), context()
    before = fingerprint((current, current_subject, current_context))
    values["future_fill_weight_target"] = D(9999)
    spec = specification()
    loaded = load_rule_set(spec)
    assert FutureFillWeightTargetRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["FutureFillWeightTargetRule"] is FutureFillWeightTargetRule
    assert current.required_fields() == ()
    assert current.metric_keys() == (METRIC,)
    assert loaded.rules_for_scope(RuleScope.PLAN) == (current,)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == ()
    assert tuple(item.metric_key for item in loaded.quality_spec) == tuple(
        item.metric_key for item in spec.quality_spec
    )
    assert METRIC not in {item.metric_key for item in loaded.quality_spec}
    first = current.evaluate(current_subject, current_context)
    assert loaded.evaluate_plan(current_subject, current_context) == first
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["future_fill_weight_target"] = D(500)
    changed = specification(parameters={"future_fill_weight_target": D(1300)})
    assert spec.fingerprint != changed.fingerprint
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(changed, fingerprint=spec.fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]


def test_chain_weight_limits_remain_independent_and_only_the_chain_rule_reports_them():
    loaded = load_rule_set(specification())
    weight_rule = ChainWeightRangeRule(
        "chain_weight_range",
        "链重范围",
        RuleScope.CHAIN,
        True,
        "1",
        {"min_weight": D(700), "max_weight": D(2000), "target_weight": D(2000)},
    )
    combined = replace(loaded, rules=loaded.rules + (weight_rule,))
    current = subject(600, 2001)
    assert_gap(combined.evaluate_plan(current, context()), 600)
    results = tuple(
        combined.evaluate_chain(ChainRuleSubject(chain.chain_id, chain), context())
        for chain in current.plan.chains
    )
    assert [
        (item.reason_code, item.disposition) for result in results for item in result.violations
    ] == [
        ("chain_weight_below_minimum", RuleDisposition.ALLOWED_FINAL_DEVIATION),
        ("chain_weight_above_maximum", RuleDisposition.PROHIBITED),
    ]
    assert combined.quality_spec == loaded.quality_spec
