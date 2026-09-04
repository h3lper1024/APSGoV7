"""Optional customer-name matching supplies construction rank, not a quality level."""

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
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    NodeRuleSubject,
    Rule,
    RuleContribution,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import StrategicCustomerPriorityRule

D = Decimal
METRIC = "strategic_customer_rank"


def parameters(**changes):
    return {"contains_any": ("ACME", "重点"), "rank": -2, "default_rank": 5} | changes


def rule(**changes):
    return StrategicCustomerPriorityRule(
        **(
            dict(
                rule_id="strategic_customer_priority",
                name="战略客户构造优先级",
                scope=RuleScope.NODE,
                enabled=True,
                version="1",
                parameters=parameters(),
            )
            | changes
        )
    )


def node(index=0, attributes=None, role=MaterialRole.NORMAL_REAL):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        node_id=f"node-{index}",
        source_order_id=None if virtual else f"order-{index}",
        source_resource_id=None if virtual else f"resource-{index}",
        source_period=None if virtual else "period",
        weight=D("10"),
        width=None,
        thickness=None,
        min_temperature=None,
        max_temperature=None,
        grade="",
        material_role=role,
        rule_attributes={} if attributes is None else attributes,
        virtual_lineage=VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1)
        if virtual
        else None,
    )


def context():
    return RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def specification(**changes):
    definition = RuleDefinitionSpec(
        "strategic_customer_priority",
        "StrategicCustomerPriorityRule",
        "战略客户构造优先级",
        RuleScope.NODE,
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
    "attributes,expected",
    [
        ({}, 5),
        ({"customer_name": None}, 5),
        ({"customer_name": ""}, 5),
        ({"customer_name": " \t\n"}, 5),
        ({"customer_name": "OTHER"}, 5),
        ({"customer_name": "acme"}, 5),
        ({"customer_name": "  ACME 客户  "}, -2),
        ({"customer_name": "本地重点客户"}, -2),
        ({"customer_name": "ACME 重点客户"}, -2),
        ({"customer_name": "OTHER", "customer_grade": "ACME"}, 5),
        ({"customer_name": "ACME", "customer_grade": "OTHER"}, -2),
    ],
)
def test_optional_customer_name_drives_both_priority_and_metric_without_violations(
    attributes, expected
):
    current = rule()
    current_node = node(attributes=attributes)
    value = current.evaluate(NodeRuleSubject("order-subject", current_node), context())
    assert current.construction_priority(current_node) == (expected,)
    assert value.violations == ()
    assert [(item.metric_key, item.value) for item in value.metrics] == [(METRIC, expected)]
    assert type(value.metrics[0].value) is int


@pytest.mark.parametrize(
    "keywords,name,expected",
    [
        ([], "ACME", 5),
        ([" ACME "], "  ACME  ", 5),
        (["ACME "], "ACME 客户", -2),
        (["acme"], "ACME", 5),
        (["ACME", "ME", "ACME"], "ACME", -2),
    ],
)
def test_keywords_remain_verbatim_and_multiple_matches_do_not_accumulate(keywords, name, expected):
    current = rule(parameters=parameters(contains_any=keywords))
    assert current.parameters["contains_any"] == tuple(keywords)
    assert current.construction_priority(node(attributes={"customer_name": name})) == (expected,)


def test_signed_integer_ranks_are_used_verbatim_without_ordering_or_nonnegative_restrictions():
    current = rule(parameters=parameters(rank=7, default_rank=-9))
    assert current.construction_priority(node(attributes={"customer_name": "ACME"})) == (7,)
    assert current.construction_priority(node()) == (-9,)


@pytest.mark.parametrize("role", tuple(MaterialRole))
def test_all_material_roles_use_the_same_optional_name_projection(role):
    current = rule()
    assert current.construction_priority(node(role=role)) == (5,)
    matched = node(attributes={"customer_name": "ACME"}, role=role)
    assert current.construction_priority(matched) == (-2,)
    assert current.evaluate(NodeRuleSubject("node", matched), context()).metrics[0].value == -2


def test_split_fragment_uses_its_own_customer_name_attribute():
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
        node(attributes={"customer_name": "ACME"}),
        source_order_id="source",
        source_resource_id="resource",
        split_lineage=lineage,
    )
    assert rule().construction_priority(piece) == (-2,)
    assert rule().construction_priority(replace(piece, rule_attributes={})) == (5,)


@pytest.mark.parametrize("value", [True, 7, D("1")])
def test_nontext_name_fails_closed_in_both_entry_points_even_with_no_keywords(value):
    current = rule(parameters=parameters(contains_any=[]))
    current_node = node(attributes={"customer_name": value})
    with pytest.raises(ValueError, match=r"node-0\.customer_name.*text"):
        current.construction_priority(current_node)
    with pytest.raises(ValueError, match=r"node-0\.customer_name.*text"):
        current.evaluate(NodeRuleSubject("node", current_node), context())


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"contains_any": [], "rank": 1},
        {"contains_any": [], "default_rank": 1},
        parameters(extra=None),
        parameters(contains_any="ACME"),
        parameters(contains_any={"ACME": True}),
        parameters(contains_any=[""]),
        parameters(contains_any=["  "]),
        parameters(contains_any=[None]),
        parameters(rank=True),
        parameters(default_rank=True),
        parameters(rank=D(1)),
        parameters(default_rank="2"),
    ],
)
def test_invalid_business_parameters_are_rejected_with_located_loading_diagnostics(config):
    with pytest.raises(ValueError):
        rule(parameters=config)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(parameters=config))
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_rule_parameters"
    assert issue.field_path == "rule_set_spec.rules[0].parameters"
    assert issue.subject_id == "strategic_customer_priority"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR


