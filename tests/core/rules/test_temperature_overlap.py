"""Temperature interval overlap, material switches, and numeric safety boundaries."""

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
    MaterialRole,
    Node,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    EdgeRuleSubject,
    NodeRuleSubject,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import TemperatureOverlapRule

D = Decimal


def parameters(**changes):
    return {
        "min_overlap": D(10),
        "ignore_temperature": False,
        "virtual_temperature_adaptive": False,
    } | changes


def rule(**changes):
    return TemperatureOverlapRule(
        **(
            dict(
                rule_id="temperature_overlap",
                name="温区重叠",
                scope=RuleScope.EDGE,
                enabled=True,
                version="1",
                parameters=parameters(),
            )
            | changes
        )
    )


def node(index, minimum=800, maximum=850, role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D("10"),
        width=None,
        thickness=None,
        min_temperature=None if minimum is None else D(minimum),
        max_temperature=None if maximum is None else D(maximum),
        grade="",
        material_role=role,
        rule_attributes={},
        virtual_lineage=VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1)
        if virtual
        else None,
    )


def context():
    return RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def specification(**changes):
    definition = RuleDefinitionSpec(
        "temperature_overlap",
        "TemperatureOverlapRule",
        "温区重叠",
        RuleScope.EDGE,
        True,
        "1",
        parameters(),
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
    "left_bounds,right_bounds,minimum,severity",
    [
        ((800, 900), (810, 850), "10", None),
        ((800, 810), (800, 810), "10", None),
        ((800, 805), (800, 850), "10", D(1)),
        ((0, 10), (30, 40), "10", D(3)),
        ((0, 0), (0, 0), "0", None),
        ((0, 0), ("0.000000002", "0.000000002"), "0", D(2)),
        ((-20, -5), (-10, 0), "5", None),
        ((-20, -5), (-10, 0), "6", D(1)),
    ],
)
def test_overlap_and_severity_are_symmetric_and_support_negative_temperatures(
    left_bounds, right_bounds, minimum, severity
):
    current = rule(parameters=parameters(min_overlap=D(minimum)))
    left, right = node(0, *left_bounds), node(1, *right_bounds)
    for first, second in ((left, right), (right, left)):
        value = current.evaluate(EdgeRuleSubject("edge-subject", first, second), context())
        assert value.metrics == ()
        if severity is None:
            assert value.violations == ()
            continue
        assert len(value.violations) == 1
        violation = value.violations[0]
        assert violation.rule_id == current.rule_id
        assert violation.scope is RuleScope.EDGE
        assert violation.subject_id == "edge-subject"
        assert violation.reason_code == "temperature"
        assert violation.disposition is RuleDisposition.PROHIBITED
        assert violation.severity == severity
        assert type(violation.severity) is Decimal
        assert violation.message


@pytest.mark.parametrize(
    "overlap,allowed",
    [
        (D.from_float(nextafter(10.0 - 1e-9, -inf)), False),
        (D.from_float(10.0 - 1e-9), True),
        (D.from_float(nextafter(10.0 - 1e-9, inf)), True),
        (D("9.9999999989999999"), True),
    ],
)
def test_overlap_plus_epsilon_is_compared_after_float_projection_independent_of_decimal_context(
    overlap, allowed
):
    current_subject = EdgeRuleSubject("edge", node(0, 0, 100), node(1, 0, overlap))
    with localcontext() as decimal_context:
        decimal_context.prec = 3
        value = rule().evaluate(current_subject, context())
    assert (not value.violations) is allowed
    assert value.metrics == ()
    assert 10.0 - (10.0 - 1e-9) > 1e-9


@pytest.mark.parametrize("role", tuple(MaterialRole))
def test_only_generated_virtual_material_can_use_the_adaptive_bypass_on_either_side(role):
    left, right = node(0, 0, 10, role), node(1, 30, 40)
    for adaptive in (False, True):
        current = rule(parameters=parameters(virtual_temperature_adaptive=adaptive))
        for first, second in ((left, right), (right, left)):
            value = current.evaluate(EdgeRuleSubject("edge", first, second), context())
            bypass = adaptive and role is MaterialRole.GENERATED_VIRTUAL
            assert len(value.violations) == (0 if bypass else 1)
            assert value.metrics == ()
        assert current.required_fields() == ("min_temperature", "max_temperature")


