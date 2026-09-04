"""Frozen rule values, fail-closed dispatch and task-bound split decisions."""

from dataclasses import FrozenInstanceError, dataclass, fields, replace
from decimal import Decimal
from typing import ClassVar

import pytest

from apsgo_scheduler.core.contracts import (
    ControlledSplitMode,
    RuleScope,
    canonical_json,
    fingerprint,
    freeze_rule_parameters,
    freeze_scalars,
)
from apsgo_scheduler.core.model import Chain, MaterialRole, Node, SchedulePlan
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    ControlledSplitDecision,
    ControlledSplitRuleSubject,
    EdgeRuleSubject,
    MetricContribution,
    NodeRuleSubject,
    NumericProjection,
    PlanRuleSubject,
    QualityAggregation,
    QualityCriterion,
    QualityDirection,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    RuleViolation,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.helpers import create_controlled_split_decision

D = Decimal


def node(**changes):
    return Node(
        **(
            dict(
                node_id="node",
                source_order_id="order",
                source_resource_id="resource",
                source_period="p-z",
                weight=D("12"),
                width=D("1000"),
                thickness=D("1"),
                min_temperature=None,
                max_temperature=None,
                grade="steel",
                material_role=MaterialRole.NORMAL_REAL,
                rule_attributes={},
            )
            | changes
        )
    )


def context():
    return RuleEvaluationContext(("p-z", "p-a"), {"p-z": 0, "p-a": 1}, ())


def subject():
    return ControlledSplitRuleSubject("split", node(), "p-z", "p-z", 0)


def decision(**changes):
    return create_controlled_split_decision(
        subject(),
        context(),
        **(
            dict(
                eligible=True,
                rule_id="split-rule",
                rule_version="v1",
                reason_code="same_period_allowed",
                mode=ControlledSplitMode.SAME_PERIOD_SPLIT,
                target_assigned_period="p-z",
                maximum_piece_weight=D("10"),
                minimum_piece_weight=D("1"),
                maximum_accepted_source_count=3,
                maximum_separator_node_count=2,
                maximum_separator_weight=D("8"),
            )
            | changes
        ),
    )


@dataclass(frozen=True, slots=True)
class DeclaredOnlyRule(Rule):
    supported_scope: ClassVar[RuleScope] = RuleScope.EDGE

    def required_fields(self):
        return ()


def rule(**changes):
    return DeclaredOnlyRule(
        **(
            dict(
                rule_id="declared",
                name="Declared",
                scope=RuleScope.EDGE,
                enabled=True,
                version="v1",
                parameters={},
            )
            | changes
        )
    )


def test_rule_is_abstract_frozen_and_uses_only_the_shared_scope():
    with pytest.raises(TypeError):
        Rule("id", "name", RuleScope.EDGE, True, "v1", {})
    parameters = {"limit": D("1")}
    value = rule(parameters=parameters)
    parameters["limit"] = D("2")
    assert value.parameters["limit"] == D("1")
    assert value.rule_type == "DeclaredOnlyRule"
    assert value.metric_keys() == ()
    assert [item.name for item in fields(value)] == [
        "rule_id",
        "name",
        "scope",
        "enabled",
        "version",
        "parameters",
    ]
    with pytest.raises(FrozenInstanceError):
        value.enabled = False
    with pytest.raises(TypeError):
        value.parameters["limit"] = D("2")
    with pytest.raises((TypeError, AttributeError)):
        value.context = context()


@pytest.mark.parametrize(
    "change",
    [
        {"rule_id": ""},
        {"name": " "},
        {"version": ""},
        {"enabled": 1},
        {"scope": "edge"},
        {"scope": RuleScope.CHAIN},
        {"parameters": []},
        {"parameters": {"float": 1.2}},
        {"parameters": {"bad": D("NaN")}},
    ],
)
def test_rule_metadata_rejects_invalid_values(change):
    with pytest.raises(ValueError):
        rule(**change)


