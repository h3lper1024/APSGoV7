"""Typed requests retain invalid semantics for aggregate, deterministic diagnosis."""

from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, localcontext

import pytest

from apsgo_scheduler.api.request import (
    OrderInput,
    PeriodInput,
    QualityCriterionSpec,
    RuleDefinitionSpec,
    RuleScope,
    RuleSetSpec,
    SchedulingRequest,
    SolverPolicy,
    VirtualPrototypeInput,
    fingerprint_public_request,
    validate_request,
)
from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    DiagnosticPhase,
    DiagnosticSeverity,
    canonical_json,
    fingerprint,
)
from apsgo_scheduler.core.model import MaterialRole


def order(**changes):
    values = dict(
        node_id="node-1",
        source_order_id="order-1",
        source_resource_id="resource-1",
        source_period="p0",
        weight=Decimal("12"),
        width=Decimal("1000"),
        thickness=Decimal("0.8"),
        min_temperature=None,
        max_temperature=None,
        grade="steel",
        material_role=MaterialRole.NORMAL_REAL,
        rule_attributes={},
    )
    return OrderInput(**(values | changes))


def request(**changes):
    rule = RuleDefinitionSpec(
        "rule-1",
        "registered_later",
        "重量规则",
        RuleScope.CHAIN,
        True,
        "1",
        {"maximum": Decimal("600")},
    )
    criterion = QualityCriterionSpec(
        "quality-1", "chain_count", "minimize", "count", "exact_decimal"
    )
    rules = RuleSetSpec(
        "LINE",
        "PROCESS",
        "month",
        "1",
        [rule],
        [criterion],
        {"chain_weight_below_minimum"},
        "claimed-rules",
    )
    policy = SolverPolicy(
        7,
        Decimal("12"),
        Decimal("2"),
        0,
        CONSTRUCTION_ORDER_KEY,
        NUMERIC_SEMANTICS_KEY,
        Decimal("0"),
    )
    values = dict(
        contract_version="1",
        request_id="request-1",
        product_line_code="LINE",
        process_code="PROCESS",
        scenario="month",
        orders=[order()],
        periods=[PeriodInput("p0", 0)],
        virtual_prototypes=[],
        rule_set_spec=rules,
        policy=policy,
    )
    return SchedulingRequest(**(values | changes))


@pytest.mark.parametrize(
    "value_type,expected",
    [
        (
            OrderInput,
            "node_id source_order_id source_resource_id source_period weight width "
            "thickness min_temperature max_temperature grade material_role rule_attributes",
        ),
        (PeriodInput, "period_id sequence"),
        (
            VirtualPrototypeInput,
            "prototype_id unit_weight width thickness min_temperature "
            "max_temperature grade rule_attributes",
        ),
        (RuleDefinitionSpec, "rule_id rule_type name scope enabled version parameters"),
        (QualityCriterionSpec, "criterion_id metric_key direction aggregation numeric_projection"),
        (
            RuleSetSpec,
            "product_line_code process_code scenario version rules quality_spec "
            "allowed_final_deviation_codes fingerprint",
        ),
        (
            SchedulingRequest,
            "contract_version request_id product_line_code process_code scenario "
            "orders periods virtual_prototypes rule_set_spec policy delivery_timing",
        ),
    ],
)
def test_public_input_field_lists_match_design(value_type, expected):
    assert [field.name for field in fields(value_type)] == expected.split()


