"""Task-ordered original-period protection, with no resource ledger or scheduling mutation."""

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
    SchedulePlan,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    NodeRuleSubject,
    PlanRuleSubject,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import LateOriginalPeriodMoveRule

D = Decimal
METRIC = "late_original_due_period_move_count"


def rule(**changes):
    return LateOriginalPeriodMoveRule(
        **(
            dict(
                rule_id="forbid_late_original_due_period",
                name="原订单延后计划期",
                scope=RuleScope.PLAN,
                enabled=True,
                version="1",
                parameters={},
            )
            | changes
        )
    )


def node(index, source="z-first", role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else source,
        weight=D(10),
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


def context(periods=("z-first", "a-second", "m-third")):
    return RuleEvaluationContext(
        periods, {period: index for index, period in enumerate(periods)}, ()
    )


def subject(source="z-first", assigned="a-second", role=MaterialRole.NORMAL_REAL):
    chain = Chain("chain-0", (node(0, source, role),), assigned)
    return PlanRuleSubject("plan-subject", SchedulePlan((chain,)), None)


def specification(**changes):
    definition = RuleDefinitionSpec(
        "forbid_late_original_due_period",
        "LateOriginalPeriodMoveRule",
        "原订单延后计划期",
        RuleScope.PLAN,
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
    "periods,source,assigned,late",
    [
        (("only",), "only", "only", False),
        (("z", "a"), "z", "a", True),
        (("z", "a"), "a", "z", False),
        (("z", "a"), "z", "z", False),
        (("Q4", "Q1", "Q9", "Q2", "Q0"), "Q4", "Q0", True),
        (("Q4", "Q1", "Q9", "Q2", "Q0"), "Q0", "Q4", False),
        (("Q4", "Q1", "Q9", "Q2", "Q0"), "Q9", "Q2", True),
        (("Q4", "Q1", "Q9", "Q2", "Q0"), "Q2", "Q9", False),
    ],
)
def test_lateness_uses_explicit_task_order_not_names_or_a_fixed_period_count(
    periods, source, assigned, late
):
    current = rule()
    result = current.evaluate(subject(source, assigned), context(periods))
    assert [(item.metric_key, item.value) for item in result.metrics] == [(METRIC, int(late))]
    assert type(result.metrics[0].value) is int
    assert len(result.violations) == int(late)
    if late:
        violation = result.violations[0]
        assert violation.rule_id == current.rule_id
        assert violation.scope is RuleScope.PLAN
        assert violation.subject_id == "node-0"
        assert violation.reason_code == "late_original_due_period_move"
        assert violation.disposition is RuleDisposition.PROHIBITED
        assert violation.severity == D(1) and type(violation.severity) is Decimal
        assert source in violation.message and assigned in violation.message


def test_multiple_chains_count_real_transition_and_each_split_piece_in_traversal_order():
    lineage = SplitLineage(
        "partition",
        "parent",
        "source",
        "resource",
        "z-first",
        "z-first",
        ControlledSplitMode.SAME_PERIOD_SPLIT,
        "z-first",
        1,
        D(20),
        1,
        2,
        "split-rule",
        "1",
        "decision",
        "authorized",
    )
    fragments = tuple(
        replace(
            node(index + 3),
            source_order_id="source",
            source_resource_id="resource",
            split_lineage=replace(lineage, piece_index=index),
        )
        for index in (1, 2)
    )
    first = Chain(
        "first-chain",
        (
            node(0),
            node(1, role=MaterialRole.ACTUAL_TRANSITION),
            node(2, role=MaterialRole.GENERATED_VIRTUAL),
            node(3, "m-third"),
        ),
        "a-second",
    )
    last = Chain("last-chain", (*fragments, node(6, "m-third")), "m-third")
    current = PlanRuleSubject("plan", SchedulePlan((first, last)), None)
    result = rule().evaluate(current, context())
    assert [item.subject_id for item in result.violations] == [
        "node-0",
        "node-1",
        "node-4",
        "node-5",
    ]
    assert [item.severity for item in result.violations] == [D(1)] * 4
    assert result.metrics[0].value == 4
    assert first.nodes[2].source_period is None


@pytest.mark.parametrize("role", [MaterialRole.NORMAL_REAL, MaterialRole.ACTUAL_TRANSITION])
@pytest.mark.parametrize("unknown_field", ["assigned_period", "source_period"])
def test_unknown_periods_fail_with_chain_or_node_field_location(role, unknown_field):
    source = "unknown" if unknown_field == "source_period" else "z-first"
    assigned = "unknown" if unknown_field == "assigned_period" else "a-second"
    owner = "node-0" if unknown_field == "source_period" else "chain-0"
    with pytest.raises(ValueError, match=rf"{owner}.*{unknown_field}"):
        rule().evaluate(subject(source, assigned, role), context())


@pytest.mark.parametrize("source", [None, ""])
def test_real_source_period_cannot_be_missing_in_the_core_node(source):
    with pytest.raises(ValueError, match="source_period"):
        node(0, source)


@pytest.mark.parametrize("invalid", [None, object(), {"z-first": 0, "a-second": 1}])
def test_enabled_rule_requires_the_typed_context(invalid):
    with pytest.raises(ValueError, match="context"):
        rule().evaluate(subject(), invalid)


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_and_scope_fail_even_when_disabled(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0)), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.CHAIN)