def test_recursive_parameters_detach_shared_inputs_and_preserve_order_and_empty_values():
    shared = {"grades": ["FD", "FC", "FD"], "limits": [D("0.60"), None]}
    parameters = {"first": shared, "second": shared, "empty": {"items": [], "text": ""}}
    value = rule(parameters=parameters)
    expected = fingerprint(value)
    shared["grades"].append("changed")
    shared["limits"].clear()
    parameters.clear()
    assert value.parameters["first"] == value.parameters["second"]
    assert value.parameters["first"]["grades"] == ("FD", "FC", "FD")
    assert value.parameters["first"]["limits"] == (D("0.60"), None)
    assert value.parameters["empty"] == {"items": (), "text": ""}
    assert fingerprint(value) == expected
    with pytest.raises(TypeError):
        value.parameters["first"]["limits"] = ()
    with pytest.raises(TypeError):
        value.parameters["first"]["grades"][0] = "changed"
    assert freeze_rule_parameters(value.parameters) == value.parameters


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "invalid",
    [1.0, D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"), {1}, frozenset(), b"x", object()],
)
def test_rule_rejects_nested_nonparameter_values_even_when_disabled(enabled, invalid):
    with pytest.raises(ValueError, match=r"parameters\['group'\]\[0\]"):
        rule(enabled=enabled, parameters={"group": [invalid]})


@pytest.mark.parametrize("key", [None, 1, "", " "])
def test_rule_rejects_invalid_nested_mapping_keys(key):
    with pytest.raises(ValueError, match=r"parameters\['group'\]\[0\] key"):
        rule(parameters={"group": [{key: "value"}]})


@pytest.mark.parametrize("kind", ["mapping", "list", "tuple"])
def test_rule_rejects_cycles_but_not_shared_acyclic_containers(kind):
    cycle = {} if kind == "mapping" else []
    if kind == "mapping":
        cycle["self"] = cycle
    else:
        cycle.append(cycle if kind == "list" else (cycle,))
    with pytest.raises(ValueError, match="parameters.*cycle.*circular"):
        rule(parameters={"cycle": cycle})


@pytest.mark.parametrize("invalid", [None, [], (), "{}", 1])
def test_rule_parameters_root_requires_mapping(invalid):
    with pytest.raises(ValueError, match="parameters must be a mapping"):
        freeze_rule_parameters(invalid)


def test_rule_parameters_preserve_literal_pre_fix_flat_encoding_and_fingerprint():
    # Captured from 6fb7a2b before changing either constructor or the freezer.
    flat = {"text": "FC", "count": 5, "limit": D("0.60"), "enabled": True, "unset": None}
    frozen = freeze_rule_parameters(flat)
    assert canonical_json(frozen) == (
        '["object",[["count",5],["enabled",true],["limit",["decimal",0,"6",-1]],'
        '["text","FC"],["unset",null]]]'
    )
    assert fingerprint(frozen) == "d9b12deea111cffe9159054b41740a21eb203dfbbe39ab4be7eb6d509a33d6fd"
    assert canonical_json(frozen) == canonical_json(freeze_scalars(flat))
    assert len({fingerprint(freeze_rule_parameters({"x": x})) for x in (True, 1, "1", D(1))}) == 4
    for nested in ([], (), {}):
        with pytest.raises(ValueError):
            freeze_scalars({"attribute": nested})
        with pytest.raises(ValueError):
            node(rule_attributes={"attribute": nested})


def test_undeclared_entry_points_fail_closed_instead_of_passing():
    value = rule()
    with pytest.raises(UnsupportedRuleSubjectError):
        value.evaluate(EdgeRuleSubject("edge", node(), node(node_id="other")), context())
    with pytest.raises(UnsupportedRuleSubjectError):
        value.construction_priority(node())
    with pytest.raises(UnsupportedRuleSubjectError):
        value.evaluate_controlled_split(subject(), context())


