"""Material short-circuits and explicit missing-class policy for soft/hard connections."""

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
from apsgo_scheduler.core.rules.concrete import SoftHardConnectionRule

D = Decimal
REAL = MaterialRole.NORMAL_REAL
ACTUAL = MaterialRole.ACTUAL_TRANSITION
VIRTUAL = MaterialRole.GENERATED_VIRTUAL


def parameters(**changes):
    return {
        "virtual_sphc_allows_bridge": True,
        "transition_material_breaks_soft_hard": True,
        "missing_grade_policy": "deny",
    } | changes


def rule(**changes):
    return SoftHardConnectionRule(
        **(
            dict(
                rule_id="gqga4_soft_hard_connection",
                name="软硬材连接",
                scope=RuleScope.EDGE,
                enabled=True,
                version="1",
                parameters=parameters(),
            )
            | changes
        )
    )


def node(index, soft="soft", hot="SPHC", role=REAL):
    virtual = role is VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D(10),
        width=None,
        thickness=None,
        min_temperature=None,
        max_temperature=None,
        grade="display-grade",
        material_role=role,
        rule_attributes={"soft_hard_class": soft, "hot_roll_grade": hot},
        virtual_lineage=VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1)
        if virtual
        else None,
    )


def context():
    return RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def specification(**changes):
    definition = RuleDefinitionSpec(
        "gqga4_soft_hard_connection",
        "SoftHardConnectionRule",
        "软硬材连接",
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


def assert_connection(current, left, right, allowed):
    for first, second in ((left, right), (right, left)):
        result = current.evaluate(EdgeRuleSubject("supplied-edge", first, second), context())
        assert result.metrics == ()
        if allowed:
            assert result == RuleContribution((), ())
        else:
            assert len(result.violations) == 1
            violation = result.violations[0]
            assert violation.rule_id == current.rule_id
            assert violation.scope is RuleScope.EDGE
            assert violation.subject_id == "supplied-edge"
            assert violation.reason_code == "soft_hard_connection_not_allowed"
            assert violation.disposition is RuleDisposition.PROHIBITED
            assert violation.severity == D(1) and type(violation.severity) is Decimal
            assert violation.message


@pytest.mark.parametrize(
    "roles,virtual_enabled,actual_enabled,allowed",
    [
        ((VIRTUAL, REAL), True, False, True),
        ((VIRTUAL, REAL), False, True, False),
        ((VIRTUAL, ACTUAL), True, False, True),
        ((VIRTUAL, ACTUAL), False, True, False),
        ((VIRTUAL, VIRTUAL), True, False, True),
        ((VIRTUAL, VIRTUAL), False, True, False),
        ((ACTUAL, REAL), False, True, True),
        ((ACTUAL, REAL), True, False, False),
        ((ACTUAL, ACTUAL), False, True, True),
        ((ACTUAL, ACTUAL), True, False, False),
    ],
)
def test_virtual_then_actual_switches_return_directly_without_reading_bypassed_attributes(
    roles, virtual_enabled, actual_enabled, allowed
):
    current = rule(
        parameters=parameters(
            virtual_sphc_allows_bridge=virtual_enabled,
            transition_material_breaks_soft_hard=actual_enabled,
            missing_grade_policy="fallback_same_hot_roll_grade",
        )
    )
    assert_connection(current, node(0, 1, True, roles[0]), node(1, 1, True, roles[1]), allowed)


@pytest.mark.parametrize(
    "left,right,allowed",
    [
        ("soft", "soft", True),
        ("soft", "hard", False),
        (" soft ", "soft", True),
        ("SOFT", "soft", False),
        ("custom", "custom", True),
        ("custom", "other", False),
    ],
)
def test_nonempty_classes_compare_stripped_case_sensitive_text_without_using_hot_grade(
    left, right, allowed
):
    assert_connection(rule(), node(0, left, True), node(1, right, 1), allowed)


@pytest.mark.parametrize(
    "policy,allowed",
    [
        ("deny", False),
        ("allow", True),
        ("pass", True),
        ("ignore", True),
        ("fallback_same_hot_roll_grade", True),
    ],
)
def test_any_missing_class_uses_the_explicit_policy(policy, allowed):
    current = rule(parameters=parameters(missing_grade_policy=policy))
    for missing in (None, "", "  "):
        assert_connection(current, node(0, missing), node(1, "hard"), allowed)
    absent = replace(node(0), rule_attributes={"hot_roll_grade": "SPHC"})
    assert_connection(current, absent, node(1, None), allowed)


@pytest.mark.parametrize(
    "left_hot,right_hot,allowed",
    [
        ("SPHC", "SPHC", True),
        ("SPHC", "OTHER", False),
        ("sphc", "SPHC", False),
        (" SPHC", "SPHC", False),
        (None, "SPHC", False),
        (" ", " ", False),
    ],
)
def test_fallback_uses_nonblank_exact_hot_roll_grade_not_node_grade(left_hot, right_hot, allowed):
    current = rule(parameters=parameters(missing_grade_policy="fallback_same_hot_roll_grade"))
    left, right = node(0, None, left_hot), node(1, None, right_hot)
    assert_connection(current, left, right, allowed)
    assert_connection(
        current,
        replace(left, grade="different-left"),
        replace(right, grade="different-right"),
        allowed,
    )


@pytest.mark.parametrize("attribute", ["soft_hard_class", "hot_roll_grade"])
@pytest.mark.parametrize("bad_left", [True, False])
def test_consumed_attributes_reject_nontext_values_in_either_direction(attribute, bad_left):
    current = rule(parameters=parameters(missing_grade_policy="fallback_same_hot_roll_grade"))
    for invalid in (1, True, D(1)):
        left, right = node(0, None), node(1, None)
        bad = left if bad_left else right
        bad = replace(bad, rule_attributes=dict(bad.rule_attributes) | {attribute: invalid})
        edge = EdgeRuleSubject("edge", bad if bad_left else left, right if bad_left else bad)
        with pytest.raises(ValueError, match=attribute):
            current.evaluate(edge, context())


def test_real_split_fragments_are_not_exempted_from_class_incompatibility():
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
        D(20),
        1,
        2,
        "split-rule",
        "1",
        "decision",
        "authorized",
    )
    fragment = replace(
        node(0, "soft"),
        source_order_id="source",
        source_resource_id="resource",
        split_lineage=lineage,
    )
    assert_connection(rule(), fragment, node(1, "hard"), False)
    assert_connection(rule(), fragment, node(1, "soft"), True)


