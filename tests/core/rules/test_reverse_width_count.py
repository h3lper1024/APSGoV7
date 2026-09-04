"""Reverse-width count retains its baseline, unlike adjacent-width continuity."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from math import inf, nextafter

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
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    NodeRuleSubject,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import ConsecutiveReverseWidthRule, ReverseWidthCountRule

D = Decimal
METRIC = "reverse_width_count"


def rule(**changes):
    return ReverseWidthCountRule(
        **(
            dict(
                rule_id="max_reverse_width_count",
                name="链内逆宽次数",
                scope=RuleScope.CHAIN,
                enabled=True,
                version="1",
                parameters={"max_count": 2},
            )
            | changes
        )
    )


def node(index, width, role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D("10"),
        width=None if width is None else D(width),
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


def subject(nodes):
    return ChainRuleSubject("chain-subject", Chain("chain", tuple(nodes), "period"))


def context():
    return RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def specification(**changes):
    definition = RuleDefinitionSpec(
        "max_reverse_width_count",
        "ReverseWidthCountRule",
        "链内逆宽次数",
        RuleScope.CHAIN,
        True,
        "1",
        {"max_count": 2},
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
    "widths,limit,count",
    [
        ((1000,), 0, 0),
        ((1000, 1010), 0, 1),
        ((1000, 1010, 1005), 2, 2),
        ((1000, 1010, 1010), 2, 2),
        ((1000, 1010, 1005, 1007), 2, 3),
        ((1000, 1010, 1020, 1030, 1040), 2, 4),
        ((1000, 1010, 1020, 1030), 5, 3),
        ((1030, 1020, 1010, 1000), 0, 0),
        ((1000, 1010, 1000, 1010), 2, 2),
        ((1000, 1010, 990, 995), 1, 2),
        ((1000, 1010, None, 1020, 1030), 1, 2),
        ((None, 1010, 1020, None), 1, 1),
        ((None, None), 0, 0),
    ],
)
def test_retained_baseline_reset_and_limit_golden_cases(widths, limit, count):
    current = rule(parameters={"max_count": limit})
    value = current.evaluate(
        subject(node(index, width) for index, width in enumerate(widths)), context()
    )
    assert [(item.metric_key, item.value) for item in value.metrics] == [(METRIC, count)]
    assert type(value.metrics[0].value) is int
    if count <= limit:
        assert value.violations == ()
        return
    assert len(value.violations) == 1
    violation = value.violations[0]
    assert violation.rule_id == current.rule_id
    assert violation.scope is RuleScope.CHAIN
    assert violation.subject_id == "chain-subject"
    assert violation.reason_code == "reverse_width"
    assert violation.disposition is RuleDisposition.PROHIBITED
    assert violation.severity == D(count - limit)
    assert type(violation.severity) is Decimal
    assert violation.message


@pytest.mark.parametrize(
    "right,count",
    [
        (D.from_float(nextafter(1.0 + 1e-9, -inf)), 0),
        (D.from_float(1.0 + 1e-9), 0),
        (D.from_float(nextafter(1.0 + 1e-9, inf)), 1),
        (D("1.0000000010000000000000001"), 0),
    ],
)
def test_float_plus_epsilon_boundary_is_independent_of_decimal_context(right, count):
    current_subject = subject((node(0, 1), node(1, right)))
    with localcontext() as decimal_context:
        decimal_context.prec = 3
        value = rule(parameters={"max_count": 0}).evaluate(current_subject, context())
    assert value.metrics[0].value == count
    assert len(value.violations) == count
    # Subtract-first would count the exact rounded threshold, unlike the reference formula.
    assert (1.0 + 1e-9) - 1.0 > 1e-9


@pytest.mark.parametrize("role", tuple(MaterialRole))
def test_all_roles_participate_without_grade_or_real_anchor_filtering(role):
    nodes = (node(0, 1000), node(1, 1010, role), node(2, 1005, role), node(3, 1007))
    value = rule().evaluate(subject(nodes), context())
    assert value.metrics[0].value == 3
    assert [item.reason_code for item in value.violations] == ["reverse_width"]


def test_same_source_split_pieces_each_count_above_retained_baseline():
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
        D("20"),
        1,
        2,
        "split-rule",
        "1",
        "decision",
        "authorized",
    )
    fragments = tuple(
        replace(
            node(index, 1010),
            source_order_id="source",
            source_resource_id="resource",
            split_lineage=replace(lineage, piece_index=index),
        )
        for index in (1, 2)
    )
    value = rule(parameters={"max_count": 1}).evaluate(
        subject((node(0, 1000), *fragments)), context()
    )
    assert value.metrics[0].value == 2
    assert [item.severity for item in value.violations] == [D(1)]


def test_count_does_not_reuse_adjacent_continuous_reverse_decisions():
    current_subject = subject((node(0, 1000), node(1, 1010), node(2, 1005)))
    continuous = ConsecutiveReverseWidthRule(
        "forbid_consecutive_reverse_width", "连续逆宽", RuleScope.CHAIN, True, "1", {}
    )
    counted = rule(parameters={"max_count": 1}).evaluate(current_subject, context())
    adjacent = continuous.evaluate(current_subject, context())
    assert counted.metrics[0].value == 2
    assert len(counted.violations) == 1
    assert adjacent.violations == ()
    assert adjacent.metrics[0].value == 0


@pytest.mark.parametrize("position", [0, 2])
def test_nonfinite_float_projection_fails_closed_without_other_rules(position):
    nodes = [node(0, 1000), node(1, 1010), node(2, 1020)]
    nodes[position] = replace(nodes[position], width=D("1e999"))
    current_subject = subject(nodes)
    message = rf"node-{position}\.width.*finite float projection"
    with pytest.raises(ValueError, match=message):
        rule().evaluate(current_subject, context())
    with pytest.raises(ValueError, match=message):
        load_rule_set(specification()).evaluate_chain(current_subject, context())


@pytest.mark.parametrize(
    "parameters",
    [{}, {"max_count": 2, "reverse_width_carrier_grades": ["SPHC"]}]
    + [{"max_count": value} for value in (True, -1, D(2), "2", None, [])],
)
def test_parameters_require_one_explicit_nonnegative_integer_with_located_diagnostic(parameters):
    with pytest.raises(ValueError):
        rule(parameters=parameters)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=parameters))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "max_reverse_width_count"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_and_scope_are_rejected_even_when_disabled(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0, 1000)), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.EDGE)


def test_disabled_rule_still_rejects_float_parameter_shape():
    with pytest.raises(ValueError):
        rule(enabled=False, parameters={"max_count": 2.0})
    with pytest.raises(ValueError):
        specification(enabled=False, parameters={"max_count": 2.0})


def test_registered_rule_is_frozen_detached_and_does_not_retain_baseline_or_count():
    parameters = {"max_count": 2}
    current = rule(parameters=parameters)
    current_subject = subject((node(0, 1000), node(1, 1010), node(2, 1005)))
    current_context = context()
    before = fingerprint((current, current_subject, current_context))
    parameters["max_count"] = 0
    assert ReverseWidthCountRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["ReverseWidthCountRule"] is ReverseWidthCountRule
    assert current.required_fields() == ()
    assert current.metric_keys() == (METRIC,)
    loaded = load_rule_set(specification())
    assert loaded.rules_for_scope(RuleScope.CHAIN) == (current,)
    assert loaded.rules_for_scope(RuleScope.EDGE) == ()
    first = current.evaluate(current_subject, current_context)
    assert loaded.evaluate_chain(current_subject, current_context) == first
    assert current.evaluate(subject((node(3, 1200),)), current_context).metrics[0].value == 0
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["max_count"] = 3


def test_disabled_rule_bypasses_invalid_projection_but_keeps_signed_configuration():
    parameters = {"legacy": ["ignored"], "max_count": "not a count"}
    disabled = rule(enabled=False, parameters=parameters)
    value = specification(enabled=False, parameters=parameters)
    parameters["legacy"].clear()
    assert disabled.parameters["legacy"] == ("ignored",)
    assert disabled.required_fields() == disabled.metric_keys() == ()
    current_subject = subject((node(0, D("1e999")), node(1, 1010)))
    assert disabled.evaluate(current_subject, None) == RuleContribution((), ())
    assert rule(enabled=False, parameters={}).evaluate(current_subject, None) == (
        RuleContribution((), ())
    )
    loaded = load_rule_set(value)
    assert loaded.rules == loaded.rules_for_scope(RuleScope.CHAIN) == ()
    assert loaded.evaluate_chain(current_subject, context()) == RuleContribution((), ())
    assert value.fingerprint != specification().fingerprint
    assert value.fingerprint != specification(enabled=False, parameters={}).fingerprint
    assert specification().fingerprint != specification(parameters={"max_count": 3}).fingerprint
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(value, fingerprint=specification().fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]


def test_loader_rejects_non_chain_scope():
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(scope=RuleScope.EDGE))
    assert [(item.code, item.field_path) for item in caught.value.issues] == [
        ("rule_scope_mismatch", "rule_set_spec.rules[0].scope")
    ]