def test_context_preserves_task_order_and_freezes_copied_inputs():
    periods = ["p-z", "p-a"]
    index = {"p-a": 1, "p-z": 0}
    prototypes = ["virtual"]
    value = RuleEvaluationContext(periods, index, prototypes)
    periods.reverse()
    index.clear()
    prototypes.clear()
    assert value.period_order == ("p-z", "p-a")
    assert dict(value.period_index) == {"p-z": 0, "p-a": 1}
    assert value.virtual_prototype_ids == ("virtual",)
    with pytest.raises(TypeError):
        value.period_index["p-z"] = 1
    with pytest.raises(FrozenInstanceError):
        value.period_order = ("p-a", "p-z")
    assert context().virtual_prototype_ids == ()


@pytest.mark.parametrize(
    "periods,index,prototypes",
    [
        ((), {}, ()),
        (("p", "p"), {"p": 0}, ()),
        (("p",), {"p": 1}, ()),
        (("p",), {"p": False}, ()),
        (("p",), {"p": 0.0}, ()),
        (("p",), {"p": 0, "extra": 1}, ()),
        (("p",), {}, ()),
        (("p",), (), ()),
        ((" ",), {" ": 0}, ()),
        (("p",), {"p": 0}, ("v", "v")),
        (("p",), {"p": 0}, ("",)),
        ({"p"}, {"p": 0}, ()),
    ],
)
def test_context_rejects_invalid_catalogs_or_indexes(periods, index, prototypes):
    with pytest.raises(ValueError):
        RuleEvaluationContext(periods, index, prototypes)


def test_subjects_hold_typed_immutable_structure_without_task_state():
    current = node()
    chain = Chain("chain", (current,), "p-z")
    plan = SchedulePlan((chain,))
    subjects = (
        NodeRuleSubject("node", current),
        EdgeRuleSubject("edge", current, node(node_id="other")),
        ChainRuleSubject("chain", chain),
        PlanRuleSubject("plan", plan, None),
        subject(),
    )
    for value in subjects:
        with pytest.raises(FrozenInstanceError):
            value.subject_id = "changed"
        with pytest.raises(ValueError):
            replace(value, subject_id=" ")
    assert PlanRuleSubject.__annotations__["resource_view"] == "EvaluationResourceView"
    assert not hasattr(subjects[0], "context")


@pytest.mark.parametrize(
    "factory,change",
    [
        (lambda: NodeRuleSubject("node", node()), {"node": object()}),
        (lambda: EdgeRuleSubject("edge", node(), node()), {"left": object()}),
        (lambda: EdgeRuleSubject("edge", node(), node()), {"right": object()}),
        (lambda: ChainRuleSubject("chain", Chain("chain", (node(),), "p-z")), {"chain": ()}),
        (
            lambda: PlanRuleSubject("plan", SchedulePlan((Chain("c", (node(),), "p-z"),)), None),
            {"plan": ()},
        ),
        (subject, {"parent_node": object()}),
        (subject, {"source_period": ""}),
        (subject, {"origin_assigned_period": ""}),
        (subject, {"accepted_split_source_count": -1}),
        (subject, {"accepted_split_source_count": True}),
    ],
)
def test_subjects_reject_malformed_structure(factory, change):
    with pytest.raises(ValueError):
        replace(factory(), **change)


def test_contributions_preserve_per_violation_disposition_and_freeze_sequences():
    below = RuleViolation(
        "weight",
        RuleScope.CHAIN,
        "c",
        "below",
        "Below minimum",
        RuleDisposition.ALLOWED_FINAL_DEVIATION,
        D("1"),
    )
    above = replace(below, reason_code="above", disposition=RuleDisposition.PROHIBITED)
    metrics = [MetricContribution("signed", D("-1.25")), MetricContribution("count", 1)]
    violations = [below, above]
    result = RuleContribution(violations, metrics)
    violations.clear()
    metrics.clear()
    assert result.violations == (below, above)
    assert len(result.metrics) == 2
    with pytest.raises(FrozenInstanceError):
        result.metrics = ()
    with pytest.raises(ValueError):
        replace(below, severity=D("-1"))
    with pytest.raises(ValueError):
        replace(below, disposition="prohibited")
    with pytest.raises(ValueError):
        RuleContribution((object(),), ())
    with pytest.raises(ValueError):
        RuleContribution((), (object(),))