def test_request_copies_and_freezes_all_supplied_collections():
    attributes = {"family": "IF", "rank": 1, "limit": Decimal("2"), "flag": True}
    orders = [order(rule_attributes=attributes)]
    parameters = {"maximum": Decimal("600")}
    rule = replace(request().rule_set_spec.rules[0], parameters=parameters)
    definitions, criteria = [rule], list(request().rule_set_spec.quality_spec)
    deviations = {"chain_weight_below_minimum"}
    spec = replace(
        request().rule_set_spec,
        rules=definitions,
        quality_spec=criteria,
        allowed_final_deviation_codes=deviations,
    )
    result = request(orders=orders, rule_set_spec=spec)
    attributes["rank"] = 99
    parameters["maximum"] = Decimal("1")
    orders.clear()
    definitions.clear()
    criteria.clear()
    deviations.clear()
    assert result.orders[0].rule_attributes["rank"] == 1
    assert result.rule_set_spec.rules[0].parameters["maximum"] == Decimal("600")
    assert len(result.rule_set_spec.quality_spec) == 1
    assert result.rule_set_spec.allowed_final_deviation_codes == frozenset(
        {"chain_weight_below_minimum"}
    )
    with pytest.raises(TypeError):
        result.orders[0].rule_attributes["rank"] = 2
    with pytest.raises(TypeError):
        result.rule_set_spec.rules[0].parameters["maximum"] = Decimal("1")
    with pytest.raises(FrozenInstanceError):
        result.request_id = "changed"
    assert not hasattr(result, "__dict__")


def test_constructors_reject_shapes_but_preserve_semantic_errors():
    for changes in (
        {"weight": 12.0},
        {"weight": None},
        {"node_id": None},
        {"material_role": "normal_real"},
        {"rule_attributes": {"nested": []}},
    ):
        with pytest.raises(ValueError):
            order(**changes)
    for changes in (
        {"orders": [object()]},
        {"orders": set()},
        {"policy": object()},
        {"rule_set_spec": object()},
    ):
        with pytest.raises(ValueError):
            request(**changes)
    with pytest.raises(ValueError):
        PeriodInput("p0", True)
    for changes in ({"scope": "chain"}, {"enabled": 1}, {"parameters": {"x": 1.0}}):
        with pytest.raises(ValueError):
            replace(request().rule_set_spec.rules[0], **changes)
    with pytest.raises(TypeError):
        order(split_lineage=object())
    assert order(weight=Decimal("-1"), node_id="").weight == Decimal("-1")
    assert PeriodInput("", -1).sequence == -1


def test_flat_parameter_encoding_and_request_fingerprints_match_pre_fix_goldens():
    # Captured from a clean git archive of 6fb7a2b before recursive parameters were implemented.
    original = request()
    assert fingerprint_public_request(original) == (
        "62f9672badd9984b46106043d664e4f954e2b538b1fff214422e0184d6c4ceff"
    )
    parameters = {
        "maximum": Decimal("600.00"),
        "labels": "FC,FD",
        "count": 5,
        "enabled": True,
        "optional": None,
    }
    rule = replace(original.rule_set_spec.rules[0], parameters=parameters)
    value = replace(original, rule_set_spec=replace(original.rule_set_spec, rules=(rule,)))
    assert canonical_json(rule.parameters) == (
        '["object",[["count",5],["enabled",true],["labels","FC,FD"],'
        '["maximum",["decimal",0,"6",2]],["optional",null]]]'
    )
    assert fingerprint(rule.parameters) == (
        "bed59cd5a4ade0554d0a5e83fe6c06083ef24362b4ce63e8f1518a0cd884dd63"
    )
    assert fingerprint_public_request(value) == (
        "683e4b73f8b5f9498cef088ae879105ea83af6f5df9fbedba3133d2c29dcf1a6"
    )


def test_nested_rule_parameters_are_detached_and_deeply_frozen_in_requests():
    grades = ["FC", "FD", "FC"]
    interval = {"upper": Decimal("0.6"), "limit": Decimal("0.2")}
    ranges = [interval]
    groups = {"basis": "thinner", "ranges": ranges}
    parameters = {"grades": grades, "groups": groups, "empty": [[], {}, None]}
    original = request()
    rule = replace(original.rule_set_spec.rules[0], parameters=parameters)
    value = replace(original, rule_set_spec=replace(original.rule_set_spec, rules=(rule,)))
    expected = fingerprint_public_request(value)
    grades.reverse()
    grades.append("EXTRA")
    interval["upper"] = Decimal("99")
    ranges.clear()
    groups.clear()
    parameters.clear()
    frozen = value.rule_set_spec.rules[0].parameters
    assert frozen["grades"] == ("FC", "FD", "FC")
    assert frozen["groups"]["ranges"] == ({"upper": Decimal("0.6"), "limit": Decimal("0.2")},)
    assert frozen["empty"] == ((), {}, None)
    assert fingerprint_public_request(value) == expected
    assert validate_request(value) == ()
    with pytest.raises(TypeError):
        frozen["groups"]["ranges"][0]["upper"] = Decimal("1")
    with pytest.raises(TypeError):
        frozen["grades"][0] = "CHANGED"
    with pytest.raises(TypeError):
        frozen["empty"][1]["new"] = 1


