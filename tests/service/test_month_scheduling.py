import json
from dataclasses import fields, replace
from decimal import Decimal

import pytest

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    ControlledSplitMode,
    RuleScope,
    SolverPolicy,
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
from apsgo_scheduler.core.rules.base import RuleDisposition, RuleViolation
from apsgo_v7_service import month_scheduling
from apsgo_v7_service.month_scheduling import MonthSchedulingContractError
from apsgo_v7_service.rule_management import initialize_gqga4_rules
from apsgo_v7_service.scheduling import solve_gqga4_scheduling_task
from tests.service.grade_dictionary_support import sample_grade_dictionary

D = Decimal
REQUEST_ID = "00000000-0000-4000-8000-000000000901"


def policy():
    return SolverPolicy(
        590531,
        D("20"),
        D("2"),
        100,
        CONSTRUCTION_ORDER_KEY,
        NUMERIC_SEMANTICS_KEY,
        D("40"),
        2,
    )


def order(order_id="order-1", period="P0", **changes):
    value = {
        "source_order_id": order_id,
        "source_period": period,
        "is_virtual": False,
        "weight": D("600.125"),
        "grade": "DC01",
        "grade_class": "普通钢",
        "hot_roll_grade": "DC01",
        "width": D("1000.25"),
        "thickness": D("0.8"),
        "min_temperature": D("700"),
        "max_temperature": D("800"),
        "customer_grade": "战略客户",
        "customer_name": None,
        "execution_standard": None,
        "surface_grade": None,
    }
    return value | changes


def request_data(**changes):
    value = {
        "contract_version": month_scheduling.MONTH_SOLVE_CONTRACT_VERSION,
        "request_id": REQUEST_ID,
        "expected_active_version_id": 1,
        "periods": [
            {"period_id": "P1", "sequence": 1},
            {"period_id": "P0", "sequence": 0},
        ],
        "orders": [order(), order("order-2", weight=D("600"), width=D("990"))],
    }
    return value | changes


def parse(value=None):
    return month_scheduling.loads_month_solve_request(
        dumps_exact_json(request_data() if value is None else value),
        policy(),
    )


def issue_from(value):
    with pytest.raises(MonthSchedulingContractError) as caught:
        parse(value)
    assert len(caught.value.issues) == 1
    return caught.value.issues[0]


def test_request_parser_preserves_decimal_order_identity_and_derives_only_service_fields():
    value = request_data(
        orders=[
            order(
                customer_grade=None,
                hot_roll_grade=" sphc ",
                execution_standard=" Q/TB 305-2017 ",
                surface_grade="  ",
            ),
            order("order-2", period="P1", weight=D("1.2300")),
        ]
    )
    parsed = parse(value)

    assert parsed.expected_active_version_id == 1
    assert parsed.raw_request_fingerprint == fingerprint(value)
    assert parsed.typed_request_fingerprint == fingerprint(parsed.task_input)
    assert tuple(item.period_id for item in parsed.task_input.periods) == ("P0", "P1")
    assert tuple(item.source_order_id for item in parsed.task_input.orders) == (
        "order-1",
        "order-2",
    )
    assert parsed.task_input.orders[0].material_role is MaterialRole.ACTUAL_TRANSITION
    assert parsed.task_input.orders[1].material_role is MaterialRole.NORMAL_REAL
    assert parsed.task_input.orders[0].rule_attributes["surface_grade"] is None
    assert parsed.task_input.orders[0].rule_attributes["soft_hard_class"] is None
    assert parsed.task_input.orders[1].weight == D("1.2300")
    assert parsed.task_input.orders[0].node_id == parsed.task_input.orders[0].source_order_id
    assert parsed.task_input.orders[0].source_resource_id == "order-1"

    changed_version = parse(value | {"expected_active_version_id": 2})
    assert changed_version.raw_request_fingerprint != parsed.raw_request_fingerprint


