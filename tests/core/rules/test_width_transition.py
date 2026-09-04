"""One width configuration drives adjacent edges and virtual-bridge real anchors."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from itertools import product
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
    EdgeRuleSubject,
    NodeRuleSubject,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import WidthTransitionRule

D = Decimal
REAL = MaterialRole.NORMAL_REAL
ACTUAL = MaterialRole.ACTUAL_TRANSITION
VIRTUAL = MaterialRole.GENERATED_VIRTUAL


def parameters(**changes):
    return {"max_reverse_width": D("20"), "virtual_width_tolerance": D("200")} | changes


def rule(**changes):
    return WidthTransitionRule(
        **(
            dict(
                rule_id="reverse_width_limit",
                name="逆宽规则",
                scope=RuleScope.EDGE,
                enabled=True,
                version="1",
                parameters=parameters(),
            )
            | changes
        )
    )


def node(index, width, role=REAL):
    virtual = role is VIRTUAL
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


def chain_subject(widths, virtual_positions=()):
    nodes = tuple(
        node(index, width, VIRTUAL if index in virtual_positions else REAL)
        for index, width in enumerate(widths)
    )
    return ChainRuleSubject("chain-subject", Chain("chain", nodes, "period"))


def context():
    return RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def definition(**changes):
    return replace(
        RuleDefinitionSpec(
            "reverse_width_limit",
            "WidthTransitionRule",
            "逆宽规则",
            RuleScope.EDGE,
            True,
            "1",
            parameters(),
        ),
        **changes,
    )


def specification(*definitions):
    value = RuleSetSpec(
        "LINE",
        "PROCESS",
        "month",
        "1",
        definitions or (definition(),),
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


def assert_violation(value, *, scope, subject_id, reason, severity=D(1)):
    assert value.metrics == ()
    assert len(value.violations) == 1
    item = value.violations[0]
    assert item.rule_id == "reverse_width_limit"
    assert item.scope is scope
    assert item.subject_id == subject_id
    assert item.reason_code == reason
    assert item.disposition is RuleDisposition.PROHIBITED
    assert item.severity == severity and type(item.severity) is Decimal
    assert item.message


@pytest.mark.parametrize("left_role,right_role", tuple(product(MaterialRole, repeat=2)))
def test_edge_limits_use_roles_not_grade_and_real_decreases_are_unrestricted(left_role, right_role):
    current = rule()
    virtual = VIRTUAL in (left_role, right_role)
    limit = 200 if virtual else 20
    for right_width in (1000, 1000 + limit, 1001 + limit, 500):
        edge = EdgeRuleSubject("edge", node(0, 1000, left_role), node(1, right_width, right_role))
        result = current.evaluate(edge, context())
        expected = abs(right_width - 1000) > limit if virtual else right_width - 1000 > limit
        assert bool(result.violations) is expected
        assert result.metrics == ()
        if expected:
            delta = abs(right_width - 1000) if virtual else right_width - 1000
            assert_violation(
                result,
                scope=RuleScope.EDGE,
                subject_id="edge",
                reason="width_transition_exceeded",
                severity=D(str(max(1.0, (delta - limit) / max(limit, 1.0)))),
            )


@pytest.mark.parametrize("virtual,limit", [(False, 20.0), (True, 200.0)])
@pytest.mark.parametrize("offset,expected", [(-1, False), (0, False), (1, True)])
def test_edge_epsilon_compares_float_difference_to_limit_plus_epsilon(
    virtual, limit, offset, expected
):
    boundary = limit + 1e-9
    right = boundary if offset == 0 else nextafter(boundary, -inf if offset < 0 else inf)
    edge = EdgeRuleSubject(
        "edge", node(0, "1e-30"), node(1, D.from_float(right), VIRTUAL if virtual else REAL)
    )
    assert bool(rule().evaluate(edge, context()).violations) is expected


def test_widths_are_projected_before_subtraction_at_a_decimal_rounding_boundary():
    right = D("1020.00000000100001")
    assert float(right - D(1000)) > 20.0 + 1e-9
    loaded = load_rule_set(specification())
    edge = EdgeRuleSubject("edge", node(0, 1000), node(1, right))
    assert loaded.evaluate_edge(edge, context()) == RuleContribution((), ())
    current = chain_subject((1000, 1010, right), (1,))
    assert loaded.evaluate_chain(current, context()) == RuleContribution((), ())


@pytest.mark.parametrize("bad,reason", [(None, "missing_width"), (D("1e999"), "invalid_width")])
@pytest.mark.parametrize("bad_left", [True, False])
def test_missing_or_nonfinite_float_endpoint_is_a_located_prohibited_record(bad, reason, bad_left):
    nodes = (node(0, bad if bad_left else 1000), node(1, 1050 if bad_left else bad, VIRTUAL))
    assert_violation(
        rule().evaluate(EdgeRuleSubject("supplied-edge", *nodes), context()),
        scope=RuleScope.EDGE,
        subject_id="supplied-edge",
        reason=reason,
    )
    chain = chain_subject((bad if bad_left else 1000, 1100, 1050 if bad_left else bad), (1,))
    assert_violation(
        load_rule_set(specification()).evaluate_chain(chain, context()),
        scope=RuleScope.CHAIN,
        subject_id="chain-subject:reverse_width_limit:virtual_anchor:0-2",
        reason=reason,
    )


@pytest.mark.parametrize(
    "widths,virtual_positions,edge_count,bridge_count",
    [
        ((1000, 1010, 1020), (1,), 0, 0),
        ((1000, 1010, 1030), (1,), 0, 1),
        ((1000, 1100, 1050), (1,), 0, 1),
        ((1000, 1200, 1020), (1,), 0, 0),
        ((1000, 1201, 1020), (1,), 1, 0),
        ((1000, 1100, 950), (1,), 0, 0),
        ((1000, 1100, 1120, 1050), (1, 2), 0, 1),
        ((1000, 1100, 1120, 1020), (1, 2), 0, 0),
        ((1000, 1010, 1020, 1030, 1040), (1, 3), 0, 0),
    ],
)
def test_all_nine_documented_width_examples(widths, virtual_positions, edge_count, bridge_count):
    loaded = load_rule_set(specification())
    current = chain_subject(widths, virtual_positions)
    edges = tuple(
        item
        for index, (left, right) in enumerate(zip(current.chain.nodes, current.chain.nodes[1:]))
        for item in loaded.evaluate_edge(
            EdgeRuleSubject(f"edge-{index}", left, right), context()
        ).violations
    )
    bridges = loaded.evaluate_chain(current, context())
    assert len(edges) == edge_count
    assert len(bridges.violations) == bridge_count
    assert all(item.reason_code == "width_transition_exceeded" for item in edges)
    assert all(
        item.reason_code == "virtual_bridge_reverse_width_exceeded" for item in bridges.violations
    )
    assert bridges.metrics == ()


@pytest.mark.parametrize("offset,expected", [(-1, False), (0, False), (1, True)])
def test_bridge_epsilon_has_the_same_float_projection_as_real_edges(offset, expected):
    boundary = 20.0 + 1e-9
    right = boundary if offset == 0 else nextafter(boundary, -inf if offset < 0 else inf)
    current = chain_subject((D("1e-30"), 10, D.from_float(right)), (1,))
    assert (
        bool(load_rule_set(specification()).evaluate_chain(current, context()).violations)
        is expected
    )


def test_bridge_uses_nearest_real_anchors_without_resetting_on_virtual_plateaus_or_decreases():
    loaded = load_rule_set(specification())
    current = chain_subject((1000, 1120, 1120, 1100, 1050, 1100, 1080, 1000, 900), (1, 2, 3, 5, 7))
    result = loaded.evaluate_chain(current, context())
    assert [(item.subject_id, item.severity) for item in result.violations] == [
        ("chain-subject:reverse_width_limit:virtual_anchor:0-4", D("1.5")),
        ("chain-subject:reverse_width_limit:virtual_anchor:4-6", D(1)),
    ]
    assert result.metrics == ()


@pytest.mark.parametrize(
    "widths,virtual_positions",
    [
        ((1000,), ()),
        ((1000, 1100), ()),
        ((1000, 1100), (0,)),
        ((1000, 1100), (1,)),
        ((1000, 1100, 1200), (0, 2)),
        ((1000, 1010, 1020, 1200, 1210, 1220), (0, 1, 4, 5)),
    ],
)
def test_bridge_does_not_duplicate_real_edges_or_invent_missing_anchors(widths, virtual_positions):
    result = load_rule_set(specification()).evaluate_chain(
        chain_subject(widths, virtual_positions), context()
    )
    assert result == RuleContribution((), ())


def test_missing_interior_virtual_width_does_not_hide_real_anchor_excess():
    loaded = load_rule_set(specification())
    current = chain_subject((1000, None, 1100, 1050), (1, 2))
    assert_violation(
        loaded.evaluate_chain(current, context()),
        scope=RuleScope.CHAIN,
        subject_id="chain-subject:reverse_width_limit:virtual_anchor:0-3",
        reason="virtual_bridge_reverse_width_exceeded",
        severity=D("1.5"),
    )
    assert_violation(
        loaded.evaluate_edge(EdgeRuleSubject("edge", *current.chain.nodes[:2]), context()),
        scope=RuleScope.EDGE,
        subject_id="edge",
        reason="missing_width",
    )


def test_actual_transition_and_split_fragments_are_real_anchors():
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
    fragment = replace(
        node(2, 1050),
        source_order_id="source",
        source_resource_id="resource",
        split_lineage=lineage,
    )
    nodes = (
        node(0, 1000, ACTUAL),
        node(1, 1100, VIRTUAL),
        fragment,
        node(3, 1100, VIRTUAL),
        node(4, 1070, ACTUAL),
    )
    current = ChainRuleSubject("chain-subject", Chain("chain", nodes, "period"))
    assert_violation(
        load_rule_set(specification()).evaluate_chain(current, context()),
        scope=RuleScope.CHAIN,
        subject_id="chain-subject:reverse_width_limit:virtual_anchor:0-2",
        reason="virtual_bridge_reverse_width_exceeded",
        severity=D("1.5"),
    )


def test_severity_and_input_identity_are_independent_of_decimal_context():
    loaded = load_rule_set(specification())
    current = chain_subject((1000, 1100, 1100), (1,))
    edge = EdgeRuleSubject("edge", node(0, 1000), node(1, 2000, VIRTUAL))
    before = fingerprint((loaded.rules, current, edge, context()))
    expected = (loaded.evaluate_edge(edge, context()), loaded.evaluate_chain(current, context()))
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        assert (
            loaded.evaluate_edge(edge, context()),
            loaded.evaluate_chain(current, context()),
        ) == expected
    assert expected[0].violations[0].severity == expected[1].violations[0].severity == D(4)
    assert fingerprint((loaded.rules, current, edge, context())) == before


@pytest.mark.parametrize("name", ["max_reverse_width", "virtual_width_tolerance"])
@pytest.mark.parametrize("invalid", [None, D("-1"), "20", 20, True, D("1e999")])
def test_enabled_numeric_parameters_are_explicit_decimal_and_finite_as_float(name, invalid):
    values = parameters(**{name: invalid})
    with pytest.raises(ValueError):
        rule(parameters=values)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(definition(parameters=values)))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "reverse_width_limit"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"max_reverse_width": D(20)},
        {"virtual_width_tolerance": D(200)},
        parameters(extra=D(20)),
        parameters(reverse_width_carrier_grades=["SPHC"]),
    ],
)
def test_enabled_parameter_keys_are_exactly_the_two_declared_limits(values):
    with pytest.raises(ValueError):
        rule(parameters=values)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(definition(parameters=values)))
    assert [item.code for item in caught.value.issues] == ["invalid_rule_parameters"]


@pytest.mark.parametrize("invalid", [D("NaN"), D("Infinity"), 1.0, {1}, {1: D(20)}])
def test_parameter_shape_validation_still_applies_to_disabled_rules(invalid):
    with pytest.raises(ValueError):
        rule(enabled=False, parameters={"max_reverse_width": invalid})
    with pytest.raises(ValueError):
        definition(enabled=False, parameters={"max_reverse_width": invalid})


def test_zero_limits_are_valid_and_custom_maximum_controls_edge_and_bridge_together():
    for maximum, prohibited in ((D(0), True), (D(20), True), (D(30), False)):
        values = parameters(max_reverse_width=maximum)
        loaded = load_rule_set(specification(definition(parameters=values)))
        edge = EdgeRuleSubject("edge", node(0, 1000), node(1, 1030))
        current = chain_subject((1000, 1010, 1030), (1,))
        assert bool(loaded.evaluate_edge(edge, context()).violations) is prohibited
        assert bool(loaded.evaluate_chain(current, context()).violations) is prohibited
    zero = rule(parameters=parameters(max_reverse_width=D(0), virtual_width_tolerance=D(0)))
    assert zero.evaluate(
        EdgeRuleSubject("equal", node(0, 1000), node(1, 1000, VIRTUAL)), context()
    ) == RuleContribution((), ())
    for tolerance, prohibited in ((D(10), True), (D(11), False)):
        current = rule(parameters=parameters(virtual_width_tolerance=tolerance))
        edge = EdgeRuleSubject("virtual-edge", node(0, 1000), node(1, 1011, VIRTUAL))
        assert bool(current.evaluate(edge, context()).violations) is prohibited


def test_private_companion_is_derived_frozen_ordered_and_not_a_second_configuration():
    first = definition(rule_id="width-first")
    consecutive = definition(
        rule_id="consecutive",
        rule_type="ConsecutiveReverseWidthRule",
        scope=RuleScope.CHAIN,
        parameters={},
    )
    last = definition(
        rule_id="width-last", version="2", parameters=parameters(max_reverse_width=D(40))
    )
    spec = specification(first, consecutive, last)
    loaded = load_rule_set(spec)
    assert WidthTransitionRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["WidthTransitionRule"] is WidthTransitionRule
    assert loaded.fingerprint == spec.fingerprint == fingerprint_rule_set_spec(spec)
    assert [item.rule_id for item in loaded.rules] == ["width-first", "consecutive", "width-last"]
    indexed = loaded.rules_for_scope(RuleScope.CHAIN)
    assert [item.rule_id for item in indexed] == ["width-first", "consecutive", "width-last"]
    assert indexed[1] is loaded.rules[1]
    for parent, companion in ((loaded.rules[0], indexed[0]), (loaded.rules[2], indexed[2])):
        assert type(companion).__name__ == "_VirtualBridgeWidthRule"
        assert type(companion).__bases__ == (Rule,)
        assert type(companion).__name__ not in RULE_REGISTRY
        assert companion not in loaded.rules
        assert companion.scope is RuleScope.CHAIN
        assert companion.enabled is parent.enabled is True
        assert (companion.rule_id, companion.name, companion.version, companion.parameters) == (
            parent.rule_id,
            parent.name,
            parent.version,
            parent.parameters,
        )
        assert companion.metric_keys() == parent.metric_keys() == ()
        assert parent.required_fields() == ("width",)
        with pytest.raises(FrozenInstanceError):
            companion.enabled = False
        with pytest.raises(TypeError):
            companion.parameters["max_reverse_width"] = D(99)
        for candidate in (companion, replace(companion, enabled=False)):
            with pytest.raises(ValueError):
                replace(loaded, rules=(candidate,))


def test_private_class_cannot_be_enabled_through_configuration_or_custom_registry():
    loaded = load_rule_set(specification())
    companion = loaded.rules_for_scope(RuleScope.CHAIN)[0]
    private = specification(definition(rule_type=type(companion).__name__, scope=RuleScope.CHAIN))
    with pytest.raises(RuleSetLoadError) as unknown:
        load_rule_set(private)
    assert [item.code for item in unknown.value.issues] == ["unknown_enabled_rule"]
    with pytest.raises(RuleSetLoadError) as configured:
        load_rule_set(private, registry={type(companion).__name__: type(companion)})
    assert [item.code for item in configured.value.issues] == ["invalid_rule_set"]
    disabled = specification(
        definition(rule_type=type(companion).__name__, scope=RuleScope.CHAIN, enabled=False)
    )
    assert load_rule_set(disabled, registry={}).rules == ()


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_or_scope_is_never_accepted(enabled):
    current = rule(enabled=enabled)
    wrong = NodeRuleSubject("node", node(0, 1000))
    with pytest.raises(UnsupportedRuleSubjectError):
        current.evaluate(wrong, context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.CHAIN)
    companion = load_rule_set(specification()).rules_for_scope(RuleScope.CHAIN)[0]
    with pytest.raises(UnsupportedRuleSubjectError):
        replace(companion, enabled=enabled).evaluate(wrong, context())


@pytest.mark.parametrize(
    "width_enabled,consecutive_enabled", tuple(product((True, False), repeat=2))
)
def test_width_and_consecutive_checks_have_independent_switches(width_enabled, consecutive_enabled):
    consecutive = definition(
        rule_id="consecutive",
        rule_type="ConsecutiveReverseWidthRule",
        scope=RuleScope.CHAIN,
        enabled=consecutive_enabled,
        parameters={},
    )
    loaded = load_rule_set(specification(definition(enabled=width_enabled), consecutive))
    monotonic = chain_subject((1000, 1010, 1030), (1,))
    reasons = [item.reason_code for item in loaded.evaluate_chain(monotonic, context()).violations]
    assert reasons == (["virtual_bridge_reverse_width_exceeded"] if width_enabled else []) + (
        ["consecutive_reverse_width"] if consecutive_enabled else []
    )
    nonmonotonic = chain_subject((1000, 1100, 1050), (1,))
    assert bool(loaded.evaluate_chain(nonmonotonic, context()).violations) is width_enabled


def test_disabled_width_removes_both_checks_but_legacy_parameters_still_bind_fingerprint():
    values = {
        "max_reverse_width": "legacy",
        "virtual_width_tolerance": D("1e999"),
        "legacy": [20, 200],
    }
    current = rule(enabled=False, parameters=values)
    assert current.required_fields() == current.metric_keys() == ()
    assert current.evaluate(
        EdgeRuleSubject("missing", node(0, None), node(1, None)), context()
    ) == RuleContribution((), ())
    disabled = specification(definition(enabled=False, parameters=values))
    values["legacy"].clear()
    assert current.parameters["legacy"] == disabled.rules[0].parameters["legacy"] == (20, 200)
    loaded = load_rule_set(disabled)
    assert (
        loaded.rules
        == loaded.rules_for_scope(RuleScope.EDGE)
        == loaded.rules_for_scope(RuleScope.CHAIN)
        == ()
    )
    assert loaded.evaluate_chain(
        chain_subject((1000, 1100, 1050), (1,)), context()
    ) == RuleContribution((), ())
    assert disabled.fingerprint != specification(definition(enabled=False)).fingerprint
    assert (
        specification(definition(parameters=parameters(max_reverse_width=D(30)))).fingerprint
        != specification().fingerprint
    )
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(disabled, fingerprint=specification().fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]
