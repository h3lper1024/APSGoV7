"""Golden high-surface run counting, configuration and rule-set dispatch."""

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
    NodeRuleSubject,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import HighSurfaceRunCountRule

D = Decimal


def node(index, surface="FC", role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D("10"),
        width=D("1200"),
        thickness=D("1"),
        min_temperature=None,
        max_temperature=None,
        grade="steel",
        material_role=role,
        rule_attributes={"surface_grade": surface},
        virtual_lineage=VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1)
        if virtual
        else None,
    )


def rule(**changes):
    return HighSurfaceRunCountRule(
        **(
            dict(
                rule_id="chain_high_surface_run_count_lte",
                name="高表面连续数量",
                scope=RuleScope.CHAIN,
                enabled=True,
                version="1",
                parameters={"surface_grades": ["FC", "FD"], "max_run_count": 5},
            )
            | changes
        )
    )


def subject(nodes):
    return ChainRuleSubject("chain-subject", Chain("chain", tuple(nodes), "period"))


def context():
    return RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def loaded_rule_set(value):
    definition = RuleDefinitionSpec(
        value.rule_id,
        value.rule_type,
        value.name,
        value.scope,
        value.enabled,
        value.version,
        value.parameters,
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


@pytest.mark.parametrize("count,excess", [(1, 0), (4, 0), (5, 0), (6, 1), (9, 4)])
def test_reference_limit_boundary_and_excess_are_per_maximal_run(count, excess):
    current = rule()
    value = current.evaluate(subject(node(index) for index in range(count)), context())
    assert [(metric.metric_key, metric.value) for metric in value.metrics] == [
        ("max_high_surface_run_count", count)
    ]
    assert type(value.metrics[0].value) is int
    if not excess:
        assert value.violations == ()
        return
    assert len(value.violations) == 1
    violation = value.violations[0]
    assert violation.rule_id == current.rule_id
    assert violation.scope is RuleScope.CHAIN
    assert violation.subject_id == f"chain-subject:{current.rule_id}:0-{count - 1}"
    assert violation.reason_code == "high_surface_run_count"
    assert violation.disposition is RuleDisposition.PROHIBITED
    assert violation.severity == D(excess)
    assert isinstance(violation.severity, Decimal)
    assert violation.message


def test_zero_limit_rejects_one_matching_node_but_nonmatching_run_is_zero():
    current = rule(parameters={"surface_grades": ("FC", "FD"), "max_run_count": 0})
    value = current.evaluate(subject((node(0, "FB"), node(1, "FC"))), context())
    assert [item.severity for item in value.violations] == [D(1)]
    assert value.violations[0].subject_id.endswith(":1-1")
    no_match = current.evaluate(subject((node(0, "FB"), node(1, "FA"))), context())
    assert no_match.violations == ()
    assert no_match.metrics[0].value == 0


def test_mixed_fc_fd_and_normalized_text_remain_one_continuous_run():
    current = rule(parameters={"surface_grades": [" fc ", "fD"], "max_run_count": 5})
    grades = ("FC", "fd", " fc ", "FD", "FC", "Fd")
    value = current.evaluate(
        subject(node(index, grade) for index, grade in enumerate(grades)), context()
    )
    assert [item.subject_id for item in value.violations] == [
        f"chain-subject:{current.rule_id}:0-5"
    ]
    assert value.violations[0].severity == 1
    assert value.metrics[0].value == 6


def test_grade_membership_and_count_limit_come_from_configuration():
    current_subject = subject((node(0, "CUSTOM"), node(1, "custom")))
    for grades, limit, maximum, prohibited in (
        (["FC"], 1, 0, 0),
        (["CUSTOM"], 1, 2, 1),
        (["CUSTOM"], 2, 2, 0),
    ):
        value = rule(parameters={"surface_grades": grades, "max_run_count": limit}).evaluate(
            current_subject, context()
        )
        assert value.metrics[0].value == maximum
        assert len(value.violations) == prohibited


def test_multiple_and_trailing_runs_produce_one_record_each_in_chain_order():
    grades = ("FB", "FC", "FD", "FC", "FB", "FD", "FC", "FD", "FC")
    current = rule(parameters={"surface_grades": ["FC", "FD"], "max_run_count": 2})
    value = current.evaluate(
        subject(node(index, grade) for index, grade in enumerate(grades)), context()
    )
    assert [(item.subject_id, item.severity) for item in value.violations] == [
        (f"chain-subject:{current.rule_id}:1-3", D(1)),
        (f"chain-subject:{current.rule_id}:5-8", D(2)),
    ]
    assert value.metrics[0].value == 4
    reversed_value = current.evaluate(
        subject(node(index, grade) for index, grade in enumerate(reversed(grades))), context()
    )
    assert [(item.subject_id, item.severity) for item in reversed_value.violations] == [
        (f"chain-subject:{current.rule_id}:0-3", D(2)),
        (f"chain-subject:{current.rule_id}:5-7", D(1)),
    ]


@pytest.mark.parametrize("role", [MaterialRole.ACTUAL_TRANSITION, MaterialRole.GENERATED_VIRTUAL])
@pytest.mark.parametrize("surface", ["FC", None])
def test_transition_and_generated_material_break_runs_even_with_high_or_missing_grade(
    role, surface
):
    current = rule(parameters={"surface_grades": ["FC", "FD"], "max_run_count": 2})
    nodes = (node(0), node(1, "FD"), node(2, surface, role), node(3), node(4))
    value = current.evaluate(subject(nodes), context())
    assert value.violations == ()
    assert value.metrics[0].value == 2


def test_split_fragments_of_one_source_each_count_as_a_real_node():
    fragments = []
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
            parent_weight=D("20"),
            piece_index=index,
            piece_count=2,
            authorization_rule_id="split-rule",
            authorization_rule_version="1",
            authorization_decision_fingerprint="decision",
            reason_code="authorized",
        )
        fragments.append(
            replace(
                node(index),
                source_order_id="source",
                source_resource_id="resource",
                split_lineage=lineage,
            )
        )
    value = rule(parameters={"surface_grades": ["FC"], "max_run_count": 1}).evaluate(
        subject(fragments), context()
    )
    assert len(value.violations) == 1
    assert value.violations[0].severity == 1
    assert value.metrics[0].value == 2


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"surface_grades": ["FC"]},
        {"max_run_count": 5},
        {"surface_grades": "FC,FD", "max_run_count": 5},
        {"surface_grades": [], "max_run_count": 5},
        {"surface_grades": [""], "max_run_count": 5},
        {"surface_grades": [" "], "max_run_count": 5},
        {"surface_grades": [1], "max_run_count": 5},
        {"surface_grades": [["FC"]], "max_run_count": 5},
        {"surface_grades": ["FC"], "max_run_count": -1},
        {"surface_grades": ["FC"], "max_run_count": True},
        {"surface_grades": ["FC"], "max_run_count": Decimal("5")},
        {"surface_grades": ["FC"], "max_run_count": "5"},
    ],
)
def test_enabled_rule_requires_explicit_valid_business_parameters(parameters):
    with pytest.raises(ValueError):
        rule(parameters=parameters)


