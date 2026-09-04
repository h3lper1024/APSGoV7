"""Virtual output ratio consumes the existing resource view with fixed Decimal arithmetic."""

from dataclasses import FrozenInstanceError, replace
from decimal import ROUND_UP, Decimal, Inexact, localcontext

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
from apsgo_scheduler.core.resource_facts import EvaluationResourceView
from apsgo_scheduler.core.rules.base import (
    NodeRuleSubject,
    PlanRuleSubject,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import VirtualOutputRatioRule

D = Decimal
METRIC = "virtual_output_weight_ratio"


def rule(**changes):
    return VirtualOutputRatioRule(
        **(
            dict(
                rule_id="virtual_output_weight_ratio_limit",
                name="虚拟材料重量比例",
                scope=RuleScope.PLAN,
                enabled=True,
                version="1",
                parameters={"max_ratio": D("0.05")},
            )
            | changes
        )
    )


def node(index=0, weight=10, role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D(weight),
        width=None,
        thickness=None,
        min_temperature=None,
        max_temperature=None,
        grade="",
        material_role=role,
        rule_attributes={},
        virtual_lineage=VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1)
        if virtual
        else None,
    )


def resource_view(real=95, virtual=5, **changes):
    return EvaluationResourceView(
        **(
            dict(
                borrowed_node_ids=(),
                generated_virtual_node_ids=(),
                split_partition_ids=(),
                scheduled_real_weight=D(real),
                generated_virtual_weight=D(virtual),
                future_pool_weight=D(0),
                borrowed_future_weight=D(0),
            )
            | changes
        )
    )


def subject(resources, nodes=None):
    plan = SchedulePlan((Chain("chain", (node(),) if nodes is None else tuple(nodes), "period"),))
    return PlanRuleSubject("plan-subject", plan, resources)


def context():
    return RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def specification(**changes):
    definition = RuleDefinitionSpec(
        "virtual_output_weight_ratio_limit",
        "VirtualOutputRatioRule",
        "虚拟材料重量比例",
        RuleScope.PLAN,
        True,
        "1",
        {"max_ratio": D("0.05")},
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


@pytest.mark.parametrize(
    "real,virtual,expected,severity",
    [
        ("95", "5", "0.05", None),
        ("100", "5", "0.04761904761904761904761904762", None),
        ("0", "0", "0", None),
        ("100", "0", "0", None),
        ("0", "5", "1", "0.95"),
        ("90", "10", "0.1", "0.05"),
    ],
)
def test_denominator_includes_real_and_virtual_weight_and_zero_total_has_zero_ratio(
    real, virtual, expected, severity
):
    current = rule()
    value = current.evaluate(subject(resource_view(real, virtual)), None)
    assert [(item.metric_key, item.value) for item in value.metrics] == [(METRIC, D(expected))]
    assert type(value.metrics[0].value) is Decimal
    if severity is None:
        assert value.violations == ()
        return
    assert len(value.violations) == 1
    violation = value.violations[0]
    assert violation.rule_id == current.rule_id
    assert violation.scope is RuleScope.PLAN
    assert violation.subject_id == "plan-subject"
    assert violation.reason_code == "virtual_budget"
    assert violation.disposition is RuleDisposition.PROHIBITED
    assert violation.severity == D(severity)
    assert type(violation.severity) is Decimal
    assert violation.message


@pytest.mark.parametrize(
    "real,virtual,severity",
    [
        ("0.950000000001", "0.049999999999", None),
        ("0.949999000001", "0.050000999999", None),
        ("0.949999", "0.050001", None),
        ("0.949998999999", "0.050001000001", "0.000001000001"),
    ],
)
def test_five_percent_epsilon_boundary_is_not_rounded_to_six_decimal_places(
    real, virtual, severity
):
    value = rule().evaluate(subject(resource_view(real, virtual)), context())
    assert value.metrics[0].value == D(virtual)
    assert [item.severity for item in value.violations] == (
        [] if severity is None else [D(severity)]
    )


@pytest.mark.parametrize(
    "limit,prohibited", [("0", True), ("1", False), ("1.5", False), ("1e1000000", False)]
)
def test_explicit_zero_and_above_one_limits_are_valid_and_large_unused_limits_do_not_overflow(
    limit, prohibited
):
    value = rule(parameters={"max_ratio": D(limit)}).evaluate(
        subject(resource_view(0, 1)), context()
    )
    assert value.metrics[0].value == 1
    assert len(value.violations) == int(prohibited)


@pytest.mark.parametrize(
    "real,virtual,expected",
    [
        ("2", "1", "0.3333333333333333333333333333"),
        (
            "87654321098765432109876543215",
            "12345678901234567890123456785",
            "0.1234567890123456789012345678",
        ),
        (
            "87654321098765432109876543225",
            "12345678901234567890123456775",
            "0.1234567890123456789012345678",
        ),
    ],
)
def test_fixed_28_digit_half_even_division_and_severity_ignore_callers_precision_and_rounding(
    real, virtual, expected
):
    current_subject = subject(resource_view(real, virtual))
    current = rule(parameters={"max_ratio": D(0)})
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        decimal_context.rounding = ROUND_UP
        decimal_context.traps[Inexact] = True
        value = current.evaluate(current_subject, context())
    assert value.metrics[0].value == D(expected)
    assert value.violations[0].severity == D(expected)


def test_future_borrow_statistics_and_identity_lists_do_not_change_ratio():
    plain = resource_view(90, 10)
    unrelated = replace(
        plain,
        future_pool_weight=D(100000),
        borrowed_future_weight=D(99999),
        borrowed_node_ids=("future-a",),
        generated_virtual_node_ids=("v-a", "v-b"),
        split_partition_ids=("partition",),
    )
    assert rule().evaluate(subject(plain), context()) == rule().evaluate(
        subject(unrelated), context()
    )


def test_rule_consumes_supplied_weight_projection_without_reclassifying_plan_materials():
    lineage = SplitLineage(
        "partition",
        "parent",
        "source",
        "resource",
        "period",
        "period",
        ControlledSplitMode.SAME_PERIOD_SPLIT,
        "period",
        1,
        D(10),
        1,
        2,
        "split-rule",
        "1",
        "decision",
        "authorized",
    )
    nodes = [node(0, 90), node(1, 20, MaterialRole.ACTUAL_TRANSITION)]
    for piece_index in (1, 2):
        nodes.append(
            replace(
                node(piece_index + 1, 5),
                source_order_id="source",
                source_resource_id="resource",
                split_lineage=replace(lineage, piece_index=piece_index),
            )
        )
    for index, purpose in enumerate(VirtualPurpose, start=4):
        nodes.append(
            replace(
                node(index, 10, MaterialRole.GENERATED_VIRTUAL),
                virtual_lineage=VirtualLineage(
                    "prototype",
                    purpose,
                    "partition" if purpose is VirtualPurpose.SPLIT_SEPARATOR else None,
                    index,
                ),
            )
        )
    projected = resource_view(120, 30)
    mixed = subject(projected, nodes)
    value = rule().evaluate(mixed, context())
    assert value.metrics[0].value == D("0.2")
    # View derivation and consistency checks belong to the evaluator, not this leaf rule.
    assert rule().evaluate(subject(projected, (node(9, 999),)), context()) == value


@pytest.mark.parametrize("resources", [None, {}, object()])
def test_enabled_rule_requires_the_existing_resource_view_type(resources):
    with pytest.raises(ValueError, match="resource_view"):
        rule().evaluate(subject(resources), context())


@pytest.mark.parametrize(
    "field,value",
    [
        ("scheduled_real_weight", D(-1)),
        ("generated_virtual_weight", D(-1)),
        ("scheduled_real_weight", D("Infinity")),
        ("generated_virtual_weight", D("NaN")),
    ],
)
def test_existing_resource_view_rejects_invalid_weights_before_rule_evaluation(field, value):
    with pytest.raises(ValueError):
        resource_view(**{field: value})


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"max_ratio": D("0.05"), "extra": None},
        {"max_ratio": D(-1)},
        {"max_ratio": 1},
        {"max_ratio": True},
        {"max_ratio": "0.05"},
        {"max_ratio": None},
    ],
)
def test_invalid_enabled_parameters_produce_located_loading_diagnostics(config):
    with pytest.raises(ValueError):
        rule(parameters=config)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=config))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "virtual_output_weight_ratio_limit"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_and_scope_are_rejected_even_when_disabled(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node()), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.CHAIN)