@pytest.mark.parametrize(
    "change",
    [
        {"virtual_sphc_allows_bridge": 1},
        {"virtual_sphc_allows_bridge": "true"},
        {"transition_material_breaks_soft_hard": 0},
        {"transition_material_breaks_soft_hard": "false"},
        {"missing_grade_policy": None},
        {"missing_grade_policy": "unknown"},
        {"missing_grade_policy": " "},
    ],
)
def test_invalid_parameter_types_or_policy_are_located_by_loader(change):
    values = parameters(**change)
    with pytest.raises(ValueError):
        rule(parameters=values)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=values))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "gqga4_soft_hard_connection"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


def test_enabled_rule_requires_exactly_three_flat_parameters():
    valid = parameters()
    malformed = [
        {key: value for key, value in valid.items() if key != omitted} for omitted in valid
    ]
    malformed.extend((parameters(extra=True), {"soft_hard": valid}, {}))
    for values in malformed:
        with pytest.raises(ValueError):
            rule(parameters=values)
        with pytest.raises(RuleSetLoadError) as caught:
            load_rule_set(specification(parameters=values))
        assert [item.code for item in caught.value.issues] == ["invalid_rule_parameters"]


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_and_scope_fail_even_for_disabled_rule(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0)), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.CHAIN)


def test_disabled_rule_ignores_business_parameters_attributes_and_contributes_nothing():
    values = {"legacy": ["ignored"], "missing_grade_policy": "unsupported"}
    current = rule(enabled=False, parameters=values)
    spec = specification(enabled=False, parameters=values)
    values["legacy"].clear()
    assert current.parameters["legacy"] == ("ignored",)
    assert current.required_fields() == current.metric_keys() == ()
    bad = EdgeRuleSubject("edge", node(0, True, D(1)), node(1, 1, False))
    assert current.evaluate(bad, object()) == RuleContribution((), ())
    loaded = load_rule_set(spec)
    assert loaded.rules == loaded.rules_for_scope(RuleScope.EDGE) == ()
    assert loaded.evaluate_edge(bad, context()) == RuleContribution((), ())
    with pytest.raises(ValueError):
        rule(enabled=False, parameters={"legacy": D("NaN")})


def test_registered_rule_normalizes_policy_only_and_is_frozen_pure_and_configurable():
    values = parameters(missing_grade_policy=" FaLlBaCk_SaMe_HoT_RoLl_GrAdE ")
    current = rule(parameters=values)
    edge = EdgeRuleSubject("edge", node(0, None), node(1, None))
    before = fingerprint((current, edge, context()))
    spec = specification(parameters=values)
    values["missing_grade_policy"] = "deny"
    loaded = load_rule_set(spec)
    assert SoftHardConnectionRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["SoftHardConnectionRule"] is SoftHardConnectionRule
    assert current.required_fields() == ("hot_roll_grade",)
    assert current.metric_keys() == ()
    assert loaded.rules_for_scope(RuleScope.EDGE) == (current,)
    assert (
        loaded.evaluate_edge(edge, context())
        == current.evaluate(edge, context())
        == RuleContribution((), ())
    )
    assert fingerprint((current, edge, context())) == before
    assert load_rule_set(specification()).evaluate_edge(edge, context()).violations
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["missing_grade_policy"] = "deny"
    assert loaded.fingerprint == spec.fingerprint
    changed = specification(parameters=parameters(virtual_sphc_allows_bridge=False))
    assert changed.fingerprint != specification().fingerprint
    virtual_edge = EdgeRuleSubject("virtual", node(0, role=VIRTUAL), node(1))
    assert load_rule_set(changed).evaluate_edge(virtual_edge, context()).violations
    assert load_rule_set(specification()).evaluate_edge(
        virtual_edge, context()
    ) == RuleContribution((), ())
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(changed, fingerprint=specification().fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]
