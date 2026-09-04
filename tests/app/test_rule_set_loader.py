"""Configuration trust boundary, without real-line business rules or a solver."""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields, replace
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
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
    canonical_json,
)
from apsgo_scheduler.core.model import Chain, MaterialRole, Node
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    EdgeRuleSubject,
    NumericProjection,
    QualityAggregation,
    QualityDirection,
    Rule,
    RuleEvaluationContext,
)
from apsgo_scheduler.core.rules.concrete import (
    ChainWeightRangeRule,
    ControlledOrderSplitRule,
    SyntheticNodePriorityRule,
    SyntheticWidthLimitRule,
)


def criterion(metric_key, **changes):
    return replace(
        QualityCriterionSpec(metric_key, metric_key, "minimize", "named_value", "exact_decimal"),
        **changes,
    )


def definition(**changes):
    return replace(
        RuleDefinitionSpec(
            "width",
            "SyntheticWidthLimitRule",
            "合成宽度规则",
            RuleScope.EDGE,
            True,
            "1",
            {"maximum_increase": Decimal("50")},
        ),
        **changes,
    )


def spec(**changes):
    value = RuleSetSpec(
        "line-a",
        "process",
        "month",
        "1",
        (definition(),),
        (
            criterion("prohibited_violation_count"),
            criterion("prohibited_violation_severity"),
            criterion("chain_count"),
        ),
        frozenset({"chain_weight_below_minimum"}),
        "pending",
    )
    value = replace(value, **changes)
    return replace(value, fingerprint=fingerprint_rule_set_spec(value))


def underweight_spec(**quality_changes):
    gap = criterion(
        "underweight_total_gap",
        criterion_id="gap",
        aggregation="sum",
        numeric_projection="underweight_gap_round_2_then_sum",
    )
    return spec(
        rules=(
            definition(
                rule_id="weight",
                rule_type="ChainWeightRangeRule",
                name="链重范围规则",
                scope=RuleScope.CHAIN,
                parameters={
                    "min_weight": Decimal("1000"),
                    "max_weight": Decimal("2000"),
                    "target_weight": Decimal("1500"),
                },
            ),
        ),
        quality_spec=spec().quality_spec[:2] + (replace(gap, **quality_changes),),
    )


def node(node_id, width):
    return Node(
        node_id,
        node_id,
        node_id,
        "later",
        Decimal("100"),
        Decimal(width),
        Decimal("1"),
        None,
        None,
        "steel",
        MaterialRole.NORMAL_REAL,
        {"priority": 2},
    )


def issue_codes(value, **kwargs):
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(value, **kwargs)
    issues = caught.value.issues
    assert isinstance(issues, tuple) and issues
    assert all(item.phase is DiagnosticPhase.RULE_LOADING for item in issues)
    assert all(item.severity is DiagnosticSeverity.ERROR for item in issues)
    assert all(
        item.field_path and any("\u4e00" <= char <= "\u9fff" for char in item.message)
        for item in issues
    )
    return {item.code for item in issues}


def test_loading_preserves_frozen_identities_and_parameters():
    value = spec()
    loaded = load_rule_set(value)
    assert loaded.fingerprint == value.fingerprint
    assert loaded.product_line_code == value.product_line_code
    assert loaded.process_code == value.process_code
    assert loaded.scenario == value.scenario
    assert loaded.version == value.version
    assert loaded.allowed_final_deviation_codes == value.allowed_final_deviation_codes
    assert len(loaded.rules) == 1
    assert isinstance(loaded.rules[0], SyntheticWidthLimitRule)
    assert loaded.rules[0].parameters == value.rules[0].parameters
    with pytest.raises(TypeError):
        loaded.rules[0].parameters["maximum_increase"] = Decimal("999")
    with pytest.raises(FrozenInstanceError):
        loaded.version = "2"
    with pytest.raises(TypeError):
        RULE_REGISTRY["extra"] = SyntheticWidthLimitRule
    assert RULE_REGISTRY["ControlledOrderSplitRule"] is ControlledOrderSplitRule