def test_generic_parameter_shape_is_validated_even_when_disabled():
    for value in (0.05, D("Infinity"), D("NaN")):
        with pytest.raises(ValueError):
            rule(enabled=False, parameters={"max_ratio": value})
        with pytest.raises(ValueError):
            specification(enabled=False, parameters={"max_ratio": value})


def test_registered_rule_is_frozen_detached_stateless_and_does_not_add_a_quality_criterion():
    config = {"max_ratio": D("0.05")}
    current = rule(parameters=config)
    current_subject = subject(resource_view(90, 10))
    current_context = context()
    before = fingerprint((current, current_subject, current_context))
    config["max_ratio"] = D(1)
    assert VirtualOutputRatioRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["VirtualOutputRatioRule"] is VirtualOutputRatioRule
    assert current.required_fields() == ()
    assert current.metric_keys() == (METRIC,)
    loaded = load_rule_set(specification())
    assert loaded.rules_for_scope(RuleScope.PLAN) == (current,)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == ()
    assert [item.metric_key for item in loaded.quality_spec] == [
        "prohibited_violation_count",
        "prohibited_violation_severity",
    ]
    first = current.evaluate(current_subject, current_context)
    assert loaded.evaluate_plan(current_subject, current_context) == first
    assert current.evaluate(subject(resource_view(0, 0)), current_context).metrics[0].value == 0
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["max_ratio"] = D(1)


def test_disabled_rule_bypasses_invalid_view_and_preserves_full_signed_identity():
    current_subject = subject(None)
    disabled = rule(enabled=False, parameters={})
    assert disabled.required_fields() == disabled.metric_keys() == ()
    assert disabled.evaluate(current_subject, None) == RuleContribution((), ())
    value = specification(enabled=False, parameters={"max_ratio": "legacy", "unused": [1]})
    loaded = load_rule_set(value)
    assert loaded.rules == loaded.rules_for_scope(RuleScope.PLAN) == ()
    assert loaded.evaluate_plan(current_subject, context()) == RuleContribution((), ())
    assert value.fingerprint != specification().fingerprint
    assert value.fingerprint != specification(enabled=False, parameters={}).fingerprint
    assert (
        specification().fingerprint != specification(parameters={"max_ratio": D("0.1")}).fingerprint
    )
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(value, fingerprint=specification().fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]


def test_loader_rejects_non_plan_scope():
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(scope=RuleScope.CHAIN))
    assert [(item.code, item.field_path) for item in caught.value.issues] == [
        ("rule_scope_mismatch", "rule_set_spec.rules[0].scope")
    ]
