"""Typed input normalization and portable GQGA4 mapping, without running a solver."""

import csv
import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, Inexact, localcontext
from pathlib import Path

import pytest

from apsgo_scheduler.api.request import (
    OrderInput,
    PeriodInput,
    QualityCriterionSpec,
    RuleDefinitionSpec,
    RuleSetSpec,
    SchedulingRequest,
    VirtualPrototypeInput,
    fingerprint_public_request,
)
from apsgo_scheduler.app.input_normalizer import InputNormalizationError, normalize_input
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec, load_rule_set
from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
    SolverPolicy,
    sum_weights,
)
from apsgo_scheduler.core.model import Chain, MaterialRole
from apsgo_scheduler.core.rules.base import ChainRuleSubject, RuleEvaluationContext

D = Decimal
BASE = Path(__file__).resolve().parents[1] / "baselines/gqga4"


def make_order(index=0, **changes):
    return OrderInput(
        **(
            dict(
                node_id=f"node-{index}",
                source_order_id=f"order-{index}",
                source_resource_id=f"resource-{index}",
                source_period=f"P{index % 2}",
                weight=D("10"),
                width=D("1000"),
                thickness=D("1"),
                min_temperature=D("700"),
                max_temperature=D("800"),
                grade="DC01",
                material_role=MaterialRole.NORMAL_REAL,
                rule_attributes={
                    "surface_grade": "FC",
                    "grade_class": "IF钢",
                    "hot_roll_grade": "SPHC",
                    "soft_hard_class": "soft",
                    "customer_name": None,
                },
            )
            | changes
        )
    )


def make_prototype(index=0, **changes):
    return VirtualPrototypeInput(
        **(
            dict(
                prototype_id=f"prototype-{index}",
                unit_weight=D("5"),
                width=D("1000"),
                thickness=D("1"),
                min_temperature=None,
                max_temperature=None,
                grade="SPHC",
                rule_attributes={},
            )
            | changes
        )
    )


def make_spec(**changes):
    rule = RuleDefinitionSpec(
        "width",
        "SyntheticWidthLimitRule",
        "合成宽度规则",
        RuleScope.EDGE,
        True,
        "1",
        {"maximum_increase": D("50")},
    )
    criteria = tuple(
        QualityCriterionSpec(key, key, "minimize", "named_value", "exact_decimal")
        for key in ("prohibited_violation_count", "prohibited_violation_severity", "chain_count")
    )
    value = RuleSetSpec("LINE", "PROCESS", "month", "1", (rule,), criteria, frozenset(), "pending")
    value = replace(value, **changes)
    return replace(value, fingerprint=fingerprint_rule_set_spec(value))


def make_request(**changes):
    spec = changes.get("rule_set_spec", make_spec())
    policy = SolverPolicy(
        7, D("12"), D("2"), 0, CONSTRUCTION_ORDER_KEY, NUMERIC_SEMANTICS_KEY, D("0")
    )
    return SchedulingRequest(
        **(
            dict(
                contract_version="1",
                request_id="test-request",
                product_line_code=spec.product_line_code,
                process_code=spec.process_code,
                scenario=spec.scenario,
                orders=(make_order(), make_order(1)),
                periods=(PeriodInput("P0", 0), PeriodInput("P1", 1)),
                virtual_prototypes=(make_prototype(), make_prototype(1)),
                rule_set_spec=spec,
                policy=policy,
            )
            | changes
        )
    )


def normalize(request):
    return normalize_input(request, load_rule_set(request.rule_set_spec))


def get_issues(request, rules=None):
    if rules is None:
        rules = load_rule_set(request.rule_set_spec)
    with pytest.raises(InputNormalizationError) as caught:
        normalize_input(request, rules)
    issues = caught.value.issues
    assert issues and isinstance(issues, tuple)
    assert all(item.severity is DiagnosticSeverity.ERROR and item.message for item in issues)
    assert list(issues) == sorted(issues, key=lambda item: tuple(DiagnosticPhase).index(item.phase))
    return issues


def read_gqga4_spec(filename):
    raw = json.loads((BASE / filename).read_text(encoding="utf-8"), parse_float=D)
    return RuleSetSpec(
        **(
            raw
            | {
                "rules": tuple(
                    RuleDefinitionSpec(**(item | {"scope": RuleScope(item["scope"])}))
                    for item in raw["rules"]
                ),
                "quality_spec": tuple(QualityCriterionSpec(**item) for item in raw["quality_spec"]),
            }
        )
    )