def test_two_lines_change_edge_results_and_declared_quality_order_without_core_changes():
    quality_a = spec().quality_spec + (criterion("borrowed_future_weight"),)
    quality_b = quality_a[:2] + tuple(reversed(quality_a[2:]))
    line_a = load_rule_set(spec(quality_spec=quality_a))
    line_b = load_rule_set(
        spec(
            product_line_code="line-b",
            rules=(definition(parameters={"maximum_increase": Decimal("100")}),),
            quality_spec=quality_b,
        )
    )
    subject = EdgeRuleSubject("edge", node("left", "1000"), node("right", "1075"))
    context = RuleEvaluationContext(("later", "earlier"), {"later": 0, "earlier": 1}, ())
    assert line_a.evaluate_edge(subject, context).violations
    assert not line_b.evaluate_edge(subject, context).violations
    assert tuple(item.metric_key for item in line_a.quality_spec) == tuple(
        item.metric_key for item in quality_a
    )
    assert tuple(item.metric_key for item in line_b.quality_spec) == tuple(
        item.metric_key for item in quality_b
    )
    assert line_a.fingerprint != line_b.fingerprint


def test_node_rule_uses_only_configured_attribute():
    loaded = load_rule_set(
        spec(
            rules=(
                definition(
                    rule_id="priority",
                    rule_type="SyntheticNodePriorityRule",
                    scope=RuleScope.NODE,
                    parameters={"attribute": "priority"},
                ),
            )
        )
    )
    assert isinstance(loaded.rules[0], SyntheticNodePriorityRule)
    assert loaded.rules[0].required_fields() == ("priority",)
    assert loaded.construction_priority(node("a", "1000"))


@pytest.mark.parametrize("field", ["product_line_code", "process_code", "scenario", "version"])
def test_each_top_level_identity_changes_fingerprint(field):
    value = spec()
    altered = replace(value, **{field: "different"})
    assert fingerprint_rule_set_spec(value) != fingerprint_rule_set_spec(altered)
    assert "rule_set_fingerprint_mismatch" in issue_codes(altered)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("rule_id", "another"),
        ("rule_type", "UnknownType"),
        ("name", "changed"),
        ("scope", RuleScope.CHAIN),
        ("enabled", False),
        ("version", "2"),
        ("parameters", {"maximum_increase": Decimal("51")}),
    ],
)
def test_each_rule_field_changes_fingerprint(field, changed):
    value = spec()
    altered = replace(value, rules=(replace(value.rules[0], **{field: changed}),))
    assert fingerprint_rule_set_spec(value) != fingerprint_rule_set_spec(altered)
    assert "rule_set_fingerprint_mismatch" in issue_codes(altered)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("criterion_id", "another"),
        ("metric_key", "generated_virtual_weight"),
        ("direction", "maximize"),
        ("aggregation", "sum"),
        ("numeric_projection", "reference_float_round_6"),
    ],
)
def test_each_quality_field_changes_fingerprint(field, changed):
    value = spec()
    altered = replace(
        value,
        quality_spec=value.quality_spec[:2] + (replace(value.quality_spec[2], **{field: changed}),),
    )
    assert fingerprint_rule_set_spec(value) != fingerprint_rule_set_spec(altered)
    assert "rule_set_fingerprint_mismatch" in issue_codes(altered)


def test_fingerprint_covers_allowed_deviations_and_disabled_records_but_not_itself():
    value = spec(rules=(definition(enabled=False),))
    assert fingerprint_rule_set_spec(value) == fingerprint_rule_set_spec(
        replace(value, fingerprint="different")
    )
    assert fingerprint_rule_set_spec(value) != fingerprint_rule_set_spec(
        replace(value, allowed_final_deviation_codes=frozenset())
    )
    changed = replace(value, rules=(replace(value.rules[0], parameters={}),))
    assert "rule_set_fingerprint_mismatch" in issue_codes(changed)


