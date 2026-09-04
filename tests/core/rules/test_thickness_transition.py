"""Ordered thickness bands, physical boundaries, and explicit fallback tolerance."""

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
from apsgo_scheduler.core.rules.concrete import ThicknessTransitionRule

D = Decimal


def band(**changes):
    return {
        "min": None,
        "max": None,
        "include_min": True,
        "include_max": True,
        "tolerance": D("0.2"),
        "calculation_mode": "absolute",
    } | changes


def parameters(**changes):
    return {"basis": "thinner", "ranges": [band()], "fallback_tolerance": D("0.1")} | changes


def rule(**changes):
    return ThicknessTransitionRule(
        **(
            dict(
                rule_id="thickness_jump_limit",
                name="厚度跳跃",
                scope=RuleScope.EDGE,
                enabled=True,
                version="1",
                parameters=parameters(),
            )
            | changes
        )
    )


def node(index, thickness=1, role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D("10"),
        width=None,
        thickness=None if thickness is None else D(thickness),
        min_temperature=None,
        max_temperature=None,
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
        "thickness_jump_limit",
        "ThicknessTransitionRule",
        "厚度跳跃",
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
    "thinner,tolerance",
    [
        ("0.5", "0.1"),
        ("0.6", "0.2"),
        ("1.1", "0.2"),
        ("1.100000001", "0.3"),
        ("1.5", "0.5"),
        ("2", "0.5"),
    ],
)
def test_frozen_gqga4_four_bands_allow_boundary_and_reject_excess_in_both_directions(
    thinner, tolerance
):
    ranges = [
        band(max=D("0.6"), include_max=False, tolerance=D("0.1")),
        band(min=D("0.6"), max=D("1.1"), tolerance=D("0.2")),
        band(min=D("1.1"), max=D("1.5"), include_min=False, include_max=False, tolerance=D("0.3")),
        band(min=D("1.5"), tolerance=D("0.5")),
    ]
    current = rule(parameters=parameters(ranges=ranges, fallback_tolerance=D("0.3")))
    for extra, prohibited in ((D(0), False), (D("0.000001"), True)):
        left, right = node(0, thinner), node(1, D(thinner) + D(tolerance) + extra)
        for first, second in ((left, right), (right, left)):
            value = current.evaluate(EdgeRuleSubject("edge-subject", first, second), context())
            assert len(value.violations) == int(prohibited)
            assert value.metrics == ()
            if prohibited:
                violation = value.violations[0]
                assert violation.rule_id == current.rule_id
                assert violation.scope is RuleScope.EDGE
                assert violation.subject_id == "edge-subject"
                assert violation.reason_code == "thickness"
                assert violation.disposition is RuleDisposition.PROHIBITED
                assert violation.severity == D(1)
                assert type(violation.severity) is Decimal
                assert violation.message


@pytest.mark.parametrize(
    "basis,mode,allowed",
    [
        ("thinner", "absolute", False),
        ("thicker", "absolute", False),
        (" ThInner ", " ReLaTiVe ", False),
        (" THICKER ", "relative", True),
    ],
)
def test_configured_basis_and_calculation_mode_control_relative_tolerance(basis, mode, allowed):
    current = rule(
        parameters=parameters(basis=basis, ranges=[band(tolerance=D("0.3"), calculation_mode=mode)])
    )
    for left, right in (("1", "1.4"), ("1.4", "1")):
        value = current.evaluate(EdgeRuleSubject("edge", node(0, left), node(1, right)), context())
        assert (not value.violations) is allowed


def test_basis_selects_a_band_and_does_not_mean_left_or_right_node():
    ranges = [band(max=D("1.5"), tolerance=D("0.1")), band(min=D("1.5"), tolerance=D(2))]
    for basis, allowed in (("thinner", False), ("thicker", True)):
        current = rule(parameters=parameters(basis=basis, ranges=ranges))
        for left, right in ((1, 2), (2, 1)):
            assert (
                not current.evaluate(
                    EdgeRuleSubject("edge", node(0, left), node(1, right)), context()
                ).violations
            ) is allowed


@pytest.mark.parametrize("endpoint,field", [("1", "include_min"), ("2", "include_max")])
def test_each_range_endpoint_uses_its_own_inclusion_flag(endpoint, field):
    current_subject = EdgeRuleSubject("edge", node(0, endpoint), node(1, D(endpoint) + D("0.5")))
    for included in (False, True):
        current = rule(
            parameters=parameters(
                ranges=[band(min=D(1), max=D(2), tolerance=D(1), **{field: included})]
            )
        )
        assert (not current.evaluate(current_subject, context()).violations) is included


