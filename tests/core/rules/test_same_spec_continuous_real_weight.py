"""Same-specification contiguous real-weight goldens and configured grouping."""

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
from apsgo_scheduler.core.rules.concrete import SameSpecContinuousRealWeightRule

D = Decimal


def parameters(**changes):
    return {
        "group_by_fields": ["thickness", "width", "grade"],
        "max_real_weight": D("1000"),
    } | changes


def rule(**changes):
    return SameSpecContinuousRealWeightRule(
        **(
            dict(
                rule_id="chain_same_spec_real_weight_lte",
                name="同规格连续真实重量",
                scope=RuleScope.CHAIN,
                enabled=True,
                version="1",
                parameters=parameters(),
            )
            | changes
        )
    )


def node(
    index,
    weight="500",
    grade="SPCC",
    width=D("1200"),
    thickness=D("1"),
    role=MaterialRole.NORMAL_REAL,
):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D(weight),
        width=width,
        thickness=thickness,
        min_temperature=None,
        max_temperature=None,
        grade=grade,
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


@pytest.mark.parametrize(
    "second,total,severity",
    [
        ("499", "999", None),
        ("500", "1000", None),
        ("500.0000009", "1000.0000009", None),
        ("500.000001", "1000.000001", None),
        ("500.0000011", "1000.0000011", "0.0000011"),
        ("600", "1100", "100"),
    ],
)
def test_reference_weight_limit_epsilon_and_exact_severity(second, total, severity):
    current = rule()
    value = current.evaluate(subject((node(0), node(1, second))), context())
    assert [(item.metric_key, item.value) for item in value.metrics] == [
        ("max_same_spec_real_run_weight", D(total))
    ]
    assert isinstance(value.metrics[0].value, Decimal)
    if severity is None:
        assert value.violations == ()
        return
    assert len(value.violations) == 1
    violation = value.violations[0]
    assert violation.rule_id == current.rule_id
    assert violation.scope is RuleScope.CHAIN
    assert violation.subject_id == f"chain-subject:{current.rule_id}:0-1"
    assert violation.reason_code == "same_spec_run_weight"
    assert violation.disposition is RuleDisposition.PROHIBITED
    assert violation.severity == D(severity)
    assert isinstance(violation.severity, Decimal)
    assert violation.message


@pytest.mark.parametrize(
    "changes", [{"width": D("1300")}, {"thickness": D("1.1")}, {"grade": "OTHER"}]
)
def test_each_selected_specification_field_breaks_the_run_when_different(changes):
    value = rule().evaluate(
        subject((node(0, "600"), replace(node(1, "600"), **changes))), context()
    )
    assert value.violations == ()
    assert value.metrics[0].value == D("600")


@pytest.mark.parametrize(
    "fields,changes",
    [
        (["thickness"], {"width": D("1300"), "grade": "OTHER"}),
        (["width"], {"thickness": D("1.1"), "grade": "OTHER"}),
        (["grade"], {"thickness": D("1.1"), "width": D("1300")}),
        (["width", "grade"], {"thickness": D("1.1")}),
    ],
)
def test_selected_subset_changes_grouping_instead_of_using_a_fixed_specification(fields, changes):
    current_subject = subject((node(0, "600"), replace(node(1, "600"), **changes)))
    default_result = rule().evaluate(current_subject, context())
    selected = rule(parameters=parameters(group_by_fields=fields)).evaluate(
        current_subject, context()
    )
    assert default_result.metrics[0].value == D("600")
    assert default_result.violations == ()
    assert selected.metrics[0].value == D("1200")
    assert [item.severity for item in selected.violations] == [D("200")]


def test_field_order_stays_in_identity_but_reordering_the_same_fields_preserves_equality():
    first = rule()
    reordered = rule(parameters=parameters(group_by_fields=["grade", "width", "thickness"]))
    assert first.parameters["group_by_fields"] == ("thickness", "width", "grade")
    assert reordered.parameters["group_by_fields"] == ("grade", "width", "thickness")
    assert fingerprint(first) != fingerprint(reordered)
    current_subject = subject((node(0, "600"), node(1, "600"), node(2, grade="OTHER")))
    assert first.evaluate(current_subject, context()) == reordered.evaluate(
        current_subject, context()
    )


def test_grade_is_normalized_before_comparing_contiguous_specs():
    value = rule().evaluate(
        subject((node(0, "600", grade="spcc"), node(1, "600", grade=" SPCC "))), context()
    )
    assert value.metrics[0].value == D("1200")
    assert [item.severity for item in value.violations] == [D("200")]