def test_split_fragment_is_not_exempted_by_the_virtual_adaptive_switch():
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
    piece = replace(
        node(0, 0, 10),
        source_order_id="source",
        source_resource_id="resource",
        split_lineage=lineage,
    )
    value = rule(parameters=parameters(virtual_temperature_adaptive=True)).evaluate(
        EdgeRuleSubject("split-edge", piece, node(1, 30, 40)), context()
    )
    assert [item.severity for item in value.violations] == [D(3)]


@pytest.mark.parametrize(
    "position,field",
    [
        (0, "min_temperature"),
        (0, "max_temperature"),
        (1, "min_temperature"),
        (1, "max_temperature"),
    ],
)
def test_any_missing_temperature_skips_direct_check_but_remains_an_input_requirement(
    position, field
):
    nodes = [node(0, 0, 10), node(1, 30, 40)]
    nodes[position] = replace(nodes[position], **{field: None})
    current = rule()
    assert current.evaluate(EdgeRuleSubject("edge", *nodes), context()) == RuleContribution((), ())
    assert current.required_fields() == ("min_temperature", "max_temperature")


def test_missing_temperature_short_circuits_before_other_fields_float_conversion():
    current_subject = EdgeRuleSubject("edge", node(0, 0, D("1e999")), node(1, None, 40))
    assert rule().evaluate(current_subject, context()) == RuleContribution((), ())


@pytest.mark.parametrize(
    "position,field,value",
    [
        (0, "min_temperature", D("-1e999")),
        (0, "max_temperature", D("1e999")),
        (1, "min_temperature", D("-1e999")),
        (1, "max_temperature", D("1e999")),
    ],
)
def test_nonfinite_temperature_projection_is_rejected_with_node_and_field_location(
    position, field, value
):
    nodes = [node(0), node(1)]
    nodes[position] = replace(nodes[position], **{field: value})
    with pytest.raises(ValueError) as caught:
        rule().evaluate(EdgeRuleSubject("edge", *nodes), context())
    assert f"node-{position}.{field}" in str(caught.value)
    assert "finite" in str(caught.value)


@pytest.mark.parametrize(
    "left_bounds,right_bounds,minimum,field",
    [
        (("-1e308", "1e308"), ("-1e308", "1e308"), "10", "overlap"),
        (("-1e308", "-1e308"), (0, 0), "1e308", "severity"),
    ],
)
def test_nonfinite_derived_overlap_or_severity_is_rejected(
    left_bounds, right_bounds, minimum, field
):
    current_subject = EdgeRuleSubject("edge", node(0, *left_bounds), node(1, *right_bounds))
    with pytest.raises(ValueError, match=rf"edge.*{field}.*finite"):
        rule(parameters=parameters(min_overlap=D(minimum))).evaluate(current_subject, context())


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"min_overlap": D(10)},
        parameters(extra=None),
        parameters(min_overlap=-D(1)),
        parameters(min_overlap=1),
        parameters(min_overlap=None),
        parameters(min_overlap=D("1e999")),
        parameters(ignore_temperature=1),
        parameters(virtual_temperature_adaptive="true"),
        parameters(ignore_temperature=True, min_overlap=D("1e999")),
    ],
)
def test_enabled_configuration_is_validated_even_when_temperature_is_ignored(config):
    with pytest.raises(ValueError):
        rule(parameters=config)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=config))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "temperature_overlap"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("enabled,ignore", [(True, False), (False, False), (True, True)])
def test_wrong_subject_and_scope_are_rejected_before_disabled_or_ignore_bypasses(enabled, ignore):
    current = rule(enabled=enabled, parameters=parameters(ignore_temperature=ignore))
    with pytest.raises(UnsupportedRuleSubjectError):
        current.evaluate(NodeRuleSubject("node", node(0)), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.CHAIN)