def test_mapping_and_set_order_do_not_matter_but_ordered_sequences_do():
    first = definition(parameters={"a": Decimal("50"), "b": True}, enabled=False)
    reordered = replace(first, parameters={"b": True, "a": Decimal("50.0")})
    value = spec(rules=(first, definition(rule_id="second", enabled=False)))
    assert fingerprint_rule_set_spec(value) == fingerprint_rule_set_spec(
        replace(value, rules=(reordered, value.rules[1]))
    )
    assert fingerprint_rule_set_spec(value) != fingerprint_rule_set_spec(
        replace(value, rules=tuple(reversed(value.rules)))
    )
    assert fingerprint_rule_set_spec(value) != fingerprint_rule_set_spec(
        replace(value, quality_spec=tuple(reversed(value.quality_spec)))
    )
    assert (
        spec(allowed_final_deviation_codes=("a", "b")).fingerprint
        == spec(allowed_final_deviation_codes=("b", "a")).fingerprint
    )


def test_flat_rule_set_retains_pre_recursive_parameter_encoding_and_fingerprint():
    # Captured from the unmodified 6fb7a2b git archive, not computed by the new freezer.
    expected_encoding = (
        '["object",[["allowed_final_deviation_codes",["set",["chain_weight_below_minimum"]]],'
        '["process_code","process"],["product_line_code","line-a"],["quality_spec",["array",'
        '[["object",[["aggregation","named_value"],["criterion_id","prohibited_violation_count"],'
        '["direction","minimize"],["metric_key","prohibited_violation_count"],'
        '["numeric_projection","exact_decimal"]]],["object",[["aggregation","named_value"],'
        '["criterion_id","prohibited_violation_severity"],["direction","minimize"],'
        '["metric_key","prohibited_violation_severity"],["numeric_projection","exact_decimal"]]],'
        '["object",[["aggregation","named_value"],["criterion_id","chain_count"],'
        '["direction","minimize"],["metric_key","chain_count"],'
        '["numeric_projection","exact_decimal"]]]]]],["rules",["array",[["object",'
        r'[["enabled",true],["name","\u5408\u6210\u5bbd\u5ea6\u89c4\u5219"],'
        '["parameters",["object",[["maximum_increase",["decimal",0,"5",1]]]]],'
        '["rule_id","width"],["rule_type","SyntheticWidthLimitRule"],["scope","edge"],'
        '["version","1"]]]]]],["scenario","month"],["version","1"]]]'
    )
    expected_fingerprint = "c9c1632b8f81e70e3c6485511ebd4df949b3c28cff574bafea3e85a25a9f8720"
    value = spec()
    payload = {
        part.name: getattr(value, part.name) for part in fields(value) if part.name != "fingerprint"
    }
    assert canonical_json(payload) == expected_encoding
    assert fingerprint_rule_set_spec(value) == value.fingerprint == expected_fingerprint
    assert load_rule_set(value).fingerprint == expected_fingerprint


def test_reference_float_projection_keeps_pre_fix_signed_fingerprint():
    # Captured from the unmodified 220f0937 archive; the exact-decimal golden is above.
    original = spec()
    quality = original.quality_spec[:2] + (
        replace(original.quality_spec[2], numeric_projection="reference_float_round_6"),
    )
    value = spec(quality_spec=quality)
    expected = "8b18f8a957a2d4b59d067b49f15282c9c6a4c05613b32a31bd9cde177bd0d1d3"
    assert value.fingerprint == fingerprint_rule_set_spec(value) == expected
    loaded = load_rule_set(value)
    assert loaded.fingerprint == expected
    assert loaded.quality_spec[-1].numeric_projection is NumericProjection.REFERENCE_FLOAT_ROUND_6


