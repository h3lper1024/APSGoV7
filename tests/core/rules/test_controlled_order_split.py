"""Controlled split eligibility and authorization, without executing a split."""

from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, Inexact, localcontext
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
    ControlledSplitRuleSubject,
    NodeRuleSubject,
    Rule,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import (
    ContinuousNarrowSteelWeightRule,
    ControlledOrderSplitRule,
)
from apsgo_scheduler.core.rules.helpers import create_controlled_split_decision

D = Decimal
SAME = ControlledSplitMode.SAME_PERIOD_SPLIT
FUTURE = ControlledSplitMode.FUTURE_BORROW_RETURN


def parameters(**changes):
    return {
        "grade_class": "IF",
        "width_upper_exclusive": D(1000),
        "maximum_piece_weight": D(500),
        "minimum_piece_weight": D(50),
        "maximum_accepted_source_count": 3,
        "maximum_separator_node_count": 2,
        "maximum_separator_weight": D(20),
        "allowed_modes": [SAME.value, FUTURE.value],
    } | changes


def rule(**changes):
    return ControlledOrderSplitRule(
        **(
            dict(
                rule_id="controlled_order_split",
                name="受控订单拆分",
                scope=RuleScope.ACTION_ELIGIBILITY,
                enabled=True,
                version="1",
                parameters=parameters(),
            )
            | changes
        )
    )


def node(**changes):
    return Node(
        **(
            dict(
                node_id="parent-node",
                source_order_id="order",
                source_resource_id="resource",
                source_period="P2",
                weight=D(600),
                width=D(900),
                thickness=None,
                min_temperature=None,
                max_temperature=None,
                grade="",
                material_role=MaterialRole.NORMAL_REAL,
                rule_attributes={"grade_class": "IF"},
            )
            | changes
        )
    )


def piece():
    return node(
        split_lineage=SplitLineage(
            "partition",
            "original-parent",
            "order",
            "resource",
            "P2",
            "P2",
            SAME,
            "P2",
            1,
            D(1000),
            1,
            2,
            "split-rule",
            "1",
            "decision",
            "authorized",
        )
    )


def subject(**changes):
    return ControlledSplitRuleSubject(
        **(
            dict(
                subject_id="split-subject",
                parent_node=node(),
                origin_assigned_period="P9",
                source_period="P2",
                accepted_split_source_count=0,
            )
            | changes
        )
    )


def context(periods=("P9", "P2", "P1"), prototypes=("prototype",)):
    return RuleEvaluationContext(periods, dict(zip(periods, range(len(periods)))), prototypes)