@pytest.fixture(scope="module")
def gqga4_spec():
    return read_gqga4_spec("gqga4_rule_set_spec.json")


@pytest.fixture(scope="module")
def gqga4_six_level_spec():
    return read_gqga4_spec("gqga4_rule_set_spec_six_level_historical.json")


def test_normalizes_identifiers_and_text_without_mutating_request_or_scalar_attributes():
    original = make_order(
        node_id=" node-0 ",
        source_order_id=" order-0 ",
        source_resource_id=" resource-0 ",
        source_period=" P0 ",
        grade=" dc01 ",
        rule_attributes={
            "surface_grade": " fc ",
            "hot_roll_grade": " sphc ",
            "grade_class": " IF钢 ",
            "soft_hard_class": " Soft ",
            "customer_name": " 宝马 ",
            "customer_grade": " 战略客户 ",
            "execution_standard": " Q/TB 305-2017 ",
            "extra_int": 0,
            "extra_bool": False,
            "extra_text": " keep ",
            "extra_decimal": D("1.2300"),
        },
    )
    request = make_request(
        orders=(original, make_order(1)),
        periods=(PeriodInput(" P1 ", 1), PeriodInput(" P0 ", 0)),
        virtual_prototypes=(make_prototype(prototype_id=" prototype-0 ", grade=" sphc "),),
    )
    before = fingerprint_public_request(request)
    problem = normalize(request)
    node = problem.nodes[0]
    assert (
        node.node_id,
        node.source_order_id,
        node.source_resource_id,
        node.source_period,
        node.grade,
    ) == ("node-0", "order-0", "resource-0", "P0", "DC01")
    assert problem.period_order == ("P0", "P1")
    assert problem.virtual_prototypes[0].prototype_id == "prototype-0"
    assert problem.virtual_prototypes[0].grade == "SPHC"
    assert dict(node.rule_attributes) == {
        "surface_grade": "FC",
        "hot_roll_grade": "SPHC",
        "grade_class": "IF钢",
        "soft_hard_class": "Soft",
        "customer_name": "宝马",
        "customer_grade": "战略客户",
        "execution_standard": "Q/TB 305-2017",
        "extra_int": 0,
        "extra_bool": False,
        "extra_text": " keep ",
        "extra_decimal": D("1.2300"),
    }
    assert fingerprint_public_request(request) == before
    with pytest.raises(TypeError):
        node.rule_attributes["surface_grade"] = "FD"
    with pytest.raises(FrozenInstanceError):
        node.weight = D("20")


@pytest.mark.parametrize("field", ("node_id", "source_order_id", "source_resource_id"))
def test_identity_collisions_are_checked_after_stripping(field):
    first, second = make_order(), make_order(1)
    second = replace(second, **{field: f" {getattr(first, field)} "})
    issues = get_issues(make_request(orders=(first, second)))
    assert ("duplicate_identity", f"orders[1].{field}") in {
        (item.code, item.field_path) for item in issues
    }


@pytest.mark.parametrize(
    "collection,values,path",
    [
        ("periods", (PeriodInput("P0", 0), PeriodInput(" P0 ", 1)), "periods[1].period_id"),
        (
            "virtual_prototypes",
            (make_prototype(), make_prototype(1, prototype_id=" prototype-0 ")),
            "virtual_prototypes[1].prototype_id",
        ),
    ],
)
def test_period_and_prototype_collisions_are_not_hidden_by_raw_whitespace(collection, values, path):
    assert ("duplicate_identity", path) in {
        (item.code, item.field_path) for item in get_issues(make_request(**{collection: values}))
    }


@pytest.mark.parametrize("count", (1, 2, 5))
def test_period_sequence_is_authoritative_and_material_sequences_remain_in_input_order(count):
    periods = tuple(PeriodInput(f"Z{count - i}", i) for i in range(count))
    request = make_request(
        orders=(
            make_order(1, source_period=periods[0].period_id),
            make_order(0, source_period=periods[-1].period_id),
        ),
        periods=tuple(reversed(periods)),
        virtual_prototypes=(make_prototype(1), make_prototype(0)),
    )
    problem = normalize(request)
    assert problem.period_order == tuple(item.period_id for item in periods)
    assert tuple(item.node_id for item in problem.nodes) == ("node-1", "node-0")
    assert tuple(item.prototype_id for item in problem.virtual_prototypes) == (
        "prototype-1",
        "prototype-0",
    )