def test_nested_request_parameter_identity_preserves_types_and_order():
    original = request()

    def identity(parameters):
        rule = replace(original.rule_set_spec.rules[0], parameters=parameters)
        return fingerprint_public_request(
            replace(original, rule_set_spec=replace(original.rule_set_spec, rules=(rule,)))
        )

    expected = identity({"group": {"grades": ["FC", "FD"], "flag": True}})
    assert identity({"group": {"flag": True, "grades": ("FC", "FD")}}) == expected
    assert identity({"group": {"flag": 1, "grades": ["FC", "FD"]}}) != expected
    assert identity({"group": {"flag": True, "grades": ["FD", "FC"]}}) != expected
    assert identity({"group": {"flag": True, "grades": ["FC", "FE"]}}) != expected


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "parameters",
    [
        {"group": {"": 1}},
        {"group": {" ": 1}},
        {"group": {1: "value"}},
        {"group": [1.0]},
        {"group": [Decimal("NaN")]},
        {"group": [Decimal("Infinity")]},
        {"group": {"FC", "FD"}},
        {"group": frozenset({"FC", "FD"})},
        {"group": object()},
    ],
)
def test_all_rule_definitions_reject_invalid_nested_parameter_shapes(parameters, enabled):
    with pytest.raises(ValueError):
        replace(request().rule_set_spec.rules[0], parameters=parameters, enabled=enabled)


@pytest.mark.parametrize("enabled", [True, False])
def test_all_rule_definitions_reject_parameter_cycles_with_location(enabled):
    sequence = []
    sequence.append(sequence)
    mapping = {}
    mapping["again"] = mapping
    for cycle in (sequence, mapping):
        with pytest.raises(ValueError, match="cycle"):
            replace(request().rule_set_spec.rules[0], parameters={"cycle": cycle}, enabled=enabled)


@pytest.mark.parametrize("collection", [["FC"], ("FC",), {"grade": "FC"}])
def test_collection_parameters_do_not_expand_order_or_prototype_attributes(collection):
    rule = replace(request().rule_set_spec.rules[0], parameters={"grades": collection})
    assert rule.parameters["grades"]
    with pytest.raises(ValueError):
        order(rule_attributes={"grades": collection})
    with pytest.raises(ValueError):
        VirtualPrototypeInput(
            "prototype", Decimal("5"), None, None, None, None, "SPHC", {"grades": collection}
        )


def test_validation_reports_all_independent_errors_in_discovery_order():
    bad = request(
        request_id="",
        orders=[
            order(
                weight=Decimal("-1"),
                width=Decimal("0"),
                source_period="missing",
                material_role=MaterialRole.GENERATED_VIRTUAL,
                min_temperature=Decimal("700"),
                max_temperature=Decimal("600"),
            )
        ],
    )
    issues = validate_request(bad)
    assert [issue.field_path for issue in issues] == [
        "request_id",
        "orders[0].weight",
        "orders[0].width",
        "orders[0].max_temperature",
        "orders[0].source_period",
        "orders[0].material_role",
    ]
    assert {issue.code for issue in issues} == {
        "empty_identity",
        "non_positive_weight",
        "non_positive_dimension",
        "reversed_temperature_interval",
        "unknown_source_period",
        "unsupported_input_material_role",
    }
    assert all(issue.phase is DiagnosticPhase.REQUEST_VALIDATION for issue in issues)
    assert all(issue.severity is DiagnosticSeverity.ERROR and issue.message for issue in issues)
    assert validate_request(bad) == issues
    assert len(fingerprint_public_request(bad)) == 64