@pytest.mark.parametrize(
    "field,value,maximum",
    [
        ("width", D("1200.00000000000001"), D("1200")),
        ("width", D("1200.0000000001"), D("600")),
        ("thickness", D("1.00000000000000001"), D("1200")),
        ("thickness", D("1.0000000001"), D("600")),
    ],
)
def test_physical_spec_equality_uses_float_projection_without_connection_epsilon(
    field, value, maximum
):
    current = rule()
    result = current.evaluate(
        subject((node(0, "600"), replace(node(1, "600"), **{field: value}))), context()
    )
    assert result.metrics[0].value == maximum
    assert len(result.violations) == int(maximum > D("1000"))


@pytest.mark.parametrize("field", ["width", "thickness"])
def test_missing_physical_fields_are_preserved_as_group_key_values(field):
    current = rule()
    first, second = (replace(node(index, "600"), **{field: None}) for index in (0, 1))
    together = current.evaluate(subject((first, second)), context())
    assert together.metrics[0].value == D("1200")
    assert len(together.violations) == 1
    different = current.evaluate(subject((first, node(1, "600"))), context())
    assert different.metrics[0].value == D("600")
    assert different.violations == ()


def test_single_none_group_value_is_not_confused_with_an_excluded_material():
    current = rule(parameters=parameters(group_by_fields=["width"]))
    nodes = (
        node(0, "600", width=None),
        node(1, "600", width=None),
        node(2, "2000", width=None, grade="", role=MaterialRole.ACTUAL_TRANSITION),
        node(3, "800", width=None),
    )
    value = current.evaluate(subject(nodes), context())
    assert value.metrics[0].value == D("1200")
    assert [(item.subject_id, item.severity) for item in value.violations] == [
        (f"chain-subject:{current.rule_id}:0-1", D("200"))
    ]


def test_multiple_same_specs_separated_in_chain_do_not_merge_and_tail_is_closed():
    nodes = (
        node(0, "600"),
        node(1, "401"),
        node(2, "800", grade="OTHER"),
        node(3, "600"),
        node(4, "402"),
    )
    current = rule()
    value = current.evaluate(subject(nodes), context())
    assert [(item.subject_id, item.severity) for item in value.violations] == [
        (f"chain-subject:{current.rule_id}:0-1", D(1)),
        (f"chain-subject:{current.rule_id}:3-4", D(2)),
    ]
    assert value.metrics[0].value == D("1002")
    reversed_value = current.evaluate(subject(reversed(nodes)), context())
    assert [item.severity for item in reversed_value.violations] == [D(2), D(1)]


@pytest.mark.parametrize("role", [MaterialRole.ACTUAL_TRANSITION, MaterialRole.GENERATED_VIRTUAL])
@pytest.mark.parametrize("grade", ["SPCC", ""])
def test_transition_and_virtual_nodes_break_without_contributing_or_reading_grade(role, grade):
    nodes = (node(0, "700"), node(1, "2000", grade=grade, role=role), node(2, "800"))
    value = rule().evaluate(subject(nodes), context())
    assert value.violations == ()
    assert value.metrics[0].value == D("800")


def test_chain_without_normal_real_material_has_zero_metric():
    nodes = (
        node(0, "1200", grade="", role=MaterialRole.ACTUAL_TRANSITION),
        node(1, "1200", grade="", role=MaterialRole.GENERATED_VIRTUAL),
    )
    value = rule().evaluate(subject(nodes), context())
    assert value.violations == ()
    assert value.metrics[0].value == D(0)


def test_same_source_split_fragments_each_contribute_their_own_weight():
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
            parent_weight=D("1200"),
            piece_index=index,
            piece_count=2,
            authorization_rule_id="split-rule",
            authorization_rule_version="1",
            authorization_decision_fingerprint="decision",
            reason_code="authorized",
        )
        pieces.append(
            replace(
                node(index, "600"),
                source_order_id="source",
                source_resource_id="resource",
                split_lineage=lineage,
            )
        )
    value = rule().evaluate(subject(pieces), context())
    assert value.metrics[0].value == D("1200")
    assert [item.severity for item in value.violations] == [D("200")]


@pytest.mark.parametrize("missing", ["group_by_fields", "max_real_weight"])
def test_parameters_have_no_hidden_defaults(missing):
    configuration = parameters()
    configuration.pop(missing)
    with pytest.raises(ValueError):
        rule(parameters=configuration)