@pytest.mark.parametrize("field", ("product_line_code", "process_code", "scenario"))
def test_normalization_does_not_trim_away_request_rule_identity_mismatch(field):
    request = make_request()
    request = replace(request, **{field: f" {getattr(request, field)} "})
    assert ("rule_set_identity_mismatch", f"rule_set_spec.{field}") in {
        (item.code, item.field_path) for item in get_issues(request)
    }


@pytest.mark.parametrize("collection", ("orders", "periods"))
def test_normalizer_rejects_empty_required_collections(collection):
    issues = get_issues(make_request(**{collection: ()}))
    assert ("empty_collection", collection) in {(item.code, item.field_path) for item in issues}


def test_normalizer_rejects_generated_virtual_material_as_an_input_order():
    request = make_request(orders=(make_order(material_role=MaterialRole.GENERATED_VIRTUAL),))
    issues = get_issues(request)
    assert [(item.code, item.field_path) for item in issues] == [
        ("unsupported_input_material_role", "orders[0].material_role")
    ]


@pytest.mark.parametrize("role", (MaterialRole.NORMAL_REAL, MaterialRole.ACTUAL_TRANSITION))
@pytest.mark.parametrize(
    "field",
    (
        "width",
        "thickness",
        "min_temperature",
        "max_temperature",
        "grade",
        "grade_class",
        "hot_roll_grade",
    ),
)
def test_enabled_rules_require_applicable_fields_on_every_real_input(gqga4_spec, role, field):
    order = make_order(material_role=role)
    if field in order.rule_attributes:
        order = replace(order, rule_attributes=dict(order.rule_attributes) | {field: "  "})
        path = f"orders[0].rule_attributes.{field}"
    else:
        order = replace(order, **{field: " " if field == "grade" else None})
        path = f"orders[0].{field}"
    issues = get_issues(make_request(orders=(order,), rule_set_spec=gqga4_spec))
    matches = [
        item for item in issues if item.code == "missing_required_field" and item.field_path == path
    ]
    assert len(matches) == 1
    assert matches[0].phase is DiagnosticPhase.INPUT_NORMALIZATION


@pytest.mark.parametrize("field", ("width", "thickness"))
def test_virtual_prototypes_require_enabled_connection_dimensions(gqga4_spec, field):
    request = make_request(
        rule_set_spec=gqga4_spec, virtual_prototypes=(make_prototype(**{field: None}),)
    )
    assert ("missing_required_field", f"virtual_prototypes[0].{field}") in {
        (item.code, item.field_path) for item in get_issues(request)
    }


def test_virtual_prototypes_do_not_require_real_only_attributes_or_temperature(gqga4_spec):
    request = make_request(
        rule_set_spec=gqga4_spec, virtual_prototypes=(make_prototype(grade="", rule_attributes={}),)
    )
    (prototype,) = normalize(request).virtual_prototypes
    assert prototype.min_temperature is None and prototype.max_temperature is None
    assert prototype.grade == "" and not prototype.rule_attributes


@pytest.mark.parametrize("role", (MaterialRole.NORMAL_REAL, MaterialRole.ACTUAL_TRANSITION))
@pytest.mark.parametrize("surface", (None, "", " \t ", " fc "))
def test_surface_grade_is_optional_and_only_present_text_is_normalized(gqga4_spec, role, surface):
    attributes = dict(make_order().rule_attributes) | {"surface_grade": surface}
    order = make_order(material_role=role, rule_attributes=attributes)
    node = normalize(make_request(rule_set_spec=gqga4_spec, orders=(order,))).nodes[0]
    expected = None if surface is None else surface.strip().upper()
    assert node.rule_attributes["surface_grade"] == expected
    absent = replace(
        order,
        rule_attributes={key: value for key, value in attributes.items() if key != "surface_grade"},
    )
    assert (
        "surface_grade"
        not in normalize(make_request(rule_set_spec=gqga4_spec, orders=(absent,)))
        .nodes[0]
        .rule_attributes
    )