@pytest.mark.parametrize("enabled", [True, False])
def test_wrong_subject_and_scope_are_rejected_even_when_disabled(enabled):
    current = rule(enabled=enabled)
    chain = ChainRuleSubject("chain", Chain("chain", (node(),), "period"))
    with pytest.raises(UnsupportedRuleSubjectError):
        current.evaluate(chain, context())
    with pytest.raises(ValueError):
        rule(enabled=enabled, scope=RuleScope.CHAIN)


def test_disabled_rule_still_validates_generic_parameter_shape():
    with pytest.raises(ValueError):
        rule(enabled=False, parameters={"rank": 1.0})
    with pytest.raises(ValueError):
        specification(enabled=False, parameters={"rank": 1.0})


def test_registered_rule_is_detached_frozen_and_stateless_without_a_new_quality_criterion():
    keywords = ["ACME", "重点"]
    current = rule(parameters=parameters(contains_any=keywords))
    current_node = node(attributes={"customer_name": "ACME"})
    current_subject = NodeRuleSubject("node-subject", current_node)
    current_context = context()
    before = fingerprint((current, current_subject, current_context))
    keywords.clear()
    assert current.parameters["contains_any"] == ("ACME", "重点")
    assert StrategicCustomerPriorityRule.__bases__ == (Rule,)
    assert RULE_REGISTRY["StrategicCustomerPriorityRule"] is StrategicCustomerPriorityRule
    assert current.required_fields() == ()
    assert current.metric_keys() == (METRIC,)
    loaded = load_rule_set(specification())
    assert loaded.rules_for_scope(RuleScope.NODE) == (current,)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == ()
    assert [item.metric_key for item in loaded.quality_spec] == [
        "prohibited_violation_count",
        "prohibited_violation_severity",
    ]
    first = current.evaluate(current_subject, current_context)
    assert loaded.evaluate_node(current_subject, current_context) == first
    assert loaded.construction_priority(current_node) == (-2,)
    assert current.construction_priority(node(1)) == (5,)
    assert current.evaluate(current_subject, current_context) == first
    assert fingerprint((current, current_subject, current_context)) == before
    with pytest.raises(FrozenInstanceError):
        current.enabled = False
    with pytest.raises(TypeError):
        current.parameters["rank"] = 0


def test_keyword_order_remains_signed_while_existing_ruleset_preserves_rule_priority_order():
    original = specification()
    reordered = specification(parameters=parameters(contains_any=("重点", "ACME")))
    current_node = node(attributes={"customer_name": "ACME 重点", "priority": 9})
    assert original.fingerprint != reordered.fingerprint
    assert load_rule_set(original).construction_priority(current_node) == (-2,)
    assert load_rule_set(reordered).construction_priority(current_node) == (-2,)
    synthetic = RuleDefinitionSpec(
        "secondary",
        "SyntheticNodePriorityRule",
        "次级优先级",
        RuleScope.NODE,
        True,
        "1",
        {"attribute": "priority"},
    )
    for definitions, expected in (
        ((*original.rules, synthetic), (-2, 9)),
        ((synthetic, *original.rules), (9, -2)),
    ):
        value = replace(original, rules=definitions)
        loaded = load_rule_set(replace(value, fingerprint=fingerprint_rule_set_spec(value)))
        assert loaded.construction_priority(current_node) == expected


def test_disabled_rule_bypasses_business_data_but_preserves_signed_identity():
    disabled = rule(enabled=False, parameters={})
    invalid_name = node(attributes={"customer_name": 7})
    assert disabled.required_fields() == disabled.metric_keys() == ()
    assert disabled.construction_priority(invalid_name) == ()
    assert disabled.evaluate(NodeRuleSubject("node", invalid_name), None) == RuleContribution(
        (), ()
    )
    value = specification(enabled=False, parameters={"legacy": ["ignored"], "rank": "bad"})
    loaded = load_rule_set(value)
    assert loaded.rules == loaded.rules_for_scope(RuleScope.NODE) == ()
    assert loaded.construction_priority(invalid_name) == ()
    assert loaded.evaluate_node(NodeRuleSubject("node", invalid_name), context()) == (
        RuleContribution((), ())
    )
    assert value.fingerprint != specification().fingerprint
    assert value.fingerprint != specification(enabled=False, parameters={}).fingerprint
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(value, fingerprint=specification().fingerprint))
    assert [item.code for item in caught.value.issues] == ["rule_set_fingerprint_mismatch"]


def test_loader_rejects_non_node_scope():
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(specification(scope=RuleScope.CHAIN))
    assert [(item.code, item.field_path) for item in caught.value.issues] == [
        ("rule_scope_mismatch", "rule_set_spec.rules[0].scope")
    ]