def specification(**changes):
    definition = RuleDefinitionSpec(
        "controlled_order_split",
        "ControlledOrderSplitRule",
        "受控订单拆分",
        RuleScope.ACTION_ELIGIBILITY,
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


@pytest.mark.parametrize("origin,mode", [("P9", FUTURE), ("P2", SAME)])
def test_success_authorizes_mode_target_and_all_configured_limits_without_executing(origin, mode):
    current = rule()
    current_subject = subject(origin_assigned_period=origin)
    current_context = context()
    value = current.evaluate_controlled_split(current_subject, current_context)
    assert value == create_controlled_split_decision(
        current_subject,
        current_context,
        eligible=True,
        rule_id=current.rule_id,
        rule_version=current.version,
        reason_code=mode.value,
        mode=mode,
        target_assigned_period="P2",
        maximum_piece_weight=D(500),
        minimum_piece_weight=D(50),
        maximum_accepted_source_count=3,
        maximum_separator_node_count=2,
        maximum_separator_weight=D(20),
    )
    assert value.mode is mode


@pytest.mark.parametrize(
    "current,current_subject,reason",
    [
        (
            rule(enabled=False, parameters={}),
            subject(parent_node=node(material_role=MaterialRole.ACTUAL_TRANSITION)),
            "controlled_order_split_disabled",
        ),
        (
            rule(),
            subject(
                parent_node=node(material_role=MaterialRole.ACTUAL_TRANSITION, rule_attributes={}),
                origin_assigned_period="unknown",
                accepted_split_source_count=3,
            ),
            "unsupported_material_role",
        ),
        (
            rule(),
            subject(
                parent_node=node(
                    source_order_id=None,
                    source_resource_id=None,
                    source_period=None,
                    material_role=MaterialRole.GENERATED_VIRTUAL,
                    rule_attributes={},
                    virtual_lineage=VirtualLineage(
                        "prototype", VirtualPurpose.EDGE_BRIDGE, None, 1
                    ),
                )
            ),
            "unsupported_material_role",
        ),
        (rule(), subject(parent_node=piece(), origin_assigned_period="unknown"), "already_split"),
        (
            rule(),
            subject(source_period="unknown", accepted_split_source_count=3),
            "invalid_period_relation",
        ),
        (rule(), subject(origin_assigned_period="unknown"), "invalid_period_relation"),
        (rule(), subject(parent_node=node(source_period="P9")), "invalid_period_relation"),
        (
            rule(),
            subject(origin_assigned_period="P1", accepted_split_source_count=3),
            "split_source_limit",
        ),
        (
            rule(parameters=parameters(allowed_modes=[])),
            subject(origin_assigned_period="P1", parent_node=node(rule_attributes={})),
            "source_already_late",
        ),
        (
            rule(parameters=parameters(allowed_modes=[])),
            subject(parent_node=node(rule_attributes={})),
            "split_mode_not_allowed",
        ),
        (
            rule(),
            subject(parent_node=node(weight=D(1), rule_attributes={"grade_class": "OTHER"})),
            "not_narrow_real_order",
        ),
        (rule(), subject(parent_node=node(weight=D(500))), "source_weight_within_limit"),
    ],
)
def test_rejections_follow_guard_order_and_return_no_authorized_limits(
    current, current_subject, reason
):
    current_context = context()
    value = current.evaluate_controlled_split(current_subject, current_context)
    assert value == create_controlled_split_decision(
        current_subject,
        current_context,
        eligible=False,
        rule_id=current.rule_id,
        rule_version=current.version,
        reason_code=reason,
    )


@pytest.mark.parametrize(
    "accepted,cap,eligible", [(0, 0, False), (2, 3, True), (3, 3, False), (4, 3, False)]
)
def test_accepted_source_limit_uses_greater_than_or_equal_without_mutating_count(
    accepted, cap, eligible
):
    current_subject = subject(accepted_split_source_count=accepted)
    value = rule(
        parameters=parameters(maximum_accepted_source_count=cap)
    ).evaluate_controlled_split(current_subject, context())
    assert value.eligible is eligible
    if not eligible:
        assert value.reason_code == "split_source_limit"
    assert current_subject.accepted_split_source_count == accepted


@pytest.mark.parametrize(
    "weight,eligible",
    [
        ("500", False),
        ("500.000000999999", False),
        ("500.000001", False),
        ("500.000001000001", True),
        ("1000", True),
    ],
)
def test_exact_weight_epsilon_boundary_does_not_depend_on_decimal_context(weight, eligible):
    current_subject = subject(parent_node=node(weight=D(weight)))
    with localcontext() as decimal_context:
        decimal_context.prec = 3
        decimal_context.traps[Inexact] = True
        value = rule().evaluate_controlled_split(current_subject, context())
    assert value.eligible is eligible
    if not eligible:
        assert value.reason_code == "source_weight_within_limit"


def test_eligibility_does_not_prejudge_tail_piece_or_separator_feasibility():
    current = rule(
        parameters=parameters(maximum_separator_node_count=0, maximum_separator_weight=D(0))
    )
    value = current.evaluate_controlled_split(
        subject(origin_assigned_period="P2", parent_node=node(weight=D("500.000001000001"))),
        context(),
    )
    assert value.eligible
    assert value.minimum_piece_weight == D(50)
    assert value.maximum_separator_node_count == value.maximum_separator_weight == 0


@pytest.mark.parametrize(
    "width,narrow",
    [
        (D.from_float(nextafter(1000.0, -inf)), True),
        (D(1000), False),
        (D.from_float(nextafter(1000.0, inf)), False),
        (None, False),
        (D("999.999999999999999999999"), False),
        (D("1e999"), False),
    ],
)
def test_width_predicate_matches_existing_narrow_rule_without_an_added_epsilon(width, narrow):
    current_node = node(width=width)
    value = rule().evaluate_controlled_split(subject(parent_node=current_node), context())
    narrow_rule = ContinuousNarrowSteelWeightRule(
        "narrow",
        "窄钢连续重量",
        RuleScope.CHAIN,
        True,
        "1",
        {"grade_class": "IF", "width_upper_exclusive": D(1000), "max_real_weight": D(500)},
    )
    contribution = narrow_rule.evaluate(
        ChainRuleSubject("chain", Chain("chain", (current_node,), "P2")), context()
    )
    assert value.eligible is narrow
    assert contribution.metrics[0].value == (D(600) if narrow else D(0))


@pytest.mark.parametrize(
    "configured,actual,eligible",
    [("IF", " IF ", True), ("IF", "if", False), (" IF ", " IF ", False)],
)
def test_grade_class_strips_input_only_and_preserves_configuration_case_and_whitespace(
    configured, actual, eligible
):
    current = rule(parameters=parameters(grade_class=configured))
    value = current.evaluate_controlled_split(
        subject(parent_node=node(rule_attributes={"grade_class": actual})), context()
    )
    assert value.eligible is eligible


@pytest.mark.parametrize(
    "attributes", [{}, {"grade_class": None}, {"grade_class": " "}, {"grade_class": 1}]
)
def test_invalid_real_grade_is_rejected_only_after_prior_eligibility_guards(attributes):
    current_subject = subject(parent_node=node(rule_attributes=attributes))
    with pytest.raises(ValueError, match="grade_class"):
        rule().evaluate_controlled_split(current_subject, context())
    assert (
        rule(enabled=False, parameters={})
        .evaluate_controlled_split(current_subject, context())
        .reason_code
        == "controlled_order_split_disabled"
    )


def test_allowed_modes_are_explicit_ordered_strings_and_can_be_empty_or_duplicated():
    request = subject()
    for modes, eligible in (
        ([], False),
        ([SAME.value], False),
        ([FUTURE.value], True),
        ([FUTURE.value, FUTURE.value, SAME.value], True),
    ):
        current = rule(parameters=parameters(allowed_modes=modes))
        assert current.parameters["allowed_modes"] == tuple(modes)
        assert current.evaluate_controlled_split(request, context()).eligible is eligible


def test_context_period_order_controls_future_same_and_late_without_name_sorting_or_fixed_count():
    current = rule()
    request = subject()
    assert current.evaluate_controlled_split(request, context()).mode is FUTURE
    assert (
        current.evaluate_controlled_split(request, context(("P1", "P2", "P9"))).reason_code
        == "source_already_late"
    )
    same_request = subject(origin_assigned_period="P2")
    for periods in (("P2",), ("z", "P9", "a", "P2", "P1")):
        assert current.evaluate_controlled_split(same_request, context(periods)).mode is SAME


@pytest.mark.parametrize(
    "config",
    [
        {key: value for key, value in parameters().items() if key != missing}
        for missing in parameters()
    ]
    + [
        parameters(extra=None),
        parameters(grade_class=" "),
        parameters(grade_class=1),
        parameters(width_upper_exclusive=D(0)),
        parameters(width_upper_exclusive=1000),
        parameters(width_upper_exclusive=D("1e999")),
        parameters(maximum_piece_weight=D(0)),
        parameters(minimum_piece_weight=D(0)),
        parameters(minimum_piece_weight=D(501)),
        parameters(maximum_accepted_source_count=True),
        parameters(maximum_accepted_source_count=-1),
        parameters(maximum_separator_node_count=True),
        parameters(maximum_separator_node_count=-1),
        parameters(maximum_separator_weight=D(-1)),
        parameters(maximum_separator_weight=20),
        parameters(allowed_modes=SAME.value),
        parameters(allowed_modes=["unknown"]),
        parameters(allowed_modes=[" SAME_PERIOD_SPLIT "]),
        parameters(allowed_modes=[1]),
    ],
)
def test_enabled_configuration_requires_all_eight_exact_parameters_with_located_diagnostics(config):
    with pytest.raises(ValueError):
        rule(parameters=config)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=config))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "controlled_order_split"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("enabled", [True, False])