@pytest.mark.parametrize("optional", (None, "", " \t "))
def test_optional_soft_class_and_customer_name_remain_optional(gqga4_spec, optional):
    order = make_order(
        rule_attributes=dict(make_order().rule_attributes)
        | {"soft_hard_class": optional, "customer_name": optional}
    )
    node = normalize(make_request(orders=(order,), rule_set_spec=gqga4_spec)).nodes[0]
    expected = None if optional is None else ""
    assert (
        node.rule_attributes["soft_hard_class"] == node.rule_attributes["customer_name"] == expected
    )


@pytest.mark.parametrize(
    "field",
    (
        "surface_grade",
        "grade_class",
        "hot_roll_grade",
        "soft_hard_class",
        "customer_name",
        "execution_standard",
    ),
)
def test_known_text_attributes_are_not_silently_coerced(field):
    order = make_order(rule_attributes={field: 3})
    assert ("invalid_text_attribute", f"orders[0].rule_attributes.{field}") in {
        (item.code, item.field_path) for item in get_issues(make_request(orders=(order,)))
    }


def test_disabled_rules_do_not_reintroduce_required_fields():
    spec = make_spec()
    spec = make_spec(rules=(replace(spec.rules[0], enabled=False),))
    problem = normalize(
        make_request(
            rule_set_spec=spec,
            orders=(
                make_order(
                    width=None,
                    thickness=None,
                    min_temperature=None,
                    max_temperature=None,
                    grade="",
                    rule_attributes={},
                ),
            ),
            virtual_prototypes=(make_prototype(width=None, thickness=None),),
        )
    )
    assert problem.nodes[0].width is None and problem.virtual_prototypes[0].width is None


@pytest.mark.parametrize("value", (0, -1))
def test_custom_required_scalar_keeps_its_type_and_is_not_treated_as_missing(value):
    rule = RuleDefinitionSpec(
        "priority",
        "SyntheticNodePriorityRule",
        "合成优先级",
        RuleScope.NODE,
        True,
        "1",
        {"attribute": "priority"},
    )
    request = make_request(
        rule_set_spec=make_spec(rules=(rule,)),
        orders=(make_order(rule_attributes={"priority": value}),),
    )
    actual = normalize(request).nodes[0].rule_attributes["priority"]
    assert actual == value and type(actual) is type(value)


@pytest.mark.parametrize("value", (False, "0", D("0")))
def test_invalid_construction_priority_is_reported_for_every_node(value):
    rule = RuleDefinitionSpec(
        "priority",
        "SyntheticNodePriorityRule",
        "合成优先级",
        RuleScope.NODE,
        True,
        "1",
        {"attribute": "priority"},
    )
    request = make_request(
        rule_set_spec=make_spec(rules=(rule,)),
        orders=tuple(make_order(index, rule_attributes={"priority": value}) for index in range(2)),
    )
    issues = get_issues(request)
    assert [(item.code, item.field_path, item.subject_id) for item in issues] == [
        ("invalid_construction_priority", "orders[0].rule_attributes", "node-0"),
        ("invalid_construction_priority", "orders[1].rule_attributes", "node-1"),
    ]


@pytest.mark.parametrize(
    "case", ("missing_width", "invalid_weight", "same_node", "missing_priority")
)
def test_other_input_errors_do_not_hide_safe_priority_diagnostics_or_duplicate_missing_fields(case):
    priority = RuleDefinitionSpec(
        "priority",
        "SyntheticNodePriorityRule",
        "合成优先级",
        RuleScope.NODE,
        True,
        "1",
        {"attribute": "priority"},
    )
    specification = make_spec(rules=make_spec().rules + (priority,))
    first = make_order(rule_attributes={"priority": 0})
    second = make_order(1, rule_attributes={"priority": True})
    expected = [("invalid_construction_priority", "orders[1].rule_attributes")]
    if case == "missing_width":
        first = replace(first, width=None)
        expected.insert(0, ("missing_required_field", "orders[0].width"))
    elif case == "invalid_weight":
        first = replace(first, weight=D("0"))
        expected.insert(0, ("non_positive_weight", "orders[0].weight"))
    elif case == "same_node":
        first = replace(first, width=None, rule_attributes={"priority": True})
        second = replace(second, rule_attributes={"priority": 0})
        expected = [
            ("missing_required_field", "orders[0].width"),
            ("invalid_construction_priority", "orders[0].rule_attributes"),
        ]
    else:
        first = replace(first, rule_attributes={})
        second = replace(second, rule_attributes={"priority": 0})
        expected = [("missing_required_field", "orders[0].rule_attributes.priority")]
    request = make_request(rule_set_spec=specification, orders=(first, second))
    issues = get_issues(request)
    assert [(item.code, item.field_path) for item in issues] == expected
    assert get_issues(request) == issues


