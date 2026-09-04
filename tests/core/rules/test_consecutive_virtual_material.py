"""Maximal generated-virtual runs, independent of their physical attributes."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

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
from apsgo_scheduler.core.rules.concrete import ConsecutiveVirtualMaterialRule

D = Decimal
METRIC = "max_consecutive_virtual_sphc"
VIRTUAL = MaterialRole.GENERATED_VIRTUAL


def rule(**changes):
    return ConsecutiveVirtualMaterialRule(
        **(
            dict(
                rule_id="max_consecutive_virtual_sphc",
                name="连续虚拟材料数量",
                scope=RuleScope.CHAIN,
                enabled=True,
                version="1",
                parameters={"max_count": 2},
            )
            | changes
        )
    )


def node(index, role=MaterialRole.NORMAL_REAL):
    virtual = role is VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D("10"),
        width=None,
        thickness=None,
        min_temperature=None,
        max_temperature=None,
        grade="SPHC",
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
        "max_consecutive_virtual_sphc",
        "ConsecutiveVirtualMaterialRule",
        "连续虚拟材料数量",
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


@pytest.mark.parametrize("count", [0, 1, 2, 3, 6])
def test_limit_boundary_and_trailing_run_emit_one_violation_not_one_per_excess_node(count):
    current = rule()
    value = current.evaluate(
        subject((node(0), *(node(index + 1, VIRTUAL) for index in range(count)))), context()
    )
    assert [(item.metric_key, item.value) for item in value.metrics] == [(METRIC, count)]
    assert type(value.metrics[0].value) is int
    if count <= 2:
        assert value.violations == ()
        return
    assert len(value.violations) == 1
    violation = value.violations[0]
    assert violation.rule_id == current.rule_id
    assert violation.scope is RuleScope.CHAIN
    assert violation.subject_id == f"chain-subject:{current.rule_id}:1-{count}"
    assert violation.reason_code == "virtual_sphc_run"
    assert violation.disposition is RuleDisposition.PROHIBITED
    assert violation.severity == D(count - 2)
    assert type(violation.severity) is Decimal
    assert violation.message


@pytest.mark.parametrize("limit,excess", [(0, 2), (1, 1), (3, 0)])
def test_limit_is_explicit_configuration_including_zero(limit, excess):
    value = rule(parameters={"max_count": limit}).evaluate(
        subject((node(0, VIRTUAL), node(1, VIRTUAL), node(2))), context()
    )
    assert value.metrics[0].value == 2
    assert [item.severity for item in value.violations] == ([D(excess)] if excess else [])


@pytest.mark.parametrize(
    "reverse,expected",
    [
        (False, [("0-2", D(1)), ("4-7", D(2))]),
        (True, [("0-3", D(2)), ("5-7", D(1))]),
    ],
)
def test_multiple_prefix_and_suffix_runs_preserve_position_order(reverse, expected):
    nodes = tuple(node(index, VIRTUAL) if index != 3 else node(index) for index in range(8))
    value = rule().evaluate(subject(reversed(nodes) if reverse else nodes), context())
    assert [(item.subject_id.rsplit(":", 1)[-1], item.severity) for item in value.violations] == (
        expected
    )
    assert value.metrics[0].value == 4


@pytest.mark.parametrize("role", [MaterialRole.NORMAL_REAL, MaterialRole.ACTUAL_TRANSITION])
def test_input_supported_material_breaks_virtual_runs_even_with_sphc_grade(role):
    nodes = tuple(node(index, role if index == 2 else VIRTUAL) for index in range(5))
    value = rule().evaluate(subject(nodes), context())
    assert value.violations == ()
    assert value.metrics[0].value == 2


def test_all_virtual_purposes_count_despite_different_grades_weights_and_missing_dimensions():
    nodes = [node(0)]
    for index, purpose in enumerate(VirtualPurpose, start=1):
        nodes.append(
            replace(
                node(index, VIRTUAL),
                grade=("", "OTHER", "SPHC")[index - 1],
                weight=D(index),
                rule_attributes={"surface_grade": None, "grade_class": None},
                virtual_lineage=VirtualLineage(
                    "prototype",
                    purpose,
                    "partition" if purpose is VirtualPurpose.SPLIT_SEPARATOR else None,
                    index,
                ),
            )
        )
    value = rule().evaluate(subject(nodes), context())
    assert value.metrics[0].value == 3
    assert [(item.subject_id.rsplit(":", 1)[-1], item.severity) for item in value.violations] == [
        ("1-3", D(1))
    ]


def test_each_split_piece_breaks_a_run_without_source_order_deduplication():
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
    nodes = [node(index, VIRTUAL) for index in range(8)]
    for piece_index, position in enumerate((2, 5), start=1):
        nodes[position] = replace(
            node(position),
            source_order_id="source",
            source_resource_id="resource",
            split_lineage=replace(lineage, piece_index=piece_index),
        )
    value = rule().evaluate(subject(nodes), context())
    assert value.violations == ()
    assert value.metrics[0].value == 2


@pytest.mark.parametrize(
    "parameters",
    [{}, {"max_count": 2, "extra": None}]
    + [{"max_count": value} for value in (True, -1, D(2), "2", None, [])],
)
def test_enabled_parameters_are_one_explicit_nonnegative_integer_with_located_diagnostic(
    parameters,
):
    with pytest.raises(ValueError):
        rule(parameters=parameters)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=parameters))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "max_consecutive_virtual_sphc"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_and_scope_are_rejected_even_when_disabled(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0)), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.EDGE)


@pytest.mark.parametrize(
    "parameters", [{1: "invalid"}, {"max_count": object()}, {"max_count": 2.0}]
)
def test_disabled_rules_still_validate_generic_parameter_shape(parameters):
    with pytest.raises(ValueError):
        rule(enabled=False, parameters=parameters)
    with pytest.raises(ValueError):
        specification(enabled=False, parameters=parameters)


def test_registered_rule_is_frozen_detached_and_stateless_across_chains():
    parameters = {"max_count": 2}
    current = rule(parameters=parameters)
    current_subject = subject((node(0), node(1, VIRTUAL), node(2, VIRTUAL), node(3, VIRTUAL)))
    current_context = context()
    before = fingerprint((current, current_subject, current_context))
    parameters["max_count"] = 0
    assert ConsecutiveVirtualMaterialRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["ConsecutiveVirtualMaterialRule"] is ConsecutiveVirtualMaterialRule
    assert current.required_fields() == ()
    assert current.metric_keys() == (METRIC,)
    loaded = load_rule_set(specification())
    assert loaded.rules_for_scope(RuleScope.CHAIN) == (current,)
    assert loaded.rules_for_scope(RuleScope.EDGE) == ()
    first = current.evaluate(current_subject, current_context)
    assert loaded.evaluate_chain(current_subject, current_context) == first
    assert current.evaluate(subject((node(9),)), current_context).metrics[0].value == 0
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["max_count"] = 3


def test_disabled_rule_has_no_contribution_but_its_full_configuration_remains_signed():
    parameters = {"legacy": ["ignored"], "max_count": "not a count"}
    disabled = rule(enabled=False, parameters=parameters)
    value = specification(enabled=False, parameters=parameters)
    parameters["legacy"].clear()
    assert disabled.parameters["legacy"] == ("ignored",)
    assert disabled.required_fields() == disabled.metric_keys() == ()
    current_subject = subject((node(0), node(1, VIRTUAL), node(2, VIRTUAL), node(3, VIRTUAL)))
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