@pytest.mark.parametrize(
    "changes",
    [
        {"group_by_fields": "thickness,width,grade"},
        {"group_by_fields": []},
        {"group_by_fields": [""]},
        {"group_by_fields": [" "]},
        {"group_by_fields": [1]},
        {"group_by_fields": ["width", "width"]},
        {"group_by_fields": ["node.width"]},
        {"group_by_fields": ["surface_grade"]},
        {"group_by_fields": ["weight"]},
        {"max_real_weight": D("-1")},
        {"max_real_weight": D("NaN")},
        {"max_real_weight": D("Infinity")},
        {"max_real_weight": 1000},
        {"max_real_weight": True},
        {"max_real_weight": None},
    ],
)
def test_enabled_rule_rejects_invalid_parameter_shapes(changes):
    with pytest.raises(ValueError):
        rule(parameters=parameters(**changes))


@pytest.mark.parametrize("grade", ["", " "])
def test_normal_real_empty_grade_fails_closed(grade):
    with pytest.raises(ValueError, match="grade"):
        rule().evaluate(subject((node(0, grade=grade),)), context())


def test_required_grade_is_not_hidden_by_a_grouping_subset():
    width_only = rule(parameters=parameters(group_by_fields=["width"]))
    assert width_only.required_fields() == ("grade",)
    with pytest.raises(ValueError, match="grade"):
        width_only.evaluate(subject((node(0, grade=""),)), context())


def test_configured_zero_weight_limit_retains_only_the_explicit_epsilon():
    current = rule(parameters=parameters(max_real_weight=D(0)))
    for weight, prohibited in (("0.000001", 0), ("0.0000011", 1)):
        value = current.evaluate(subject((node(0, weight),)), context())
        assert value.metrics[0].value == D(weight)
        assert len(value.violations) == prohibited


@pytest.mark.parametrize(
    "second,total,severity",
    [
        ("400.0000004", "1000.0000008", None),
        ("400.0000006", "1000.000001", None),
        ("400.0000008", "1000.0000012", "0.0000012"),
        ("400.00000987654", "1000.00001027654", "0.00001027654"),
    ],
)
def test_decimal_sum_epsilon_and_severity_are_independent_of_ambient_precision(
    second, total, severity
):
    current = rule()
    current_subject = subject((node(0, "600.0000004"), node(1, second)))
    with localcontext() as arithmetic:
        arithmetic.prec = 2
        value = current.evaluate(current_subject, context())
        assert arithmetic.prec == 2
    assert value.metrics[0].value == D(total)
    assert [item.severity for item in value.violations] == (
        [] if severity is None else [D(severity)]
    )


def test_registered_rule_is_pure_and_configurations_are_detached():
    configuration = parameters()
    current = rule(parameters=configuration)
    assert SameSpecContinuousRealWeightRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["SameSpecContinuousRealWeightRule"] is SameSpecContinuousRealWeightRule
    assert current.required_fields() == ("grade",)
    assert current.metric_keys() == ("max_same_spec_real_run_weight",)
    current_subject = subject((node(0, "600"), node(1, "600")))
    current_context = context()
    before = fingerprint((current, current_subject, current_context))
    configuration["group_by_fields"].clear()
    configuration["max_real_weight"] = D("9999")
    first = current.evaluate(current_subject, current_context)
    loaded = loaded_rule_set(current)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == (current,)
    assert loaded.evaluate_chain(current_subject, current_context) == first
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["group_by_fields"][0] = "other"


def test_disabled_rule_needs_no_business_parameters_or_inputs_and_contributes_nothing():
    disabled = rule(enabled=False, parameters={})
    malformed_for_enabled = subject((node(0, grade=""),))
    assert disabled.required_fields() == ()
    assert disabled.metric_keys() == ()
    assert disabled.evaluate(malformed_for_enabled, context()) == RuleContribution((), ())
    loaded = loaded_rule_set(disabled)
    assert loaded.rules == ()
    assert loaded.evaluate_chain(malformed_for_enabled, context()) == RuleContribution((), ())


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_fails_before_enabled_dispatch(enabled):
    with pytest.raises(UnsupportedRuleSubjectError):
        rule(enabled=enabled).evaluate(NodeRuleSubject("node", node(0)), context())


def test_loader_rejects_missing_parameters_before_search():
    definition = RuleDefinitionSpec(
        "same", "SameSpecContinuousRealWeightRule", "同规格规则", RuleScope.CHAIN, True, "1", {}
    )
    spec = RuleSetSpec("LINE", "PROCESS", "month", "1", (definition,), (), frozenset(), "pending")
    with pytest.raises(RuleSetLoadError) as failure:
        load_rule_set(replace(spec, fingerprint=fingerprint_rule_set_spec(spec)))
    assert [issue.code for issue in failure.value.issues] == ["invalid_rule_parameters"]