@pytest.mark.parametrize(
    ("value", "code", "path"),
    [
        ({**request_data(), "extra": 1}, "unknown_field", "extra"),
        ({**request_data(), "orders": []}, "empty_collection", "orders"),
        (
            {**request_data(), "orders": [order(), order("order-1")]},
            "duplicate_identity",
            "orders[1].source_order_id",
        ),
        (
            {**request_data(), "orders": [order(is_virtual=True)]},
            "unsupported_input_virtual_order",
            "orders[0].is_virtual",
        ),
        (
            {**request_data(), "orders": [order(weight=True)]},
            "invalid_field_type",
            "orders[0].weight",
        ),
        (
            {**request_data(), "orders": [order(period="P9")]},
            "unknown_source_period",
            "orders[0].source_period",
        ),
        (
            {
                **request_data(),
                "periods": [
                    {"period_id": "P0", "sequence": 0},
                    {"period_id": "P1", "sequence": 2},
                ],
            },
            "invalid_period_sequence",
            "periods",
        ),
        (
            {**request_data(), "orders": [order(soft_hard_class="软钢")]},
            "unknown_field",
            "orders[0].soft_hard_class",
        ),
    ],
)
def test_request_parser_rejects_invalid_shapes_and_values(value, code, path):
    issue = issue_from(value)
    assert (issue.code, issue.field_path) == (code, path)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"\xff", "invalid_json"),
        ('{"contract_version":"a","contract_version":"b"}', "duplicate_json_key"),
        ('{"weight":NaN}', "invalid_json"),
    ],
)
def test_request_parser_rejects_invalid_json_without_float_coercion(payload, code):
    with pytest.raises(MonthSchedulingContractError) as caught:
        month_scheduling.loads_month_solve_request(payload, policy())
    assert caught.value.issues[0].code == code


def real_node(node_id, source_order_id, period, **changes):
    values = {
        "node_id": node_id,
        "source_order_id": source_order_id,
        "source_resource_id": source_order_id,
        "source_period": period,
        "weight": D("300"),
        "width": D("1000"),
        "thickness": D("0.8"),
        "min_temperature": D("700"),
        "max_temperature": D("800"),
        "grade": "DC01",
        "material_role": MaterialRole.NORMAL_REAL,
        "rule_attributes": {
            "grade_class": "普通钢",
            "hot_roll_grade": "DC01",
            "soft_hard_class": "软钢",
        },
    }
    return Node(**(values | changes))


def split_piece(index, weight):
    lineage = SplitLineage(
        partition_id="partition-1",
        parent_node_id="parent-node",
        parent_source_order_id="parent-order",
        source_resource_id="parent-order",
        source_period="P0",
        origin_assigned_period="P0",
        split_mode=ControlledSplitMode.SAME_PERIOD_SPLIT,
        target_assigned_period="P0",
        accepted_source_sequence=1,
        parent_weight=D("600"),
        piece_index=index,
        piece_count=2,
        authorization_rule_id="controlled_order_split",
        authorization_rule_version="1",
        authorization_decision_fingerprint="decision-fingerprint",
        reason_code="same_period_split",
    )
    return real_node(
        f"piece-{index}",
        "parent-order",
        "P0",
        weight=D(weight),
        split_lineage=lineage,
    )


def future_return_piece():
    lineage = SplitLineage(
        partition_id="partition-2",
        parent_node_id="future-parent-node",
        parent_source_order_id="future-return-order",
        source_resource_id="future-return-order",
        source_period="P1",
        origin_assigned_period="P0",
        split_mode=ControlledSplitMode.FUTURE_BORROW_RETURN,
        target_assigned_period="P1",
        accepted_source_sequence=2,
        parent_weight=D("600"),
        piece_index=1,
        piece_count=2,
        authorization_rule_id="controlled_order_split",
        authorization_rule_version="1",
        authorization_decision_fingerprint="future-decision-fingerprint",
        reason_code="future_borrow_return",
    )
    return real_node(
        "future-return-piece",
        "future-return-order",
        "P1",
        weight=D("300"),
        split_lineage=lineage,
    )


def violation(rule_id, scope, subject_id, reason_code, message, disposition):
    return RuleViolation(
        rule_id,
        scope,
        subject_id,
        reason_code,
        message,
        disposition,
        D("1"),
    )