def test_band_selection_has_no_epsilon_and_signed_bounds_are_valid():
    current = rule(parameters=parameters(ranges=[band(min=D(-2), max=D(1), tolerance=D(1))]))
    at_boundary = EdgeRuleSubject("edge", node(0, 1), node(1, "1.5"))
    above_boundary = EdgeRuleSubject("edge", node(0, "1.0000000005"), node(1, "1.5"))
    assert current.evaluate(at_boundary, context()).violations == ()
    assert len(current.evaluate(above_boundary, context()).violations) == 1


def test_first_overlapping_band_wins_and_range_order_is_part_of_fingerprint():
    ranges = [band(tolerance=D("0.1")), band(tolerance=D(1))]
    original = specification(parameters=parameters(ranges=ranges))
    reversed_spec = specification(parameters=parameters(ranges=list(reversed(ranges))))
    current_subject = EdgeRuleSubject("edge", node(0, 1), node(1, "1.5"))
    value = load_rule_set(original).evaluate_edge(current_subject, context())
    assert [item.severity for item in value.violations] == [D(4)]
    assert load_rule_set(reversed_spec).evaluate_edge(current_subject, context()).violations == ()
    assert original.fingerprint != reversed_spec.fingerprint


@pytest.mark.parametrize(
    "ranges",
    [
        [],
        [band(max=D("0.5"))],
        [band(min=D(1), max=D(1), include_min=False)],
    ],
)
def test_empty_ranges_gaps_and_exclusive_equal_bounds_use_explicit_fallback(ranges):
    current_subject = EdgeRuleSubject("edge", node(0, 1), node(1, "1.4"))
    for fallback, allowed in ((D("0.4"), True), (D(0), False)):
        current = rule(parameters=parameters(ranges=ranges, fallback_tolerance=fallback))
        assert (not current.evaluate(current_subject, context()).violations) is allowed


@pytest.mark.parametrize(
    "right,allowed",
    [
        (D.from_float(nextafter(2.0 + 1e-9, -inf)), True),
        (D.from_float(2.0 + 1e-9), True),
        (D.from_float(nextafter(2.0 + 1e-9, inf)), False),
    ],
)
def test_delta_comparison_uses_float_tolerance_plus_epsilon(right, allowed):
    current_subject = EdgeRuleSubject("edge", node(0, 1), node(1, right))
    with localcontext() as decimal_context:
        decimal_context.prec = 3
        value = rule(parameters=parameters(ranges=[], fallback_tolerance=D(1))).evaluate(
            current_subject, context()
        )
    assert (not value.violations) is allowed


@pytest.mark.parametrize("role", tuple(MaterialRole))
def test_all_material_roles_use_the_same_thickness_check(role):
    current_subject = EdgeRuleSubject("edge", node(0, 1, role), node(1, 2))
    value = rule().evaluate(current_subject, context())
    assert [item.reason_code for item in value.violations] == ["thickness"]


def test_split_piece_is_checked_using_its_actual_thickness():
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
        node(0, 1), source_order_id="source", source_resource_id="resource", split_lineage=lineage
    )
    assert (
        len(rule().evaluate(EdgeRuleSubject("edge", piece, node(1, 2)), context()).violations) == 1
    )


@pytest.mark.parametrize("position", [0, 1])
def test_missing_thickness_skips_before_projection_but_fields_remain_required(position):
    nodes = [node(0, D("1e999")), node(1, D("1e999"))]
    nodes[position] = replace(nodes[position], thickness=None)
    assert rule().evaluate(EdgeRuleSubject("edge", *nodes), context()) == RuleContribution((), ())
    assert rule().required_fields() == ("thickness",)


@pytest.mark.parametrize("position", [0, 1])
def test_nonfinite_thickness_projection_is_rejected_with_node_field_location(position):
    nodes = [node(0), node(1)]
    nodes[position] = replace(nodes[position], thickness=D("1e999"))
    with pytest.raises(ValueError) as caught:
        rule().evaluate(EdgeRuleSubject("edge", *nodes), context())
    assert f"node-{position}.thickness" in str(caught.value)
    assert "finite" in str(caught.value)


@pytest.mark.parametrize("relative", [True, False])
def test_reachable_nonfinite_relative_tolerance_or_severity_is_rejected(relative):
    current = rule(
        parameters=parameters(
            ranges=[band(tolerance=D("1e308"), calculation_mode="relative")] if relative else [],
            fallback_tolerance=D(0),
        )
    )
    nodes = (
        (node(0, D("1e308")), node(1, D("1e308")))
        if relative
        else (node(0, 1), node(1, D("1e308")))
    )
    with pytest.raises(ValueError):
        current.evaluate(EdgeRuleSubject("edge", *nodes), context())


