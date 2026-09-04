"""Narrow-steel run weight goldens and its non-authorizing rule boundary."""

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
from apsgo_scheduler.core.contracts import ControlledSplitMode, RuleScope, fingerprint
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    ControlledSplitRuleSubject,
    NodeRuleSubject,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import ContinuousNarrowSteelWeightRule

D = Decimal


def parameters(**changes):
    return {
        "grade_class": "IF钢",
        "width_upper_exclusive": D("1400"),
        "max_real_weight": D("500"),
    } | changes


def rule(**changes):
    return ContinuousNarrowSteelWeightRule(
        **(
            dict(
                rule_id="chain_if_narrow_real_weight_lte",
                name="窄钢连续真实重量",
                scope=RuleScope.CHAIN,
                enabled=True,
                version="1",
                parameters=parameters(),
            )
            | changes
        )
    )


def node(index, weight="250", grade_class="IF钢", width=D("1300"), role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D(weight),
        width=width,
        thickness=D("1"),
        min_temperature=None,
        max_temperature=None,
        grade="steel",
        material_role=role,
        rule_attributes={"grade_class": grade_class},
        virtual_lineage=VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1)
        if virtual
        else None,
    )


def subject(nodes):
    return ChainRuleSubject("chain-subject", Chain("chain", tuple(nodes), "period"))