@pytest.mark.parametrize("value", [True, 1.0, "1", None, D("NaN"), D("Infinity")])
def test_metrics_accept_only_finite_decimal_or_exact_integer(value):
    with pytest.raises(ValueError):
        MetricContribution("metric", value)


def test_quality_criterion_requires_closed_enum_values():
    value = QualityCriterion(
        "first",
        "count",
        QualityDirection.MINIMIZE,
        QualityAggregation.NAMED_VALUE,
        NumericProjection.EXACT_DECIMAL,
    )
    for name, invalid in (
        ("criterion_id", ""),
        ("metric_key", ""),
        ("direction", "minimize"),
        ("aggregation", "named_value"),
        ("numeric_projection", "exact_decimal"),
    ):
        with pytest.raises(ValueError):
            replace(value, **{name: invalid})


def test_underweight_gap_projection_is_a_frozen_typed_declaration():
    projection = NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM
    assert projection.value == "underweight_gap_round_2_then_sum"
    value = QualityCriterion(
        "gap",
        "underweight_total_gap",
        QualityDirection.MINIMIZE,
        QualityAggregation.SUM,
        projection,
    )
    assert value.numeric_projection is projection
    with pytest.raises(FrozenInstanceError):
        value.numeric_projection = NumericProjection.EXACT_DECIMAL
    with pytest.raises(ValueError):
        replace(value, numeric_projection=projection.value)
    assert (
        len(
            {fingerprint(replace(value, numeric_projection=option)) for option in NumericProjection}
        )
        == 3
    )


@pytest.mark.parametrize(
    "change",
    [
        {"metric_key": "chain_count"},
        {"metric_key": "underweight_chain_count"},
        {"metric_key": "overweight_total_excess"},
        {"aggregation": QualityAggregation.NAMED_VALUE},
        {"aggregation": QualityAggregation.COUNT},
        {"aggregation": QualityAggregation.MAXIMUM},
        {"direction": QualityDirection.MAXIMIZE},
    ],
)
def test_underweight_gap_projection_rejects_every_incompatible_dimension(change):
    value = QualityCriterion(
        "gap",
        "underweight_total_gap",
        QualityDirection.MINIMIZE,
        QualityAggregation.SUM,
        NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM,
    )
    with pytest.raises(ValueError):
        replace(value, **change)


@pytest.mark.parametrize(
    "projection,expected_fingerprint",
    [
        (
            NumericProjection.EXACT_DECIMAL,
            "5a450006c3db047b7d0d4b07c0e53dfc9e0c02a69ee5727715877426862ffca0",
        ),
        (
            NumericProjection.REFERENCE_FLOAT_ROUND_6,
            "6931d8e1a28a101f5de3421900d217fe859769e2969f4d170ff5c995e7deb2d8",
        ),
    ],
)
def test_old_quality_projections_keep_literal_encoding_and_unrestricted_combinations(
    projection, expected_fingerprint
):
    # Captured from the unmodified 220f0937 archive before adding the new projection.
    value = QualityCriterion(
        "gap",
        "underweight_total_gap",
        QualityDirection.MINIMIZE,
        QualityAggregation.SUM,
        projection,
    )
    assert canonical_json(value) == (
        '["object",[["aggregation","sum"],["criterion_id","gap"],'
        '["direction","minimize"],["metric_key","underweight_total_gap"],'
        f'["numeric_projection","{projection.value}"]]]'
    )
    assert fingerprint(value) == expected_fingerprint
    for aggregation in QualityAggregation:
        for direction in QualityDirection:
            other = replace(
                value, metric_key="chain_count", aggregation=aggregation, direction=direction
            )
            assert other.numeric_projection is projection