@pytest.mark.parametrize(
    "config",
    [
        {key: value for key, value in parameters().items() if key != missing}
        for missing in ("basis", "ranges", "fallback_tolerance")
    ]
    + [
        parameters(basis="left"),
        parameters(basis=None),
        parameters(extra=None),
        parameters(ranges={}),
        parameters(ranges=[None]),
        parameters(fallback_tolerance=-D(1)),
        parameters(fallback_tolerance=1),
        parameters(fallback_tolerance=D("1e999")),
    ]
    + [
        parameters(ranges=[{key: value for key, value in band().items() if key != missing}])
        for missing in ("min", "max", "include_min", "include_max", "tolerance", "calculation_mode")
    ]
    + [
        parameters(ranges=[band(extra=None)]),
        parameters(ranges=[band(min=D(2), max=D(1))]),
        parameters(ranges=[band(min=1)]),
        parameters(ranges=[band(max=D("1e999"))]),
        parameters(ranges=[band(include_min=1)]),
        parameters(ranges=[band(include_max="true")]),
        parameters(ranges=[band(tolerance=-D(1))]),
        parameters(ranges=[band(tolerance=D("1e999"))]),
        parameters(ranges=[band(calculation_mode="percentage")]),
    ],
)
def test_invalid_top_level_and_band_parameters_produce_located_loading_diagnostics(config):
    with pytest.raises(ValueError):
        rule(parameters=config)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=config))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "thickness_jump_limit"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_and_scope_are_rejected_even_when_disabled(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0)), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.CHAIN)


def test_generic_shape_is_validated_even_when_disabled():
    for config in (
        parameters(fallback_tolerance=0.1),
        parameters(ranges=[band(tolerance=D("Infinity"))]),
    ):
        with pytest.raises(ValueError):
            rule(enabled=False, parameters=config)
        with pytest.raises(ValueError):
            specification(enabled=False, parameters=config)


def test_registered_rule_and_nested_configuration_are_frozen_detached_and_stateless():
    ranges = [band()]
    current = rule(parameters=parameters(ranges=ranges))
    current_subject = EdgeRuleSubject("edge", node(0, 1), node(1, 2))
    current_context = context()
    before = fingerprint((current, current_subject, current_context))
    ranges[0]["tolerance"] = D(10)
    ranges.clear()
    assert current.parameters["ranges"][0]["tolerance"] == D("0.2")
    assert ThicknessTransitionRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["ThicknessTransitionRule"] is ThicknessTransitionRule
    assert current.required_fields() == ("thickness",)
    assert current.metric_keys() == ()
    loaded = load_rule_set(specification())
    assert loaded.rules_for_scope(RuleScope.EDGE) == (current,)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == ()
    first = current.evaluate(current_subject, current_context)
    assert loaded.evaluate_edge(current_subject, current_context) == first
    assert current.evaluate(
        EdgeRuleSubject("other", node(2), node(3)), current_context
    ) == RuleContribution((), ())
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["ranges"][0]["tolerance"] = D(5)


def test_disabled_rule_bypasses_business_validation_and_data_but_keeps_signed_identity():
    current_subject = EdgeRuleSubject("edge", node(0, D("1e999")), node(1))
    disabled = rule(enabled=False, parameters={})
    assert disabled.required_fields() == disabled.metric_keys() == ()
    assert disabled.evaluate(current_subject, None) == RuleContribution((), ())
    value = specification(enabled=False, parameters={"ranges": ["legacy"], "basis": "bad"})
    loaded = load_rule_set(value)
    assert loaded.rules == loaded.rules_for_scope(RuleScope.EDGE) == ()
    assert loaded.evaluate_edge(current_subject, context()) == RuleContribution((), ())
    assert value.fingerprint != specification().fingerprint
    assert value.fingerprint != specification(enabled=False, parameters={}).fingerprint
    assert (
        specification().fingerprint
        != specification(parameters=parameters(fallback_tolerance=D(1))).fingerprint
    )
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(value, fingerprint=specification().fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]


def test_loader_rejects_non_edge_scope():
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(scope=RuleScope.CHAIN))
    assert [(item.code, item.field_path) for item in caught.value.issues] == [
        ("rule_scope_mismatch", "rule_set_spec.rules[0].scope")
    ]