def context():
    return RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def loaded_rule_set(current):
    definition = RuleDefinitionSpec(
        current.rule_id,
        current.rule_type,
        current.name,
        current.scope,
        current.enabled,
        current.version,
        current.parameters,
    )
    specification = RuleSetSpec(
        "LINE",
        "PROCESS",
        "month",
        "1",
        (definition,),
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
    return load_rule_set(
        replace(specification, fingerprint=fingerprint_rule_set_spec(specification))
    )


@pytest.mark.parametrize(
    "second,total,severity",
    [
        ("249", "499", None),
        ("250", "500", None),
        ("250.0000009", "500.0000009", None),
        ("250.000001", "500.000001", None),
        ("250.0000011", "500.0000011", "0.0000011"),
        ("300", "550", "50"),
    ],
)
def test_reference_weight_limit_epsilon_and_exact_severity(second, total, severity):
    current = rule()
    value = current.evaluate(subject((node(0), node(1, second))), context())
    assert [(item.metric_key, item.value) for item in value.metrics] == [
        ("max_if_narrow_real_run_weight", D(total))
    ]
    assert isinstance(value.metrics[0].value, Decimal)
    if severity is None:
        assert value.violations == ()
        return
    assert len(value.violations) == 1
    violation = value.violations[0]
    assert violation.rule_id == current.rule_id
    assert violation.scope is RuleScope.CHAIN
    assert violation.subject_id == f"chain-subject:{current.rule_id}:0-1"
    assert violation.reason_code == "if_narrow_run_weight"
    assert violation.disposition is RuleDisposition.PROHIBITED
    assert violation.severity == D(severity)
    assert isinstance(violation.severity, Decimal)
    assert violation.message


@pytest.mark.parametrize(
    "width,matches",
    [
        (D("1399"), True),
        (D("1399.9999999999"), True),
        (D("1399.99999999999999"), False),
        (D("1400"), False),
        (D("1400.1"), False),
        (None, False),
    ],
)
def test_width_is_reference_float_strict_less_than_without_added_epsilon(width, matches):
    value = rule().evaluate(subject((node(0, "600", width=width),)), context())
    assert value.metrics[0].value == (D("600") if matches else D(0))
    assert len(value.violations) == int(matches)


@pytest.mark.parametrize(
    "bound,width,maximum",
    [("1400.00000000000001", "1400", "0"), ("1200", "1250", "0"), ("1200", "1199", "600")],
)
def test_configured_width_threshold_is_not_fixed_and_uses_float_projection(bound, width, maximum):
    current = rule(parameters=parameters(width_upper_exclusive=D(bound)))
    value = current.evaluate(subject((node(0, "600", width=D(width)),)), context())
    assert value.metrics[0].value == D(maximum)
    assert len(value.violations) == int(D(maximum) > 0)


def test_only_node_grade_class_is_trimmed_and_comparison_is_case_sensitive():
    for configured, material, expected in (
        ("IF钢", " IF钢 ", D("600")),
        (" IF钢 ", "IF钢", D(0)),
        ("IF钢", "if钢", D(0)),
        ("CUSTOM", "CUSTOM", D("600")),
        ("IF钢", "OTHER", D(0)),
    ):
        value = rule(parameters=parameters(grade_class=configured)).evaluate(
            subject((node(0, "600", grade_class=material),)), context()
        )
        assert value.metrics[0].value == expected
        assert len(value.violations) == int(expected > 0)


def test_zero_weight_limit_has_only_the_explicit_weight_epsilon_allowance():
    current = rule(parameters=parameters(max_real_weight=D(0)))
    for weight, prohibited in (("0.000001", 0), ("0.0000011", 1)):
        value = current.evaluate(subject((node(0, weight),)), context())
        assert len(value.violations) == prohibited
        assert value.metrics[0].value == D(weight)


def test_multiple_runs_and_tail_preserve_maximum_and_violation_order():
    nodes = (
        node(0, "10", grade_class="OTHER"),
        node(1, "200"),
        node(2, "301"),
        node(3, "10", width=D("1400")),
        node(4, "400"),
        node(5, "102"),
    )
    current = rule()
    value = current.evaluate(subject(nodes), context())
    assert [(item.subject_id, item.severity) for item in value.violations] == [
        (f"chain-subject:{current.rule_id}:1-2", D(1)),
        (f"chain-subject:{current.rule_id}:4-5", D(2)),
    ]
    assert value.metrics[0].value == D("502")
    reversed_value = current.evaluate(subject(reversed(nodes)), context())
    assert [(item.subject_id, item.severity) for item in reversed_value.violations] == [
        (f"chain-subject:{current.rule_id}:0-1", D(2)),
        (f"chain-subject:{current.rule_id}:3-4", D(1)),
    ]


@pytest.mark.parametrize("role", [MaterialRole.ACTUAL_TRANSITION, MaterialRole.GENERATED_VIRTUAL])
@pytest.mark.parametrize("grade_class", ["IF钢", None])
def test_actual_and_virtual_material_break_runs_and_add_no_weight(role, grade_class):
    nodes = (node(0, "300"), node(1, "1000", grade_class=grade_class, role=role), node(2, "400"))
    value = rule().evaluate(subject(nodes), context())
    assert value.violations == ()
    assert value.metrics[0].value == D("400")


def test_same_source_split_fragments_sum_their_own_weights():
    pieces = []
    for index in (1, 2):
        lineage = SplitLineage(
            partition_id="partition",
            parent_node_id="parent",
            parent_source_order_id="source",
            source_resource_id="resource",
            source_period="period",
            origin_assigned_period="period",
            split_mode=ControlledSplitMode.SAME_PERIOD_SPLIT,
            target_assigned_period="period",
            accepted_source_sequence=1,
            parent_weight=D("600"),
            piece_index=index,
            piece_count=2,
            authorization_rule_id="split-rule",
            authorization_rule_version="1",
            authorization_decision_fingerprint="decision",
            reason_code="authorized",
        )
        pieces.append(
            replace(
                node(index, "300"),
                source_order_id="source",
                source_resource_id="resource",
                split_lineage=lineage,
            )
        )
    value = rule().evaluate(subject(pieces), context())
    assert value.metrics[0].value == D("600")
    assert [item.severity for item in value.violations] == [D("100")]


@pytest.mark.parametrize("missing", ["grade_class", "width_upper_exclusive", "max_real_weight"])
def test_business_parameters_have_no_hidden_defaults(missing):
    configuration = parameters()
    configuration.pop(missing)
    with pytest.raises(ValueError):
        rule(parameters=configuration)


@pytest.mark.parametrize(
    "changes",
    [
        {"grade_class": ""},
        {"grade_class": " "},
        {"grade_class": None},
        {"grade_class": 1},
        {"width_upper_exclusive": D(0)},
        {"width_upper_exclusive": D("-1")},
        {"width_upper_exclusive": D("Infinity")},
        {"width_upper_exclusive": 1400},
        {"width_upper_exclusive": None},
        {"max_real_weight": D("-1")},
        {"max_real_weight": D("NaN")},
        {"max_real_weight": D("Infinity")},
        {"max_real_weight": 500},
        {"max_real_weight": True},
        {"max_real_weight": None},
    ],
)
def test_enabled_rule_rejects_malformed_parameters(changes):
    with pytest.raises(ValueError):
        rule(parameters=parameters(**changes))


@pytest.mark.parametrize(
    "attributes",
    [
        {},
        {"grade_class": None},
        {"grade_class": ""},
        {"grade_class": " "},
        {"grade_class": 1},
        {"grade_class": True},
    ],
)
def test_normal_real_grade_class_is_required_even_inside_a_nonmatching_group(attributes):
    nodes = (node(0, grade_class="OTHER"), replace(node(1), rule_attributes=attributes), node(2))
    with pytest.raises(ValueError, match="grade_class"):
        rule().evaluate(subject(nodes), context())


@pytest.mark.parametrize(
    "second,total,severity",
    [
        ("200.0000004", "500.0000008", None),
        ("200.0000006", "500.000001", None),
        ("200.0000008", "500.0000012", "0.0000012"),
        ("200.00000987654", "500.00001027654", "0.00001027654"),
    ],
)
def test_sum_epsilon_and_severity_do_not_depend_on_ambient_decimal_precision(
    second, total, severity
):
    current = rule()
    current_subject = subject((node(0, "300.0000004"), node(1, second)))
    with localcontext() as arithmetic:
        arithmetic.prec = 2
        value = current.evaluate(current_subject, context())
        assert arithmetic.prec == 2
    assert value.metrics[0].value == D(total)
    assert [item.severity for item in value.violations] == (
        [] if severity is None else [D(severity)]
    )


def test_registered_chain_rule_is_pure_and_does_not_authorize_splitting():
    configuration = parameters()
    current = rule(parameters=configuration)
    assert ContinuousNarrowSteelWeightRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["ContinuousNarrowSteelWeightRule"] is ContinuousNarrowSteelWeightRule
    assert current.required_fields() == ("grade_class",)
    assert current.metric_keys() == ("max_if_narrow_real_run_weight",)
    current_subject = subject((node(0, "600"),))
    current_context = context()
    before = fingerprint((current_subject, current_context, current))
    configuration["max_real_weight"] = D("999")
    first = current.evaluate(current_subject, current_context)
    loaded = loaded_rule_set(current)
    assert loaded.evaluate_chain(current_subject, current_context) == first
    assert loaded.rules_for_scope(RuleScope.CHAIN) == (current,)
    assert loaded.rules_for_scope(RuleScope.ACTION_ELIGIBILITY) == ()
    split_subject = ControlledSplitRuleSubject("split", node(0, "600"), "period", "period", 0)
    with pytest.raises(UnsupportedRuleSubjectError):
        current.evaluate_controlled_split(split_subject, current_context)
    assert not loaded.evaluate_controlled_split(split_subject, current_context).eligible
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current_subject, current_context, current)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["max_real_weight"] = D("999")


def test_disabled_rule_needs_no_business_parameters_and_has_no_contribution():
    disabled = rule(enabled=False, parameters={})
    assert disabled.required_fields() == ()
    assert disabled.metric_keys() == ()
    malformed_for_enabled = subject((replace(node(0), rule_attributes={}),))
    assert disabled.evaluate(malformed_for_enabled, context()) == RuleContribution((), ())
    loaded = loaded_rule_set(disabled)
    assert loaded.rules == ()
    assert loaded.evaluate_chain(malformed_for_enabled, context()) == RuleContribution((), ())


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_fails_before_enabled_dispatch(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0)), context())


def test_loader_rejects_missing_parameters_before_search():
    definition = RuleDefinitionSpec(
        "narrow", "ContinuousNarrowSteelWeightRule", "窄钢规则", RuleScope.CHAIN, True, "1", {}
    )
    spec = RuleSetSpec("LINE", "PROCESS", "month", "1", (definition,), (), frozenset(), "pending")
    with pytest.raises(RuleSetLoadError) as failure:
        load_rule_set(replace(spec, fingerprint=fingerprint_rule_set_spec(spec)))
    assert [issue.code for issue in failure.value.issues] == ["invalid_rule_parameters"]