@pytest.mark.parametrize("count", [1, 2, 5])
def test_period_directory_is_not_fixed_to_four_and_sequence_is_authoritative(count):
    periods = [PeriodInput(f"p{index}", index) for index in range(count)]
    assert validate_request(request(periods=periods)) == ()
    assert validate_request(request(periods=list(reversed(periods)))) == ()


def test_period_and_input_identity_errors_are_aggregated():
    bad = request(orders=[order(), order()], periods=[PeriodInput("p0", 0), PeriodInput("p0", 2)])
    actual = {(issue.code, issue.field_path) for issue in validate_request(bad)}
    assert {
        ("duplicate_identity", "orders[1].node_id"),
        ("duplicate_identity", "orders[1].source_order_id"),
        ("duplicate_identity", "orders[1].source_resource_id"),
        ("duplicate_identity", "periods[1].period_id"),
        ("invalid_period_sequence", "periods[1].sequence"),
    } <= actual
    duplicate_sequence = request(periods=[PeriodInput("p0", 0), PeriodInput("p1", 0)])
    assert ("duplicate_identity", "periods[1].sequence") in {
        (issue.code, issue.field_path) for issue in validate_request(duplicate_sequence)
    }
    assert {issue.field_path for issue in validate_request(request(orders=[], periods=[]))} == {
        "orders",
        "periods",
    }


@pytest.mark.parametrize("field", ["product_line_code", "process_code", "scenario"])
def test_request_and_rule_set_identity_must_match_exactly(field):
    valid = request()
    for changed in (getattr(valid, field).lower() + " ", getattr(valid, field) + " "):
        spec = replace(valid.rule_set_spec, **{field: changed})
        issues = validate_request(replace(valid, rule_set_spec=spec))
        assert [(issue.code, issue.field_path) for issue in issues] == [
            ("rule_set_identity_mismatch", f"rule_set_spec.{field}")
        ]


def test_virtual_prototypes_and_actual_transition_have_distinct_input_roles():
    prototype = VirtualPrototypeInput(
        "prototype", Decimal("5"), None, None, None, None, "SPHC", {"family": "SPHC"}
    )
    valid = request(
        orders=[order(material_role=MaterialRole.ACTUAL_TRANSITION)], virtual_prototypes=[prototype]
    )
    assert validate_request(valid) == ()
    assert not hasattr(prototype, "node_id")
    bad = replace(valid, virtual_prototypes=[prototype, replace(prototype, unit_weight=Decimal(0))])
    assert {(issue.code, issue.field_path) for issue in validate_request(bad)} == {
        ("duplicate_identity", "virtual_prototypes[1].prototype_id"),
        ("non_positive_weight", "virtual_prototypes[1].unit_weight"),
    }


def test_rule_lookup_and_claimed_fingerprint_verification_are_reserved_for_loader():
    valid = request()
    assert valid.rule_set_spec.rules[0].rule_type == "registered_later"
    assert valid.rule_set_spec.fingerprint == "claimed-rules"
    assert validate_request(valid) == ()


def test_canonical_fingerprint_preserves_types_and_not_mapping_order():
    assert fingerprint({"a": Decimal("1.0"), "b": True}) == fingerprint(
        {"b": True, "a": Decimal("1.00")}
    )
    assert fingerprint(frozenset({"a", "b"})) == fingerprint(frozenset({"b", "a"}))
    assert len({fingerprint(value) for value in (Decimal("1"), "1", 1, True)}) == 4
    assert fingerprint(["a", "b"]) != fingerprint(["b", "a"])
    for value in (1.0, {1: "not_text"}, object()):
        with pytest.raises(ValueError):
            canonical_json(value)


def test_request_fingerprint_ignores_mapping_order_but_preserves_sequence_order():
    valid = request(
        orders=[
            order(rule_attributes={"a": 1, "b": Decimal("2")}),
            order(node_id="node-2", source_order_id="order-2", source_resource_id="resource-2"),
        ]
    )
    equivalent = replace(
        valid,
        orders=(
            replace(valid.orders[0], rule_attributes={"b": Decimal("2.0"), "a": 1}),
            valid.orders[1],
        ),
    )
    assert fingerprint_public_request(valid) == fingerprint_public_request(equivalent)
    assert fingerprint_public_request(valid) != fingerprint_public_request(
        replace(valid, orders=tuple(reversed(valid.orders)))
    )
    assert fingerprint_public_request(valid) != fingerprint_public_request(
        replace(valid, request_id="another-request")
    )
    assert fingerprint_public_request(valid) != fingerprint_public_request(
        replace(valid, rule_set_spec=replace(valid.rule_set_spec, fingerprint="another-claim"))
    )


