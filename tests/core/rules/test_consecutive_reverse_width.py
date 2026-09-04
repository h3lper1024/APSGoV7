"""Consecutive adjacent width increases, independent of grade and material role."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
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
from apsgo_scheduler.core.rules.concrete import ConsecutiveReverseWidthRule

D = Decimal
METRIC = "consecutive_reverse_width_violation_count"


def rule(**changes):
    return ConsecutiveReverseWidthRule(
        **(
            dict(
                rule_id="forbid_consecutive_reverse_width",
                name="连续逆宽",
                scope=RuleScope.CHAIN,
                enabled=True,
                version="1",
                parameters={},
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
        "forbid_consecutive_reverse_width",
        "ConsecutiveReverseWidthRule",
        "连续逆宽",
        RuleScope.CHAIN,
        True,
        "1",
        {},
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
    "widths,edges",
    [
        ((1000,), ()),
        ((1000, 1010), ()),
        ((1000, 1010, 1020), ("1-2",)),
        ((1000, 1010, 1020, 1030), ("1-2", "2-3")),
        ((1030, 1020, 1010, 1000), ()),
        ((1000, 1010, 1005), ()),
        ((1000, 1010, 1010), ()),
        ((1000, 1010, 1005, 1007, 1009), ("3-4",)),
        ((1000, 1010, 1010, 1020, 1030), ("3-4",)),
        ((1000, 1010, 1020, 1005, 1007, 1009), ("1-2", "4-5")),
        ((None, 1010, 1020, 1030), ("2-3",)),
        ((1000, 1010, None, 1020, 1030), ()),
        ((1000, 1010, 1020, None), ("1-2",)),
        ((None, None, None), ()),
    ],
)
def test_only_consecutive_adjacent_increases_produce_ordered_edge_violations(widths, edges):
    current = rule()
    value = current.evaluate(
        subject(node(index, width) for index, width in enumerate(widths)), context()
    )
    assert [(item.metric_key, item.value) for item in value.metrics] == [(METRIC, len(edges))]
    assert type(value.metrics[0].value) is int
    assert [item.subject_id for item in value.violations] == [
        f"chain-subject:{current.rule_id}:{edge}" for edge in edges
    ]
    for item in value.violations:
        assert item.rule_id == current.rule_id
        assert item.scope is RuleScope.CHAIN
        assert item.reason_code == "consecutive_reverse_width"
        assert item.disposition is RuleDisposition.PROHIBITED
        assert item.severity == D(1)
        assert type(item.severity) is Decimal
        assert item.message


@pytest.mark.parametrize(
    "right,violations",
    [
        (D.from_float(nextafter(1010.0 + 1e-9, -inf)), 0),
        (D.from_float(1010.0 + 1e-9), 0),
        (D.from_float(nextafter(1010.0 + 1e-9, inf)), 1),
        (D("1010.00000000100001"), 0),
    ],
)
def test_increase_uses_strict_float_plus_epsilon_not_decimal_subtraction(right, violations):
    value = rule().evaluate(subject((node(0, 1000), node(1, 1010), node(2, right))), context())
    assert len(value.violations) == value.metrics[0].value == violations


@pytest.mark.parametrize("role", tuple(MaterialRole))
def test_roles_and_missing_grade_do_not_reset_or_exempt_width_increases(role):
    nodes = (node(0, 1000), node(1, 1010, role), node(2, 1020))
    assert rule().evaluate(subject(nodes), context()).metrics[0].value == 1


def test_split_fragments_keep_their_actual_width_and_are_not_deduplicated_by_source():
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
            node(index + 1, 1020),
            source_order_id="source",
            source_resource_id="resource",
            split_lineage=replace(lineage, piece_index=index),
        )
        for index in (1, 2)
    )
    nodes = (node(0, 1000), node(1, 1010), *fragments, node(4, 1030), node(5, 1040))
    value = rule().evaluate(subject(nodes), context())
    assert [item.subject_id.rsplit(":", 1)[-1] for item in value.violations] == ["1-2", "4-5"]
    assert value.metrics[0].value == 2


@pytest.mark.parametrize(
    "parameters",
    [
        {"allow_consecutive_reverse_width": True},
        {"allow_consecutive_reverse_width": False},
        {"reverse_width_carrier_grades": ["SPHC"]},
        {"unexpected": None},
    ],
)
def test_enabled_rule_rejects_all_business_parameters_with_located_loading_error(parameters):
    with pytest.raises(ValueError):
        rule(parameters=parameters)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=parameters))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "forbid_consecutive_reverse_width"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_is_rejected_even_when_disabled(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0, 1000)), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.EDGE)


def test_registered_rule_is_frozen_pure_and_uses_no_extra_required_fields():
    parameters = {}
    current = rule(parameters=parameters)
    current_subject = subject(
        (
            node(0, 1000),
            node(1, 1010, MaterialRole.ACTUAL_TRANSITION),
            node(2, 1020, MaterialRole.GENERATED_VIRTUAL),
            node(3, 1030),
        )
    )
    current_context = context()
    before = fingerprint((current, current_subject, current_context))
    parameters["allow_consecutive_reverse_width"] = True
    assert ConsecutiveReverseWidthRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["ConsecutiveReverseWidthRule"] is ConsecutiveReverseWidthRule
    assert current.required_fields() == ()
    assert current.metric_keys() == (METRIC,)
    loaded = load_rule_set(specification())
    assert loaded.rules_for_scope(RuleScope.CHAIN) == (current,)
    first = current.evaluate(current_subject, current_context)
    assert len(first.violations) == 2
    assert loaded.evaluate_chain(current_subject, current_context) == first
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["extra"] = 1


def test_disabled_legacy_parameters_are_frozen_identity_only_and_unknown_type_is_not_loaded():
    parameters = {
        "allow_consecutive_reverse_width": False,
        "reverse_width_carrier_grades": ["SPHC"],
    }
    disabled = rule(enabled=False, parameters=parameters)
    value = specification(enabled=False, parameters=parameters)
    parameters["reverse_width_carrier_grades"].clear()
    assert disabled.parameters["reverse_width_carrier_grades"] == ("SPHC",)
    assert disabled.required_fields() == disabled.metric_keys() == ()
    current_subject = subject((node(0, 1000), node(1, 1010), node(2, 1020)))
    assert disabled.evaluate(current_subject, context()) == RuleContribution((), ())
    loaded = load_rule_set(value)
    assert loaded.rules == loaded.rules_for_scope(RuleScope.CHAIN) == ()
    assert loaded.evaluate_chain(current_subject, context()) == RuleContribution((), ())
    unknown = specification(
        enabled=False, rule_type="LegacyReverseWidthRule", parameters=parameters
    )
    assert load_rule_set(unknown, registry={}).rules == ()
    assert value.fingerprint != specification(enabled=False).fingerprint
    assert value.fingerprint != specification().fingerprint
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(value, fingerprint=specification().fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]