def test_release_rows_preserve_plan_order_period_sequences_lineage_and_exact_warning_targets():
    first = split_piece(1, "300")
    virtual = Node(
        node_id="virtual-1",
        source_order_id=None,
        source_resource_id=None,
        source_period=None,
        weight=D("20"),
        width=D("990"),
        thickness=D("0.8"),
        min_temperature=D("700"),
        max_temperature=D("800"),
        grade="SPHC",
        material_role=MaterialRole.GENERATED_VIRTUAL,
        rule_attributes={"hot_roll_grade": "SPHC", "soft_hard_class": None},
        virtual_lineage=VirtualLineage(
            "virtual-prototype",
            VirtualPurpose.SPLIT_SEPARATOR,
            "partition-1",
            1,
        ),
    )
    second = split_piece(2, "300")
    future = real_node(
        "future-node",
        "future-order",
        "P1",
        material_role=MaterialRole.ACTUAL_TRANSITION,
    )
    returned = future_return_piece()
    plan = SchedulePlan(
        (
            Chain("chain-a", (first, virtual, second), "P0"),
            Chain("chain-b", (returned,), "P1"),
            Chain("chain-c", (future,), "P0"),
        )
    )
    violations = (
        violation(
            "width_transition",
            RuleScope.EDGE,
            "chain-a:piece-1>virtual-1",
            "width_transition_exceeded",
            "宽度告警一",
            RuleDisposition.PROHIBITED,
        ),
        violation(
            "width_transition",
            RuleScope.CHAIN,
            "chain-a:width_transition:virtual_anchor:0-2",
            "virtual_bridge_reverse_width_exceeded",
            "宽度告警二",
            RuleDisposition.PROHIBITED,
        ),
        violation(
            "temperature_overlap",
            RuleScope.EDGE,
            "chain-a:virtual-1>piece-2",
            "temperature",
            "温度告警",
            RuleDisposition.PROHIBITED,
        ),
        violation(
            "chain_weight_range",
            RuleScope.CHAIN,
            "chain-a",
            "chain_weight_below_minimum",
            "链重告警",
            RuleDisposition.ALLOWED_FINAL_DEVIATION,
        ),
    )

    rows = month_scheduling.build_month_solve_rows(plan, violations)

    assert tuple(row["node_id"] for row in rows) == (
        "piece-1",
        "virtual-1",
        "piece-2",
        "future-return-piece",
        "future-node",
    )
    assert tuple((row["assigned_period"], row["chain_sequence"]) for row in rows) == (
        ("P0", 1),
        ("P0", 1),
        ("P0", 1),
        ("P1", 1),
        ("P0", 2),
    )
    assert rows[0]["split_lineage"]["piece_index"] == 1
    assert rows[2]["split_lineage"]["piece_index"] == 2
    assert set(rows[0]["split_lineage"]) == {part.name for part in fields(SplitLineage)}
    assert rows[3]["split_lineage"]["split_mode"] == "future_borrow_return"
    assert rows[3]["split_lineage"]["origin_assigned_period"] == "P0"
    assert rows[3]["split_lineage"]["target_assigned_period"] == "P1"
    assert rows[3]["split_lineage"]["parent_weight"] == D("600")
    assert rows[1]["source_order_id"] is None
    assert rows[1]["source_resource_id"] is None
    assert rows[1]["source_period"] is None
    assert set(rows[1]["virtual_lineage"]) == {
        part.name for part in fields(VirtualLineage)
    }
    assert rows[1]["virtual_lineage"]["purpose"] == "split_separator"
    assert rows[0]["chain_warning"] == "链重告警"
    assert rows[1]["width_warning"] == "宽度告警一"
    assert rows[2]["width_warning"] == "宽度告警二"
    assert rows[2]["temperature_warning"] == "温度告警"
    assert rows[4]["source_period"] == "P1" and rows[4]["assigned_period"] == "P0"
    assert set(rows[0]) == {
        "node_id",
        "source_order_id",
        "source_resource_id",
        "source_period",
        "assigned_period",
        "chain_id",
        "chain_sequence",
        "node_sequence",
        "weight",
        "width",
        "thickness",
        "min_temperature",
        "max_temperature",
        "grade",
        "grade_class",
        "hot_roll_grade",
        "soft_hard_class",
        "material_role",
        "split_lineage",
        "virtual_lineage",
        "width_warning",
        "thickness_warning",
        "temperature_warning",
        "chain_warning",
        "business_warning",
    }

    unknown = violation(
        "width_transition",
        RuleScope.EDGE,
        "unknown-chain:missing>nodes",
        "width_transition_exceeded",
        "不能静默丢失的告警",
        RuleDisposition.PROHIBITED,
    )
    with pytest.raises(month_scheduling.MonthSchedulingMappingError, match="not present"):
        month_scheduling.build_month_solve_rows(plan, (unknown,))


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "rules.sqlite3"
    initialize_gqga4_rules(path, initial_grade_dictionary=sample_grade_dictionary())
    return path