@pytest.mark.parametrize("collection", ("orders", "virtual_prototypes"))
@pytest.mark.parametrize("field", ("width", "thickness", "min_temperature", "max_temperature"))
def test_finite_decimal_with_infinite_physical_float_projection_is_rejected(collection, field):
    factory = make_order if collection == "orders" else make_prototype
    value = D("-1E10000") if field == "min_temperature" else D("1E10000")
    request = make_request(**{collection: (factory(**{field: value}),)})
    assert ("non_finite_float_projection", f"{collection}[0].{field}") in {
        (item.code, item.field_path) for item in get_issues(request)
    }


def test_large_weights_are_not_rejected_by_an_unneeded_float_projection():
    request = make_request(
        orders=(make_order(weight=D("1E10000")),),
        virtual_prototypes=(make_prototype(unit_weight=D("1E10000")),),
    )
    problem = normalize(request)
    assert problem.nodes[0].weight == problem.virtual_prototypes[0].unit_weight == D("1E10000")


@pytest.mark.parametrize(
    "weight,invalid", (("2000", False), ("2000.000001", False), ("2000.0000011", True))
)
def test_atomic_chain_maximum_uses_exact_epsilon_under_low_decimal_precision(
    gqga4_spec, weight, invalid
):
    request = make_request(rule_set_spec=gqga4_spec, orders=(make_order(weight=D(weight)),))
    with localcontext() as arithmetic:
        arithmetic.prec = 3
        arithmetic.traps[Inexact] = True
        if invalid:
            issues = get_issues(request)
            assert [(item.code, item.field_path, item.phase) for item in issues] == [
                (
                    "atomic_node_above_chain_maximum",
                    "orders[0].weight",
                    DiagnosticPhase.INPUT_NORMALIZATION,
                )
            ]
        else:
            assert normalize(request).nodes[0].weight == D(weight)


def test_independent_validation_and_normalization_failures_are_aggregated_stably(gqga4_spec):
    request = make_request(
        request_id="",
        rule_set_spec=gqga4_spec,
        orders=(
            make_order(
                weight=D("-1"),
                width=None,
                source_period="unknown",
                rule_attributes={"surface_grade": 3},
            ),
            make_order(1, max_temperature=D("1E10000")),
        ),
    )
    issues = get_issues(request)
    actual = {(item.code, item.field_path) for item in issues}
    assert {
        ("empty_identity", "request_id"),
        ("non_positive_weight", "orders[0].weight"),
        ("unknown_source_period", "orders[0].source_period"),
        ("missing_required_field", "orders[0].width"),
        ("invalid_text_attribute", "orders[0].rule_attributes.surface_grade"),
        ("missing_required_field", "orders[0].rule_attributes.grade_class"),
        ("non_finite_float_projection", "orders[1].max_temperature"),
    } <= actual
    assert get_issues(request) == issues


@pytest.mark.parametrize(
    "change", ("loaded_content", "claimed_fingerprint", "unknown_rule", "invalid_loaded_type")
)
def test_rules_are_reverified_and_bound_to_the_loaded_content(change):
    request = make_request()
    rules = load_rule_set(request.rule_set_spec)
    if change == "loaded_content":
        rules = replace(
            rules, rules=(replace(rules.rules[0], parameters={"maximum_increase": D("51")}),)
        )
        code = "rule_set_content_mismatch"
    elif change == "claimed_fingerprint":
        request = replace(
            request, rule_set_spec=replace(request.rule_set_spec, fingerprint="false-claim")
        )
        code = "rule_set_fingerprint_mismatch"
    elif change == "unknown_rule":
        request = replace(
            request,
            rule_set_spec=make_spec(
                rules=(replace(request.rule_set_spec.rules[0], rule_type="UnknownRule"),)
            ),
        )
        code = "unknown_enabled_rule"
    else:
        rules, code = object(), "invalid_rule_set_type"
    request = replace(request, orders=(make_order(weight=D("0")),))
    issues = get_issues(request, rules)
    assert code in {item.code for item in issues}
    assert "non_positive_weight" in {item.code for item in issues}