def test_underweight_projection_loads_without_rounding_rule_contributions():
    value = underweight_spec()
    loaded = load_rule_set(value)
    assert loaded.fingerprint == value.fingerprint
    assert isinstance(loaded.rules[0], ChainWeightRangeRule)
    gap = loaded.quality_spec[-1]
    assert gap.criterion_id == "gap"
    assert gap.metric_key == "underweight_total_gap"
    assert gap.direction is QualityDirection.MINIMIZE
    assert gap.aggregation is QualityAggregation.SUM
    assert gap.numeric_projection is NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM
    with pytest.raises(FrozenInstanceError):
        gap.aggregation = QualityAggregation.MAXIMUM
    current = replace(node("order", "1000"), weight=Decimal("999.9949"))
    subject = ChainRuleSubject("chain", Chain("chain", (current,), "later"))
    context = RuleEvaluationContext(("later",), {"later": 0}, ())
    contribution = loaded.evaluate_chain(subject, context)
    metrics = {item.metric_key: item.value for item in contribution.metrics}
    assert metrics["underweight_total_gap"] == Decimal("0.0051")
    assert contribution.violations[0].severity == Decimal("0.0051")
    old = load_rule_set(underweight_spec(numeric_projection="exact_decimal"))
    assert contribution == old.evaluate_chain(subject, context)


@pytest.mark.parametrize("projection", ["exact_decimal", "reference_float_round_6"])
def test_underweight_projection_change_requires_a_new_complete_signature(projection):
    original = underweight_spec(numeric_projection=projection)
    updated = underweight_spec()
    assert original.fingerprint != updated.fingerprint
    assert load_rule_set(original).quality_spec[-1].numeric_projection.value == projection
    assert load_rule_set(updated).fingerprint == updated.fingerprint
    for tampered in (
        replace(updated, fingerprint=original.fingerprint),
        replace(original, fingerprint=updated.fingerprint),
    ):
        assert issue_codes(tampered, registry=NeverReadRegistry()) == {
            "rule_set_fingerprint_mismatch"
        }


def test_nested_parameters_pass_through_loader_without_mutable_aliases():
    parameters = {
        "maximum_increase": Decimal("50"),
        "groups": {
            "grades": ["FC", "FD", "FC"],
            "ranges": [{"upper": Decimal("0.6"), "limit": Decimal("0.2")}],
            "empty": {},
            "optional": None,
        },
    }
    value = spec(rules=(definition(parameters=parameters),))
    loaded = load_rule_set(value)
    before = canonical_json(loaded.rules[0].parameters)
    original_fingerprint = value.fingerprint
    parameters["maximum_increase"] = Decimal("99")
    parameters["groups"]["grades"][0] = "changed"
    parameters["groups"]["ranges"][0]["limit"] = Decimal("9")
    parameters["groups"]["ranges"].append({"upper": Decimal("10")})
    parameters["groups"]["empty"]["new"] = True
    parameters["groups"]["optional"] = "changed"
    assert canonical_json(loaded.rules[0].parameters) == before
    assert loaded.rules[0].parameters == value.rules[0].parameters
    assert loaded.fingerprint == fingerprint_rule_set_spec(value) == original_fingerprint
    for frozen in (value.rules[0].parameters, loaded.rules[0].parameters):
        assert frozen["groups"]["grades"] == ("FC", "FD", "FC")
        assert frozen["groups"]["ranges"] == ({"upper": Decimal("0.6"), "limit": Decimal("0.2")},)
        assert frozen["groups"]["empty"] == {}
        assert frozen["groups"]["optional"] is None
        with pytest.raises(TypeError):
            frozen["groups"]["ranges"][0]["limit"] = Decimal("9")
        with pytest.raises(TypeError):
            frozen["groups"]["grades"][0] = "changed"
        with pytest.raises(TypeError):
            frozen["groups"]["empty"]["new"] = True