def test_generic_parameter_shape_is_checked_even_when_disabled():
    for config in (parameters(min_overlap=1.0), parameters(min_overlap=D("Infinity"))):
        with pytest.raises(ValueError):
            rule(enabled=False, parameters=config)
        with pytest.raises(ValueError):
            specification(enabled=False, parameters=config)


@pytest.mark.parametrize("bypass", ["disabled", "ignore", "adaptive_virtual"])
def test_bypasses_do_not_convert_temperature_fields_or_emit_contributions(bypass):
    current = rule(
        enabled=bypass != "disabled",
        parameters=parameters(
            ignore_temperature=bypass == "ignore",
            virtual_temperature_adaptive=bypass == "adaptive_virtual",
        ),
    )
    role = (
        MaterialRole.GENERATED_VIRTUAL if bypass == "adaptive_virtual" else MaterialRole.NORMAL_REAL
    )
    current_subject = EdgeRuleSubject("edge", node(0, 0, D("1e999"), role), node(1))
    assert current.evaluate(current_subject, None) == RuleContribution((), ())
    assert current.metric_keys() == ()
    expected_fields = ("min_temperature", "max_temperature") if bypass == "adaptive_virtual" else ()
    assert current.required_fields() == expected_fields


def test_registered_rule_is_frozen_detached_and_stateless():
    config = parameters()
    current = rule(parameters=config)
    current_subject = EdgeRuleSubject("edge", node(0, 0, 10), node(1, 30, 40))
    current_context = context()
    before = fingerprint((current, current_subject, current_context))
    config["ignore_temperature"] = True
    assert TemperatureOverlapRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["TemperatureOverlapRule"] is TemperatureOverlapRule
    assert current.metric_keys() == ()
    loaded = load_rule_set(specification())
    assert loaded.rules_for_scope(RuleScope.EDGE) == (current,)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == ()
    first = current.evaluate(current_subject, current_context)
    assert loaded.evaluate_edge(current_subject, current_context) == first
    assert current.evaluate(EdgeRuleSubject("other-edge", node(2), node(3)), current_context) == (
        RuleContribution((), ())
    )
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["ignore_temperature"] = True


def test_loading_disabled_and_ignored_rules_preserves_their_configuration_identities():
    current_subject = EdgeRuleSubject("edge", node(0, 0, D("1e999")), node(1))
    disabled_spec = specification(enabled=False, parameters={"legacy": ["ignored"]})
    disabled = load_rule_set(disabled_spec)
    assert disabled.rules == disabled.rules_for_scope(RuleScope.EDGE) == ()
    assert disabled.evaluate_edge(current_subject, context()) == RuleContribution((), ())
    assert rule(enabled=False, parameters={}).evaluate(current_subject, None) == RuleContribution(
        (), ()
    )
    ignored_spec = specification(parameters=parameters(ignore_temperature=True))
    ignored = load_rule_set(ignored_spec)
    assert len(ignored.rules_for_scope(RuleScope.EDGE)) == 1
    assert ignored.rules[0].required_fields() == ()
    assert ignored.evaluate_edge(current_subject, context()) == RuleContribution((), ())
    identities = {
        specification().fingerprint,
        disabled_spec.fingerprint,
        ignored_spec.fingerprint,
        specification(parameters=parameters(virtual_temperature_adaptive=True)).fingerprint,
        specification(parameters=parameters(min_overlap=D(20))).fingerprint,
    }
    assert len(identities) == 5
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(disabled_spec, fingerprint=specification().fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]


def test_loader_rejects_non_edge_scope_and_model_rejects_reversed_intervals():
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(scope=RuleScope.CHAIN))
    assert [(item.code, item.field_path) for item in caught.value.issues] == [
        ("rule_scope_mismatch", "rule_set_spec.rules[0].scope")
    ]
    with pytest.raises(ValueError, match="temperature interval is reversed"):
        node(0, 900, 800)