@pytest.mark.parametrize("explicit_none", (False, True))
def test_omitted_loaded_rules_use_the_independently_verified_configuration(explicit_none):
    request = make_request()
    actual = normalize_input(request, None) if explicit_none else normalize_input(request)
    assert actual == normalize(request)


def test_failed_rule_loading_still_collects_normalized_input_errors_without_dummy_rules():
    specification = make_spec()
    specification = make_spec(rules=(replace(specification.rules[0], rule_type="UnknownRule"),))
    request = make_request(
        rule_set_spec=specification,
        orders=(make_order(source_period=" P0 ", weight=D("0")),),
        periods=(PeriodInput(" P0 ", 0),),
    )
    with pytest.raises(InputNormalizationError) as caught:
        normalize_input(request)
    issues = caught.value.issues
    assert {item.code for item in issues} == {"non_positive_weight", "unknown_enabled_rule"}
    assert [item.phase for item in issues] == [
        DiagnosticPhase.REQUEST_VALIDATION,
        DiagnosticPhase.RULE_LOADING,
    ]
    assert len(issues) == 2 and all(item.field_path for item in issues)


def test_normalizer_does_not_split_atoms_or_reclassify_explicit_roles(gqga4_spec):
    request = make_request(
        rule_set_spec=gqga4_spec,
        orders=(
            make_order(weight=D("570.30")),
            make_order(1, material_role=MaterialRole.ACTUAL_TRANSITION),
        ),
    )
    problem = normalize(request)
    assert len(problem.nodes) == 2
    assert problem.nodes[0].weight == D("570.30")
    assert problem.nodes[1].material_role is MaterialRole.ACTUAL_TRANSITION
    assert all(
        node.virtual_lineage is None and node.split_lineage is None for node in problem.nodes
    )


def decode_fixture(value):
    """Decode only the frozen fixture's value tags; never load historical Python classes."""
    if isinstance(value, list):
        return tuple(decode_fixture(item) for item in value)
    if not isinstance(value, dict):
        return value
    kind = value["$type"]
    if kind == "dataclass":
        return {key: decode_fixture(item) for key, item in value["fields"]}
    if kind == "tuple":
        return tuple(decode_fixture(item) for item in value["items"])
    if kind == "decimal":
        return D(value["value"])
    if kind in ("enum", "datetime"):
        return value["value"]
    raise AssertionError(f"unsupported frozen fixture tag: {kind}")


def gqga4_material_role(customer_grade, hot_roll_grade, execution_standard):
    actual = (
        (customer_grade or "").strip() != "战略客户"
        and (hot_roll_grade or "").strip().upper() == "SPHC"
        and (execution_standard or "").strip() == "Q/TB 305-2017"
    )
    return MaterialRole.ACTUAL_TRANSITION if actual else MaterialRole.NORMAL_REAL


@pytest.fixture(scope="module")
def gqga4_request(gqga4_spec):
    raw = json.loads((BASE / "inputs/optimization_problem.json").read_text())
    source = decode_fixture(raw["optimization_problem"])
    orders = []
    for record in source["nodes"]:
        physical, classification = record["physical_spec"], record["classifications"]
        features = {item["key"]: item["value"] for item in record["rule_features"]}
        role = gqga4_material_role(
            features.get("customer_grade"),
            classification["hot_rolled_grade"],
            features.get("execution_standard"),
        )
        assert role.value == record["material_role"]
        (weight,) = (
            item["value"] for item in record["measures"]["values"] if item["metric_key"] == "weight"
        )
        orders.append(
            OrderInput(
                node_id=record["node_id"]["value"],
                source_order_id=record["source_resource_id"]["value"],
                source_resource_id=record["source_resource_id"]["value"],
                source_period=record["planning_period_id"]["value"],
                weight=weight,
                width=physical["width"]["millimeters"],
                thickness=physical["thickness"]["millimeters"],
                min_temperature=physical["temperature_range"]["lower"],
                max_temperature=physical["temperature_range"]["upper"],
                grade=features["grade"],
                material_role=role,
                rule_attributes={
                    "surface_grade": features.get("surface_grade"),
                    "grade_class": classification["grade_class"],
                    "hot_roll_grade": classification["hot_rolled_grade"],
                    "soft_hard_class": classification["soft_or_hard"],
                    "customer_name": features.get("customer_name"),
                    "customer_grade": features.get("customer_grade"),
                    "execution_standard": features.get("execution_standard"),
                },
            )
        )
    prototypes = tuple(
        VirtualPrototypeInput(
            prototype_id=item["prototype_id"]["value"],
            unit_weight=item["unit_weight"]["tons"],
            width=item["rule_spec_key"]["width"]["millimeters"],
            thickness=item["rule_spec_key"]["thickness"]["millimeters"],
            min_temperature=None,
            max_temperature=None,
            grade=item["display_hot_rolled_grade"],
            rule_attributes={},
        )
        for item in source["virtual_catalog"]["prototypes"]
    )
    periods = json.loads((BASE / "inputs/solver_config.json").read_text())["period_order"]
    policy = SolverPolicy(
        **json.loads((BASE / "gqga4_solver_policy.json").read_text(), parse_float=D)
    )
    return make_request(
        rule_set_spec=gqga4_spec,
        policy=policy,
        orders=tuple(orders),
        virtual_prototypes=prototypes,
        periods=tuple(PeriodInput(value, index) for index, value in enumerate(periods)),
    )