def test_subject_and_context_types_are_validated_before_disabled_rejection(enabled):
    current = rule(enabled=enabled)
    with pytest.raises(UnsupportedRuleSubjectError):
        current.evaluate_controlled_split(NodeRuleSubject("node", node()), context())
    with pytest.raises(ValueError, match="RuleEvaluationContext"):
        current.evaluate_controlled_split(subject(), None)
    with pytest.raises(UnsupportedRuleSubjectError):
        current.evaluate(subject(), context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.CHAIN)


def test_disabled_configuration_still_validates_generic_value_shapes():
    for config in (
        parameters(maximum_piece_weight=500.0),
        parameters(maximum_separator_weight=D("Infinity")),
    ):
        with pytest.raises(ValueError):
            rule(enabled=False, parameters=config)
        with pytest.raises(ValueError):
            specification(enabled=False, parameters=config)


def test_decision_fingerprint_binds_complete_subject_context_identity_and_returned_limits():
    current, current_subject, current_context = rule(), subject(), context()
    value = current.evaluate_controlled_split(current_subject, current_context)
    values = {
        field.name: getattr(value, field.name)
        for field in fields(value)
        if field.name != "decision_fingerprint"
    }
    assert value.decision_fingerprint == fingerprint(
        {
            "subject": current_subject,
            "context": current_context,
            "decision": values,
        }
    )
    rebuilt = RuleEvaluationContext(
        list(current_context.period_order), {"P1": 2, "P2": 1, "P9": 0}, ["prototype"]
    )
    assert current.evaluate_controlled_split(current_subject, rebuilt) == value
    alternatives = [
        current.evaluate_controlled_split(
            replace(current_subject, subject_id="other-subject"), current_context
        ),
        current.evaluate_controlled_split(
            replace(current_subject, accepted_split_source_count=1), current_context
        ),
        current.evaluate_controlled_split(
            replace(current_subject, parent_node=node(weight=D(601))), current_context
        ),
        current.evaluate_controlled_split(
            replace(current_subject, parent_node=node(width=D(901))), current_context
        ),
        current.evaluate_controlled_split(
            current_subject, context(prototypes=("other-prototype",))
        ),
        current.evaluate_controlled_split(current_subject, context(("extra", "P9", "P2", "P1"))),
        rule(version="2").evaluate_controlled_split(current_subject, current_context),
        rule(parameters=parameters(maximum_piece_weight=D(501))).evaluate_controlled_split(
            current_subject, current_context
        ),
        rule(parameters=parameters(maximum_separator_node_count=0)).evaluate_controlled_split(
            current_subject, current_context
        ),
    ]
    assert all(item.eligible for item in alternatives)
    assert (
        len({value.decision_fingerprint, *(item.decision_fingerprint for item in alternatives)})
        == len(alternatives) + 1
    )


