"""Chain weight bounds, per-reason disposition and diagnostic target metrics."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

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
from apsgo_scheduler.core.rules.concrete import ChainWeightRangeRule

D = Decimal
METRIC_KEYS = (
    "underweight_chain_count",
    "underweight_total_gap",
    "overweight_chain_count",
    "overweight_total_excess",
    "chain_target_weight_deviation",
)


def parameters(**changes):
    return {"min_weight": D("700"), "max_weight": D("2000"), "target_weight": D("2000")} | changes


def rule(**changes):
    return ChainWeightRangeRule(
        **(
            dict(
                rule_id="chain_weight_range",
                name="链重范围",
                scope=RuleScope.CHAIN,
                enabled=True,
                version="1",
                parameters=parameters(),
            )
            | changes
        )
    )


def node(index, weight, role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D(weight),
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


def subject(nodes):
    return ChainRuleSubject("supplied-chain-subject", Chain("chain", tuple(nodes), "period"))


def context():
    return RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def loaded_rule_set(current):
    definition = RuleDefinitionSpec(
        current.rule_id,
        current.rule_type,
        current.name,
        current.scope,
        current.enabled,
        current.version,
        current.parameters,
    )
    spec = RuleSetSpec(
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
        frozenset({"chain_weight_below_minimum"}),
        "pending",
    )
    return load_rule_set(replace(spec, fingerprint=fingerprint_rule_set_spec(spec)))


@pytest.mark.parametrize(
    "weight,under,gap,over,excess,target_delta",
    [
        ("699", 1, "1", 0, "0", "1301"),
        ("699.9999989", 1, "0.0000011", 0, "0", "1300.0000011"),
        ("699.999999", 0, "0", 0, "0", "1300.000001"),
        ("699.9999991", 0, "0", 0, "0", "1300.0000009"),
        ("700", 0, "0", 0, "0", "1300"),
        ("700.0000011", 0, "0", 0, "0", "1299.9999989"),
        ("1500", 0, "0", 0, "0", "500"),
        ("1999.999999", 0, "0", 0, "0", "0.000001"),
        ("2000", 0, "0", 0, "0", "0"),
        ("2000.0000009", 0, "0", 0, "0", "0.0000009"),
        ("2000.000001", 0, "0", 0, "0", "0.000001"),
        ("2000.0000011", 0, "0", 1, "0.0000011", "0.0000011"),
        ("2001", 0, "0", 1, "1", "1"),
    ],
)
def test_frozen_range_boundaries_have_ordered_typed_metrics_and_per_reason_disposition(
    weight, under, gap, over, excess, target_delta
):
    current = rule()
    value = current.evaluate(subject((node(0, weight),)), context())
    assert tuple(metric.metric_key for metric in value.metrics) == METRIC_KEYS
    assert tuple(metric.value for metric in value.metrics) == (
        under,
        D(gap),
        over,
        D(excess),
        D(target_delta),
    )
    assert tuple(type(metric.value) for metric in value.metrics) == (
        int,
        Decimal,
        int,
        Decimal,
        Decimal,
    )
    assert len(value.violations) == under + over
    if not value.violations:
        return
    violation = value.violations[0]
    assert violation.rule_id == current.rule_id
    assert violation.scope is RuleScope.CHAIN
    assert violation.subject_id == "supplied-chain-subject"
    assert violation.reason_code == (
        "chain_weight_below_minimum" if under else "chain_weight_above_maximum"
    )
    assert violation.disposition is (
        RuleDisposition.ALLOWED_FINAL_DEVIATION if under else RuleDisposition.PROHIBITED
    )
    assert violation.severity == D(gap if under else excess)
    assert isinstance(violation.severity, Decimal)
    assert violation.message


@pytest.mark.parametrize(
    "target,deviation", [("0", "1000"), ("100", "900"), ("1000", "0"), ("5000", "4000")]
)
def test_target_outside_bounds_is_valid_and_remains_only_an_explicit_metric(target, deviation):
    current = rule(parameters=parameters(target_weight=D(target)))
    loaded = loaded_rule_set(current)
    value = loaded.evaluate_chain(subject((node(0, "1000"),)), context())
    assert value.violations == ()
    assert value.metrics[-1].value == D(deviation)
    assert tuple(item.metric_key for item in loaded.quality_spec) == (
        "prohibited_violation_count",
        "prohibited_violation_severity",
    )


def test_equal_minimum_and_maximum_are_valid_and_only_strict_epsilon_crossings_violate():
    current = rule(
        parameters=parameters(min_weight=D("100"), max_weight=D("100"), target_weight=D(0))
    )
    for weight, reason in (
        ("100", None),
        ("99.999999", None),
        ("100.000001", None),
        ("99.9999989", "chain_weight_below_minimum"),
        ("100.0000011", "chain_weight_above_maximum"),
    ):
        value = current.evaluate(subject((node(0, weight),)), context())
        assert [item.reason_code for item in value.violations] == (
            [] if reason is None else [reason]
        )


def test_zero_minimum_is_valid_without_a_hidden_minimum_floor():
    value = rule(parameters=parameters(min_weight=D(0))).evaluate(
        subject((node(0, "0.1"),)), context()
    )
    assert value.violations == ()
    assert value.metrics[0].value == 0
    assert value.metrics[1].value == D(0)


def test_all_material_roles_count_towards_total_chain_weight():
    current = rule()
    nodes = (
        node(0, "500"),
        node(1, "100", MaterialRole.ACTUAL_TRANSITION),
        node(2, "100", MaterialRole.GENERATED_VIRTUAL),
    )
    exactly_minimum = current.evaluate(subject(nodes), context())
    assert exactly_minimum.violations == ()
    assert exactly_minimum.metrics[-1].value == D("1300")
    overweight = current.evaluate(
        subject(nodes[:-1] + (replace(nodes[-1], weight=D("1500")),)), context()
    )
    assert [item.reason_code for item in overweight.violations] == ["chain_weight_above_maximum"]
    assert overweight.violations[0].severity == D("100")


def test_same_source_split_fragments_add_piece_weights_not_parent_weight_or_unique_source():
    pieces = []
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
            parent_weight=D("800"),
            piece_index=index,
            piece_count=2,
            authorization_rule_id="split-rule",
            authorization_rule_version="1",
            authorization_decision_fingerprint="decision",
            reason_code="authorized",
        )
        pieces.append(
            replace(
                node(index, "400"),
                source_order_id="source",
                source_resource_id="resource",
                split_lineage=lineage,
            )
        )
    value = rule().evaluate(subject(pieces), context())
    assert value.violations == ()
    assert value.metrics[-1].value == D("1200")


@pytest.mark.parametrize("missing", ["min_weight", "max_weight", "target_weight"])
def test_enabled_parameters_have_no_hidden_defaults(missing):
    configuration = parameters()
    configuration.pop(missing)
    with pytest.raises(ValueError):
        rule(parameters=configuration)


@pytest.mark.parametrize(
    "changes",
    [
        {"min_weight": D("-1")},
        {"min_weight": D("2001")},
        {"min_weight": 700},
        {"min_weight": True},
        {"min_weight": None},
        {"min_weight": D("NaN")},
        {"max_weight": D(0)},
        {"max_weight": D("-1")},
        {"max_weight": D("Infinity")},
        {"max_weight": 2000},
        {"max_weight": None},
        {"target_weight": D("-1")},
        {"target_weight": D("NaN")},
        {"target_weight": D("Infinity")},
        {"target_weight": 2000},
        {"target_weight": True},
        {"target_weight": None},
    ],
)
def test_invalid_range_and_parameter_types_fail_at_construction(changes):
    with pytest.raises(ValueError):
        rule(parameters=parameters(**changes))


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("300.0000004", "399.99999832346", (1, "0.00000127654", 0, "0", "1300.00000127654")),
        ("300.0000004", "399.9999987", (0, "0", 0, "0", "1300.0000009")),
        ("1200.0000004", "800.00000987654", (0, "0", 1, "0.00001027654", "0.00001027654")),
        ("1000.0000004", "234.56788972345", (0, "0", 0, "0", "765.43210987655")),
    ],
)
def test_total_bound_gaps_and_absolute_target_deviation_ignore_ambient_decimal_precision(
    first, second, expected
):
    current = rule()
    current_subject = subject((node(0, first), node(1, second)))
    with localcontext() as arithmetic:
        arithmetic.prec = 2
        value = current.evaluate(current_subject, context())
        assert arithmetic.prec == 2
    under, gap, over, excess, deviation = expected
    assert tuple(item.value for item in value.metrics) == (
        under,
        D(gap),
        over,
        D(excess),
        D(deviation),
    )
    assert [item.severity for item in value.violations] == (
        [D(gap)] if under else [D(excess)] if over else []
    )


def test_allowed_final_deviation_is_a_reason_not_permission_for_the_entire_rule():
    current = rule()
    loaded = loaded_rule_set(current)
    under = loaded.evaluate_chain(subject((node(0, "600"),)), context()).violations[0]
    over = loaded.evaluate_chain(subject((node(0, "2100"),)), context()).violations[0]
    assert under.rule_id == over.rule_id == current.rule_id
    assert under.reason_code in loaded.allowed_final_deviation_codes
    assert under.disposition is RuleDisposition.ALLOWED_FINAL_DEVIATION
    assert over.reason_code not in loaded.allowed_final_deviation_codes
    assert over.disposition is RuleDisposition.PROHIBITED


def test_registered_rule_has_no_extra_required_fields_and_is_pure():
    configuration = parameters()
    current = rule(parameters=configuration)
    assert ChainWeightRangeRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["ChainWeightRangeRule"] is ChainWeightRangeRule
    assert current.required_fields() == ()
    assert current.metric_keys() == METRIC_KEYS
    current_subject = subject((node(0, "900"),))
    current_context = context()
    before = fingerprint((current, current_subject, current_context))
    configuration["target_weight"] = D("9999")
    first = current.evaluate(current_subject, current_context)
    loaded = loaded_rule_set(current)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == (current,)
    assert loaded.evaluate_chain(current_subject, current_context) == first
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["target_weight"] = D("9999")


def test_disabled_rule_needs_no_business_parameters_and_produces_no_hidden_metrics():
    disabled = rule(enabled=False, parameters={})
    assert disabled.required_fields() == ()
    assert disabled.metric_keys() == ()
    current_subject = subject((node(0, "1"),))
    assert disabled.evaluate(current_subject, context()) == RuleContribution((), ())
    loaded = loaded_rule_set(disabled)
    assert loaded.rules == ()
    assert loaded.evaluate_chain(current_subject, context()) == RuleContribution((), ())


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_is_rejected_before_enabled_dispatch(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0, "1000")), context())


def test_loader_rejects_missing_parameters_before_search():
    definition = RuleDefinitionSpec(
        "weight", "ChainWeightRangeRule", "链重规则", RuleScope.CHAIN, True, "1", {}
    )
    spec = RuleSetSpec("LINE", "PROCESS", "month", "1", (definition,), (), frozenset(), "pending")
    with pytest.raises(RuleSetLoadError) as failure:
        load_rule_set(replace(spec, fingerprint=fingerprint_rule_set_spec(spec)))
    assert [issue.code for issue in failure.value.issues] == ["invalid_rule_parameters"]