def test_bound_publishable_result_serializes_complete_identity_quality_and_rows(database_path):
    parsed = parse()
    bound = solve_gqga4_scheduling_task(parsed.task_input, database_path)

    data = month_scheduling.month_solve_response_data(parsed, bound)
    encoded = month_scheduling.dumps_month_solve_response(parsed, bound)
    decoded = json.loads(encoded, parse_float=Decimal)

    assert data["publishable"] is True
    assert data["status"] == "success"
    assert data["raw_request_fingerprint"] == parsed.raw_request_fingerprint
    assert data["typed_request_fingerprint"] == parsed.typed_request_fingerprint
    assert data["request_fingerprint"] == bound.request_fingerprint
    assert data["rule_set_version"] == bound.rule_set_version
    assert [item["criterion_id"] for item in data["quality"]] == [
        item.criterion_id for item in bound.quality_spec
    ]
    assert len(data["rows"]) == 2
    assert {row["assigned_period"] for row in data["rows"]} == {"P0"}
    assert data["violations"] == []
    assert data["audit_summary"]["passed"] is True
    assert data["audit_summary"]["integrity_passed"] is True
    assert data["audit_summary"]["writeback_blocking_violation_count"] == 0
    assert data["business_rules_satisfied"] is True
    assert all(
        row[name] is None
        for row in data["rows"]
        for name in (
            "width_warning",
            "thickness_warning",
            "temperature_warning",
            "chain_warning",
        )
    )
    assert decoded["rows"] == data["rows"]
    assert decoded["run_manifest"]["counters"] == dict(
        data["run_manifest"]["counters"]
    )
    assert decoded["audit_summary"]["passed"] is True


def test_allowed_underweight_is_authoritative_and_projected_only_to_chain_head(database_path):
    value = request_data(orders=[order(weight=D("600"))])
    parsed = parse(value)
    bound = solve_gqga4_scheduling_task(parsed.task_input, database_path)

    data = month_scheduling.month_solve_response_data(parsed, bound)

    assert data["status"] == "publishable_with_allowed_deviation"
    assert data["publishable"] is True
    assert [item["reason_code"] for item in data["violations"]] == [
        "chain_weight_below_minimum"
    ]
    assert data["rows"][0]["chain_warning"] == data["violations"][0]["message"]
    assert len(data["violations"]) == 1
    assert data["business_rules_satisfied"] is False


def test_input_invalid_result_keeps_diagnostics_but_never_rows_or_diagnostic_violations(database_path):
    value = request_data(orders=[order(width=None)])
    parsed = parse(value)
    bound = solve_gqga4_scheduling_task(parsed.task_input, database_path)

    data = month_scheduling.month_solve_response_data(parsed, bound)

    assert (data["status"], data["stop_reason"], data["publishable"]) == (
        "failed",
        "input_invalid",
        False,
    )
    assert data["rows"] == []
    assert data["violations"] == []
    assert data["quality"] == [] and data["metrics"] == {}
    assert data["issues"]
    assert data["business_rules_satisfied"] is None
    assert data["audit_summary"]["writeback_blocking_violation_count"] is None
    assert data["audit_summary"]["integrity_passed"] is False


def test_response_rejects_request_identity_or_quality_spec_mismatch(database_path):
    parsed = parse()
    bound = solve_gqga4_scheduling_task(parsed.task_input, database_path)

    with pytest.raises(month_scheduling.MonthSchedulingMappingError, match="does not match"):
        month_scheduling.month_solve_response_data(
            replace(parsed, expected_active_version_id=2),
            bound,
        )
    different_orders = parse(request_data(orders=[order(weight=D("700"))]))
    with pytest.raises(month_scheduling.MonthSchedulingMappingError, match="does not match"):
        month_scheduling.month_solve_response_data(different_orders, bound)
    with pytest.raises(ValueError, match="binding identity"):
        replace(bound, quality_spec=tuple(reversed(bound.quality_spec)))


def test_error_envelope_has_fixed_fields_and_exact_diagnostics():
    issue = issue_from({**request_data(), "orders": []})
    data = json.loads(
        month_scheduling.dumps_month_solve_error(
            "invalid_request",
            "请求格式不正确。",
            request_id=REQUEST_ID,
            expected_active_version_id=1,
            issues=(issue,),
        )
    )["error"]

    assert set(data) == {
        "code",
        "message",
        "request_id",
        "expected_active_version_id",
        "current_active_version_id",
        "issues",
    }
    assert data["request_id"] == REQUEST_ID
    assert data["issues"][0]["field_path"] == "orders"