def test_registered_rule_is_frozen_detached_and_stateless_across_qualification_calls():
    modes = [SAME.value, FUTURE.value]
    current = rule(parameters=parameters(allowed_modes=modes))
    current_subject, current_context = subject(), context()
    before = fingerprint((current, current_subject, current_context))
    modes.clear()
    assert current.parameters["allowed_modes"] == (SAME.value, FUTURE.value)
    assert ControlledOrderSplitRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["ControlledOrderSplitRule"] is ControlledOrderSplitRule
    assert current.required_fields() == ("grade_class",)
    assert current.metric_keys() == ()
    loaded = load_rule_set(specification())
    assert loaded.rules_for_scope(RuleScope.ACTION_ELIGIBILITY) == (current,)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == ()
    first = current.evaluate_controlled_split(current_subject, current_context)
    assert loaded.evaluate_controlled_split(current_subject, current_context) == first
    assert (
        current.evaluate_controlled_split(
            subject(parent_node=node(weight=D(1))), current_context
        ).reason_code
        == "source_weight_within_limit"
    )
    assert current.evaluate_controlled_split(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    assert [criterion.metric_key for criterion in loaded.quality_spec] == [
        "prohibited_violation_count",
        "prohibited_violation_severity",
    ]
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(FrozenInstanceError):
        first.eligible = False
    with pytest.raises(TypeError):
        current.parameters["maximum_accepted_source_count"] = 99


def test_disabled_loading_keeps_configuration_identity_and_canonical_task_bound_rejection():
    current_subject, current_context = subject(parent_node=node(rule_attributes={})), context()
    disabled = rule(enabled=False, parameters={})
    assert disabled.required_fields() == disabled.metric_keys() == ()
    expected = disabled.evaluate_controlled_split(current_subject, current_context)
    spec = specification(enabled=False, parameters={"allowed_modes": ["legacy"], "extra": [1]})
    loaded = load_rule_set(spec)
    assert loaded.rules == loaded.rules_for_scope(RuleScope.ACTION_ELIGIBILITY) == ()
    assert loaded.evaluate_controlled_split(current_subject, current_context) == expected
    assert spec.fingerprint != specification().fingerprint
    assert spec.fingerprint != specification(enabled=False, parameters={}).fingerprint
    assert (
        specification().fingerprint
        != specification(
            parameters=parameters(allowed_modes=[FUTURE.value, SAME.value])
        ).fingerprint
    )
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(spec, fingerprint=specification().fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]


def test_loader_rejects_wrong_scope():
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(scope=RuleScope.CHAIN))
    assert [(item.code, item.field_path) for item in caught.value.issues] == [
        ("rule_scope_mismatch", "rule_set_spec.rules[0].scope")
    ]