def test_split_decision_fingerprint_binds_all_inputs_and_task_context():
    result = decision()
    returned = {
        part.name: getattr(result, part.name)
        for part in fields(result)
        if part.name != "decision_fingerprint"
    }
    assert result.decision_fingerprint == fingerprint(
        dict(subject=subject(), context=context(), decision=returned)
    )
    equal_context = RuleEvaluationContext(["p-z", "p-a"], {"p-a": 1, "p-z": 0}, [])
    assert result == create_controlled_split_decision(subject(), equal_context, **returned)
    changed_context = RuleEvaluationContext(("p-a", "p-z"), {"p-a": 0, "p-z": 1}, ())
    assert (
        result.decision_fingerprint
        != create_controlled_split_decision(
            subject(), changed_context, **returned
        ).decision_fingerprint
    )
    for changed_subject in (
        replace(subject(), accepted_split_source_count=1),
        replace(subject(), parent_node=node(weight=D("13"))),
        replace(subject(), origin_assigned_period="p-a"),
    ):
        assert (
            result.decision_fingerprint
            != create_controlled_split_decision(
                changed_subject, context(), **returned
            ).decision_fingerprint
        )
    assert result.decision_fingerprint != decision(rule_version="v2").decision_fingerprint
    assert result.decision_fingerprint != decision(maximum_piece_weight=D("9")).decision_fingerprint
    assert (
        result.decision_fingerprint
        != create_controlled_split_decision(
            subject(), replace(context(), virtual_prototype_ids=("v",)), **returned
        ).decision_fingerprint
    )


@pytest.mark.parametrize(
    "change",
    [
        {"eligible": 1},
        {"rule_id": ""},
        {"rule_version": ""},
        {"reason_code": ""},
        {"mode": None},
        {"mode": "same_period_split"},
        {"target_assigned_period": None},
        {"maximum_piece_weight": D(0)},
        {"minimum_piece_weight": D(0)},
        {"minimum_piece_weight": D("11")},
        {"maximum_piece_weight": D("NaN")},
        {"maximum_accepted_source_count": -1},
        {"maximum_separator_node_count": -1},
        {"maximum_separator_node_count": True},
        {"maximum_separator_weight": D("-1")},
        {"decision_fingerprint": ""},
    ],
)
def test_eligible_decision_rejects_invalid_parameters(change):
    with pytest.raises(ValueError):
        replace(decision(), **change)


def test_rejected_decision_is_canonical_and_carries_no_authorization():
    result = create_controlled_split_decision(
        subject(),
        context(),
        eligible=False,
        rule_id="split",
        rule_version="v1",
        reason_code="disabled",
    )
    assert isinstance(result, ControlledSplitDecision)
    assert result.mode is None
    assert result.target_assigned_period is None
    assert result.maximum_piece_weight is None
    assert result.minimum_piece_weight is None
    assert result.maximum_separator_weight == 0
    for change in (
        {"mode": ControlledSplitMode.SAME_PERIOD_SPLIT},
        {"target_assigned_period": "p-z"},
        {"maximum_piece_weight": D("10")},
        {"minimum_piece_weight": D("1")},
        {"maximum_accepted_source_count": 1},
        {"maximum_separator_node_count": 1},
        {"maximum_separator_weight": D("1")},
        {"reason_code": ""},
    ):
        with pytest.raises(ValueError):
            replace(result, **change)
    with pytest.raises(ValueError):
        create_controlled_split_decision(
            object(),
            context(),
            eligible=False,
            rule_id="split",
            rule_version="v1",
            reason_code="disabled",
        )
    with pytest.raises(ValueError):
        create_controlled_split_decision(
            subject(),
            object(),
            eligible=False,
            rule_id="split",
            rule_version="v1",
            reason_code="disabled",
        )