def test_nested_mapping_order_and_equal_list_tuple_share_one_identity():
    first = {
        "maximum_increase": Decimal("50"),
        "groups": {"grades": ["FC", "FD"], "ranges": [{"limit": Decimal("0.2"), "enabled": True}]},
    }
    second = {
        "groups": {
            "ranges": ({"enabled": True, "limit": Decimal("0.20")},),
            "grades": ("FC", "FD"),
        },
        "maximum_increase": Decimal("50.0"),
    }
    left, right = (spec(rules=(definition(parameters=value),)) for value in (first, second))
    assert canonical_json(left.rules[0].parameters) == canonical_json(right.rules[0].parameters)
    assert left.fingerprint == right.fingerprint
    assert load_rule_set(left).fingerprint == load_rule_set(right).fingerprint


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "changed_groups",
    [
        {"grades": ["FD", "FC"], "limit": Decimal("1"), "flag": True},
        {"grades": ["FC", "FD"], "limit": Decimal("2"), "flag": True},
        {"grades": ["FC", "FD"], "limit": Decimal("1"), "flag": 1},
    ],
)
def test_nested_sequence_leaf_and_boolean_changes_alter_enabled_or_disabled_identity(
    enabled, changed_groups
):
    parameters = {
        "maximum_increase": Decimal("50"),
        "groups": {"grades": ["FC", "FD"], "limit": Decimal("1"), "flag": True},
    }
    value = spec(rules=(definition(enabled=enabled, parameters=parameters),))
    changed = replace(
        value,
        rules=(replace(value.rules[0], parameters={**parameters, "groups": changed_groups}),),
    )
    assert fingerprint_rule_set_spec(changed) != value.fingerprint
    assert "rule_set_fingerprint_mismatch" in issue_codes(changed)


class NeverReadRegistry(Mapping):
    def __getitem__(self, key):
        raise AssertionError("disabled rules must not query the registry")

    def __len__(self):
        return 0

    def __iter__(self):
        raise AssertionError("disabled rules must not inspect the registry")


def test_disabled_unknown_or_missing_parameters_never_look_up_or_construct():
    value = spec(
        rules=(
            definition(enabled=False, rule_type="UnknownType", parameters={}),
            definition(rule_id="second", enabled=False, parameters={}),
        )
    )
    loaded = load_rule_set(value, registry=NeverReadRegistry())
    assert loaded.rules == ()
    assert loaded.fingerprint == value.fingerprint
    subject = EdgeRuleSubject("edge", node("left", "1000"), node("right", "2000"))
    context = RuleEvaluationContext(("later",), {"later": 0}, ())
    assert not loaded.evaluate_edge(subject, context).violations
    assert not loaded.evaluate_edge(subject, context).metrics
    assert loaded.construction_priority(node("a", "1000")) == ()


def test_disabled_structured_config_needs_no_registration_or_business_parameters():
    parameters = {"groups": {"grades": ["FC", "FD"], "empty": [], "optional": None}}
    value = spec(
        rules=(
            definition(enabled=False, rule_type="UnknownType", parameters=parameters),
            definition(rule_id="known", enabled=False, parameters=parameters),
        )
    )
    loaded = load_rule_set(value, registry=NeverReadRegistry())
    assert loaded.rules == ()
    assert loaded.fingerprint == fingerprint_rule_set_spec(value)
    assert value.rules[0].parameters["groups"]["empty"] == ()


@pytest.mark.parametrize(
    "invalid",
    [
        1.5,
        Decimal("NaN"),
        Decimal("Infinity"),
        {"FC"},
        frozenset({"FD"}),
        object(),
        {1: "FC"},
        {" ": "FD"},
    ],
)
def test_disabled_unknown_config_still_rejects_invalid_nested_shapes(invalid):
    with pytest.raises(ValueError):
        definition(enabled=False, rule_type="UnknownType", parameters={"groups": [invalid]})


def test_disabled_unknown_config_rejects_cycles_before_loader_lookup():
    parameters = {"groups": []}
    parameters["groups"].append(parameters)
    with pytest.raises(ValueError, match="groups"):
        definition(enabled=False, rule_type="UnknownType", parameters=parameters)


@pytest.mark.parametrize("enabled", [True, False])
def test_duplicate_rule_identity_fails_even_when_disabled(enabled):
    assert "duplicate_rule_id" in issue_codes(
        spec(rules=(definition(), definition(enabled=enabled)))
    )


@pytest.mark.parametrize("field", ["rule_id", "rule_type", "name", "version"])
def test_disabled_config_still_requires_nonempty_identity(field):
    assert "empty_identity" in issue_codes(spec(rules=(definition(enabled=False, **{field: " "}),)))


@pytest.mark.parametrize("field", ["product_line_code", "process_code", "scenario", "version"])
def test_rule_set_identity_must_be_nonempty(field):
    assert "empty_identity" in issue_codes(spec(**{field: " "}))