@pytest.mark.parametrize(
    "attributes",
    [
        {"surface_grade": 1},
        {"surface_grade": True},
    ],
)
def test_normal_real_nontext_surface_grade_still_fails_closed(attributes):
    with pytest.raises(ValueError, match="surface_grade"):
        rule().evaluate(subject((replace(node(0), rule_attributes=attributes),)), context())


@pytest.mark.parametrize("at_end", [False, True])
def test_missing_grade_in_a_nonmatching_group_is_allowed(at_end):
    nodes = [node(0, "FB"), node(1, None)]
    if not at_end:
        nodes.append(node(2, "FC"))
    result = rule().evaluate(subject(nodes), context())
    assert result.violations == ()
    assert result.metrics[0].value == (0 if at_end else 1)


@pytest.mark.parametrize(
    "attributes", [{}, {"surface_grade": None}, {"surface_grade": ""}, {"surface_grade": " \t "}]
)
def test_missing_or_empty_surface_is_allowed_and_breaks_a_run(attributes):
    middle = replace(node(1), rule_attributes=attributes)
    current = rule(parameters={"surface_grades": ["FC", "FD"], "max_run_count": 1})
    single = current.evaluate(subject((middle,)), context())
    assert single.violations == ()
    assert single.metrics[0].value == 0
    result = current.evaluate(subject((node(0), middle, node(2))), context())
    assert result.violations == ()
    assert result.metrics[0].value == 1
    assert (
        loaded_rule_set(current).evaluate_chain(subject((node(0), middle, node(2))), context())
        == result
    )


def test_registered_rule_loads_without_line_specific_core_changes_and_is_pure():
    parameters = {"surface_grades": ["FC", "FD"], "max_run_count": 1}
    current = rule(parameters=parameters)
    assert HighSurfaceRunCountRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["HighSurfaceRunCountRule"] is HighSurfaceRunCountRule
    assert current.required_fields() == ()
    assert current.metric_keys() == ("max_high_surface_run_count",)
    current_subject = subject((node(0), node(1, "FD")))
    current_context = context()
    before = fingerprint((current_subject, current_context, current))
    parameters["surface_grades"].clear()
    parameters["max_run_count"] = 99
    first = current.evaluate(current_subject, current_context)
    loaded = loaded_rule_set(current)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == (current,)
    assert loaded.evaluate_chain(current_subject, current_context) == first
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current_subject, current_context, current)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["max_run_count"] = 99


def test_disabled_rule_ignores_business_parameters_input_and_contributions():
    disabled = rule(enabled=False, parameters={})
    malformed_for_enabled = subject((replace(node(0), rule_attributes={}),))
    assert disabled.evaluate(malformed_for_enabled, context()) == RuleContribution((), ())
    loaded = loaded_rule_set(disabled)
    assert loaded.rules == ()
    assert loaded.rules_for_scope(RuleScope.CHAIN) == ()
    assert loaded.evaluate_chain(malformed_for_enabled, context()) == RuleContribution((), ())


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_is_rejected_before_enabled_dispatch(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0)), context())


def test_loader_reports_invalid_business_parameters_before_search():
    invalid = rule(enabled=False, parameters={})
    definition = RuleDefinitionSpec(
        invalid.rule_id,
        invalid.rule_type,
        invalid.name,
        invalid.scope,
        True,
        invalid.version,
        invalid.parameters,
    )
    spec = RuleSetSpec("LINE", "PROCESS", "month", "1", (definition,), (), frozenset(), "pending")
    with pytest.raises(RuleSetLoadError) as failure:
        load_rule_set(replace(spec, fingerprint=fingerprint_rule_set_spec(spec)))
    assert [issue.code for issue in failure.value.issues] == ["invalid_rule_parameters"]