@pytest.mark.parametrize(
    "parameters",
    [{"allow_late": True}, {"maximum_delay": 0}, {"period_order": ["z-first", "a-second"]}],
)
def test_enabled_rule_accepts_only_empty_parameters_and_loader_locates_error(parameters):
    with pytest.raises(ValueError):
        rule(parameters=parameters)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=parameters))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "forbid_late_original_due_period"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("invalid", [D("NaN"), 1.5, {1: "period"}])
def test_disabled_rule_still_validates_generic_parameter_shapes(invalid):
    with pytest.raises(ValueError):
        rule(enabled=False, parameters={"legacy": invalid})
    with pytest.raises(ValueError):
        specification(enabled=False, parameters={"legacy": invalid})


def test_disabled_rule_never_reads_context_resources_or_periods_and_emits_nothing():
    parameters = {"legacy": {"period_order": ["z", "a"], "allow_late": False}}
    disabled = rule(enabled=False, parameters=parameters)
    value = specification(enabled=False, parameters=parameters)
    parameters["legacy"]["period_order"].clear()
    assert disabled.parameters["legacy"]["period_order"] == ("z", "a")
    invalid_periods = replace(subject("unknown-source", "unknown-assigned"), resource_view=object())
    assert disabled.evaluate(invalid_periods, object()) == RuleContribution((), ())
    assert disabled.required_fields() == disabled.metric_keys() == ()
    loaded = load_rule_set(value)
    assert loaded.rules == loaded.rules_for_scope(RuleScope.PLAN) == ()
    assert loaded.evaluate_plan(invalid_periods, context()) == RuleContribution((), ())


def test_registered_rule_is_frozen_and_stateless_between_oppositely_ordered_tasks():
    current, current_subject = rule(), subject()
    first = context(("z-first", "a-second"))
    reverse = context(("a-second", "z-first"))
    before = fingerprint((current, current_subject, first, reverse))
    assert LateOriginalPeriodMoveRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["LateOriginalPeriodMoveRule"] is LateOriginalPeriodMoveRule
    assert current.required_fields() == ("source_period",)
    assert current.metric_keys() == (METRIC,)
    loaded = load_rule_set(specification())
    assert loaded.rules_for_scope(RuleScope.PLAN) == (current,)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == ()
    for task_context, expected in ((first, 1), (reverse, 0), (first, 1)):
        result = current.evaluate(current_subject, task_context)
        assert result.metrics[0].value == expected
        assert loaded.evaluate_plan(current_subject, task_context) == result
        assert (
            current.evaluate(replace(current_subject, resource_view=object()), task_context)
            == result
        )
    assert fingerprint((current, current_subject, first, reverse)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["extra"] = 1


def test_configuration_scope_and_full_fingerprint_are_verified_before_execution():
    enabled, disabled = specification(), specification(enabled=False)
    assert load_rule_set(enabled).fingerprint == enabled.fingerprint
    assert enabled.fingerprint != disabled.fingerprint
    with pytest.raises(RuleSetLoadError) as scope_error:
        load_rule_set(specification(scope=RuleScope.CHAIN))
    assert [item.code for item in scope_error.value.issues] == ["rule_scope_mismatch"]
    with pytest.raises(RuleSetLoadError) as fingerprint_error:
        load_rule_set(replace(disabled, fingerprint=enabled.fingerprint))
    assert [item.code for item in fingerprint_error.value.issues] == [
        "rule_set_fingerprint_mismatch"
    ]