@pytest.mark.parametrize(
    "parameters", [{}, {"maximum_increase": Decimal("-1")}, {"maximum_increase": "50"}]
)
def test_enabled_rule_must_have_valid_explicit_parameters(parameters):
    assert "invalid_rule_parameters" in issue_codes(
        spec(rules=(definition(parameters=parameters),))
    )


def test_unknown_enabled_rule_and_mismatched_scope_fail_before_search():
    assert "unknown_enabled_rule" in issue_codes(spec(rules=(definition(rule_type="UnknownType"),)))
    assert "rule_scope_mismatch" in issue_codes(spec(rules=(definition(scope=RuleScope.CHAIN),)))


@pytest.mark.parametrize("field", ["direction", "aggregation", "numeric_projection"])
def test_unknown_quality_enum_is_rejected(field):
    quality = spec().quality_spec[:2] + (criterion("chain_count", **{field: "unsupported"}),)
    assert "invalid_quality_option" in issue_codes(spec(quality_spec=quality))


@pytest.mark.parametrize(
    "change",
    [
        {"metric_key": "chain_count"},
        {"metric_key": "underweight_chain_count"},
        {"metric_key": "overweight_total_excess"},
        {"aggregation": "named_value"},
        {"aggregation": "count"},
        {"aggregation": "maximum"},
        {"direction": "maximize"},
    ],
)
def test_underweight_projection_combination_errors_identify_the_criterion(change):
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(underweight_spec(**change), registry=NeverReadRegistry())
    assert len(caught.value.issues) == 1
    issue = caught.value.issues[0]
    assert issue.code == "invalid_quality_combination"
    assert issue.field_path == "rule_set_spec.quality_spec[2]"
    assert issue.subject_id == "gap"
    assert issue.phase is DiagnosticPhase.RULE_LOADING
    assert issue.severity is DiagnosticSeverity.ERROR
    assert "质量项" in issue.message


def test_underweight_combination_errors_aggregate_with_enum_and_signature_errors():
    original = underweight_spec()
    gap = original.quality_spec[-1]
    quality = original.quality_spec[:2] + (
        replace(gap, criterion_id="wrong-metric", metric_key="chain_count"),
        replace(gap, criterion_id="wrong-aggregation", aggregation="maximum"),
        replace(gap, criterion_id="wrong-direction", direction="maximize"),
        replace(gap, criterion_id="unknown-option", numeric_projection="unknown"),
    )
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(original, quality_spec=quality), registry=NeverReadRegistry())
    assert tuple((item.code, item.field_path, item.subject_id) for item in caught.value.issues) == (
        ("invalid_quality_combination", "rule_set_spec.quality_spec[2]", "wrong-metric"),
        ("invalid_quality_combination", "rule_set_spec.quality_spec[3]", "wrong-aggregation"),
        ("invalid_quality_combination", "rule_set_spec.quality_spec[4]", "wrong-direction"),
        (
            "invalid_quality_option",
            "rule_set_spec.quality_spec[5].numeric_projection",
            "unknown-option",
        ),
        ("rule_set_fingerprint_mismatch", "rule_set_spec.fingerprint", None),
    )
    assert all(item.phase is DiagnosticPhase.RULE_LOADING for item in caught.value.issues)
    assert all(item.severity is DiagnosticSeverity.ERROR for item in caught.value.issues)


def test_quality_identity_metric_source_and_hard_first_order_are_checked():
    quality = spec().quality_spec
    assert "duplicate_criterion_id" in issue_codes(spec(quality_spec=quality + (quality[2],)))
    assert "invalid_rule_set" in issue_codes(
        spec(quality_spec=quality[:2] + (criterion("unknown_metric"),))
    )
    assert "invalid_rule_set" in issue_codes(spec(quality_spec=tuple(reversed(quality))))
    assert "invalid_rule_set" in issue_codes(
        spec(quality_spec=(replace(quality[0], direction="maximize"),) + quality[1:])
    )
    assert "invalid_rule_set" in issue_codes(spec(quality_spec=()))
    assert "empty_identity" in issue_codes(spec(allowed_final_deviation_codes=frozenset({" "})))