def test_decimal_fingerprints_do_not_use_ambient_precision_and_normalize_zero():
    value = Decimal("123456789012345678901234567890.000001")
    expected = fingerprint_public_request(request(orders=[order(weight=value)]))
    with localcontext() as context:
        context.prec = 2
        assert fingerprint_public_request(request(orders=[order(weight=value)])) == expected
        assert fingerprint(Decimal("1.00")) == fingerprint(Decimal("1"))
    assert fingerprint(Decimal("-0.000")) == fingerprint(Decimal("0"))
    assert fingerprint(Decimal("1E+10000")) == fingerprint(Decimal("10E+9999"))


@pytest.mark.parametrize("text", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_typed_inputs_have_fingerprints_before_semantic_rejection(text):
    invalid = request(orders=[order(weight=Decimal(text))])
    first = fingerprint_public_request(invalid)
    assert len(first) == 64 and fingerprint_public_request(invalid) == first
    assert [(issue.code, issue.field_path) for issue in validate_request(invalid)] == [
        ("non_finite_number", "orders[0].weight")
    ]


def test_ordered_period_rule_and_quality_sequences_remain_in_request_identity():
    original = request()
    rules = original.rule_set_spec.rules
    criteria = original.rule_set_spec.quality_spec
    spec = replace(
        original.rule_set_spec,
        rules=rules + (replace(rules[0], rule_id="rule-2"),),
        quality_spec=criteria + (replace(criteria[0], criterion_id="quality-2"),),
    )
    original = replace(
        original, rule_set_spec=spec, periods=(PeriodInput("p0", 0), PeriodInput("p1", 1))
    )
    expected = fingerprint_public_request(original)
    variants = (
        replace(original, periods=tuple(reversed(original.periods))),
        replace(original, rule_set_spec=replace(spec, rules=tuple(reversed(spec.rules)))),
        replace(
            original, rule_set_spec=replace(spec, quality_spec=tuple(reversed(spec.quality_spec)))
        ),
    )
    assert all(fingerprint_public_request(variant) != expected for variant in variants)


def test_arbitrary_unicode_request_identity_is_lossless_and_utf8_serializable():
    texts = (chr(0xD800), chr(0xD801), "中文订单")
    hashes = []
    for text in texts:
        value = request(request_id=text)
        encoded = canonical_json(value)
        assert encoded.encode("utf-8").decode("utf-8") == encoded
        actual = fingerprint_public_request(value)
        assert actual == fingerprint_public_request(value)
        hashes.append(actual)
    assert len(set(hashes)) == len(texts)


def test_validation_collects_nonfinite_dimensions_and_shadowed_core_fields():
    prototype = VirtualPrototypeInput(
        "prototype", Decimal("5"), None, None, None, None, "SPHC", {"width": Decimal("1")}
    )
    invalid = request(
        orders=[
            order(
                width=Decimal("NaN"),
                thickness=Decimal("Infinity"),
                min_temperature=Decimal("sNaN"),
                max_temperature=Decimal("-Infinity"),
                rule_attributes={"weight": Decimal("3")},
            )
        ],
        virtual_prototypes=[prototype],
    )
    assert len(fingerprint_public_request(invalid)) == 64
    assert {(issue.code, issue.field_path) for issue in validate_request(invalid)} == {
        ("non_finite_number", "orders[0].width"),
        ("non_finite_number", "orders[0].thickness"),
        ("non_finite_number", "orders[0].min_temperature"),
        ("non_finite_number", "orders[0].max_temperature"),
        ("shadowed_core_field", "orders[0].rule_attributes.weight"),
        ("shadowed_core_field", "virtual_prototypes[0].rule_attributes.width"),
    }