def test_frozen_gqga4_531_orders_and_27_prototypes_normalize_without_search(gqga4_request):
    before = fingerprint_public_request(gqga4_request)
    problem = normalize(gqga4_request)
    assert len(problem.nodes) == 531 and len(problem.virtual_prototypes) == 27
    assert (problem.nodes[0].node_id, problem.nodes[-1].node_id) == (
        "0030120727-000030",
        "0030124871-000080",
    )
    assert (
        problem.virtual_prototypes[0].prototype_id,
        problem.virtual_prototypes[-1].prototype_id,
    ) == ("virtual_sphc:1000x0.4", "virtual_sphc:1500x2.5")
    assert sum_weights(item.weight for item in problem.nodes) == D("29333.91")
    assert tuple(node.node_id for node in problem.nodes) == tuple(
        item.node_id for item in gqga4_request.orders
    )
    assert tuple(item.prototype_id for item in problem.virtual_prototypes) == tuple(
        item.prototype_id for item in gqga4_request.virtual_prototypes
    )
    assert problem.period_order == ("BR_00000001", "BR_00000002", "BR_00000003", "BR_00000006")
    assert sum(item.material_role is MaterialRole.ACTUAL_TRANSITION for item in problem.nodes) == 61
    assert all(
        item.split_lineage is None and item.virtual_lineage is None for item in problem.nodes
    )
    with (BASE / "inputs/input_orders.csv").open(encoding="utf-8", newline="") as stream:
        rows = {row["source_order_id"]: row for row in csv.DictReader(stream)}
    assert len(rows) == len(problem.nodes)
    for node in problem.nodes:
        row = rows[node.source_order_id]
        assert node.weight == D(row["连镀欠交"])
        assert node.source_period == row["source_period"].strip()
        assert node.material_role is gqga4_material_role(
            row["客户等级"], row["热轧牌号"], row["执行标准"]
        )
    assert fingerprint_public_request(gqga4_request) == before


@pytest.mark.parametrize(
    "customer,hot,standard,actual",
    (
        ("", "SPHC", "Q/TB 305-2017", True),
        ("战略客户", "SPHC", "Q/TB 305-2017", False),
        ("", "SPHETi-3", "Q/TB 305-2017", False),
        ("", "SPHC", "another-standard", False),
    ),
)
def test_gqga4_mapping_requires_all_three_conditions_and_role_breaks_real_runs(
    gqga4_spec, customer, hot, standard, actual
):
    role = gqga4_material_role(customer, hot, standard)
    assert (role is MaterialRole.ACTUAL_TRANSITION) is actual
    orders = tuple(
        make_order(
            index,
            weight=D("300"),
            source_period="P0",
            material_role=role if index == 1 else MaterialRole.NORMAL_REAL,
        )
        for index in range(3)
    )
    problem = normalize(make_request(rule_set_spec=gqga4_spec, orders=orders))
    subject = ChainRuleSubject("mapped-chain", Chain("chain", problem.nodes, "P0"))
    context = RuleEvaluationContext(
        problem.period_order,
        {period: index for index, period in enumerate(problem.period_order)},
        (),
    )
    narrow = next(
        item
        for item in load_rule_set(gqga4_spec).rules
        if item.rule_id == "chain_if_narrow_real_weight_lte"
    )
    result = narrow.evaluate(subject, context)
    assert bool(result.violations) is not actual