def test_disabled_metric_cannot_be_used_as_a_quality_producer():
    quality = spec().quality_spec + (criterion("synthetic_width_increase"),)
    assert load_rule_set(spec(quality_spec=quality))
    assert "invalid_rule_set" in issue_codes(
        spec(rules=(definition(enabled=False),), quality_spec=quality)
    )


def test_duplicate_metric_producers_fail_even_with_distinct_rule_ids():
    assert "invalid_rule_set" in issue_codes(
        spec(rules=(definition(), definition(rule_id="another")))
    )


@pytest.mark.parametrize("registered", [Rule, object, 1, SyntheticNodePriorityRule])
def test_invalid_registry_entry_is_not_instantiated(registered):
    assert "invalid_rule_registration" in issue_codes(
        spec(), registry={"SyntheticWidthLimitRule": registered}
    )


def test_registry_name_must_match_class_name_and_inheritance_must_be_direct():
    assert "invalid_rule_registration" in issue_codes(
        spec(rules=(definition(rule_type="Alias"),)), registry={"Alias": SyntheticWidthLimitRule}
    )

    class ThirdLevelRule(SyntheticWidthLimitRule):
        pass

    assert "invalid_rule_registration" in issue_codes(
        spec(rules=(definition(rule_type="ThirdLevelRule"),)),
        registry={"ThirdLevelRule": ThirdLevelRule},
    )


def test_rule_without_scope_declaration_fails_as_configuration_error():
    class MissingScopeRule(Rule):
        def required_fields(self):
            return ()

    assert "rule_scope_mismatch" in issue_codes(
        spec(rules=(definition(rule_type="MissingScopeRule", parameters={}),)),
        registry={"MissingScopeRule": MissingScopeRule},
    )


@pytest.mark.parametrize("required", [["width"], ("width", "width"), ("",), (1,)])
def test_invalid_required_field_declarations_fail(required):
    class BadFieldsRule(Rule):
        supported_scope = RuleScope.EDGE

        def required_fields(self):
            return required

    assert "invalid_required_fields" in issue_codes(
        spec(rules=(definition(rule_type="BadFieldsRule", parameters={}),)),
        registry={"BadFieldsRule": BadFieldsRule},
    )


def test_configuration_errors_are_aggregated_without_running_rule_constructors():
    value = spec(
        product_line_code=" ",
        rules=(definition(), definition()),
        quality_spec=spec().quality_spec + (criterion("chain_count", direction="bad"),),
    )
    value = replace(value, fingerprint="mismatch")
    assert {
        "empty_identity",
        "duplicate_rule_id",
        "duplicate_criterion_id",
        "invalid_quality_option",
        "rule_set_fingerprint_mismatch",
    } <= issue_codes(value, registry=NeverReadRegistry())


def test_enabled_rule_errors_are_aggregated():
    value = spec(
        rules=(
            definition(rule_type="UnknownType"),
            definition(rule_id="missing", parameters={}),
            definition(rule_id="scope", scope=RuleScope.PLAN),
        )
    )
    assert {
        "unknown_enabled_rule",
        "invalid_rule_parameters",
        "rule_scope_mismatch",
    } == issue_codes(value)


def test_unexpected_implementation_errors_are_not_misreported_as_user_configuration():
    class CrashingRule(Rule):
        supported_scope = RuleScope.EDGE

        def required_fields(self):
            raise RuntimeError("implementation bug")

    with pytest.raises(RuntimeError, match="implementation bug"):
        load_rule_set(
            spec(rules=(definition(rule_type="CrashingRule", parameters={}),)),
            registry={"CrashingRule": CrashingRule},
        )


def test_loader_reports_wrong_input_shape_and_registry_type():
    assert "invalid_rule_set_spec" in issue_codes(None)
    assert "invalid_rule_registry" in issue_codes(spec(), registry=[])
    with pytest.raises(ValueError, match="RuleSetSpec"):
        fingerprint_rule_set_spec(None)
