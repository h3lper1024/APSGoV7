from dataclasses import replace
from decimal import Decimal
import json
import sqlite3

import pytest

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.rule_management import EditableRuleInput, SetActiveRulesRequest
from apsgo_scheduler.core.contracts import INTEGER_NUMERIC_SEMANTICS_KEY
from apsgo_v7_service.enable_delivery_rules import enable_delivery_rules
from apsgo_v7_service.month_scheduling import loads_month_solve_request, dumps_month_solve_response, MonthSchedulingContractError
from apsgo_v7_service.rule_management import initialize_gqga4_rules, get_active_gqga4_rules, set_active_gqga4_rules
from apsgo_v7_service.scheduling import bind_gqga4_scheduling_task, solve_gqga4_scheduling_task
from tests.service.grade_dictionary_support import sample_grade_dictionary
from tests.service.test_month_scheduling import request_data, policy


def delivery_body():
    body = request_data(contract_version="v7-month-solve-v2", schedule_start_at="2026-06-01T08:30:00+08:00")
    for item in body["orders"]:
        item.update(due_date="2026-06-02", furnace_speed_mpm=Decimal(100), process_speed_mpm=None)
    return body


def parse(body):
    return loads_month_solve_request(dumps_exact_json(body), replace(policy(), total_time_limit_seconds=Decimal(300)))


def test_submitted_order_quantity_drives_both_weight_and_production_duration():
    body = delivery_body()
    body["orders"] = [dict(body["orders"][0], weight=Decimal("94.2"), width=1000, thickness=1)]
    parsed = parse(body)
    assert parsed.task_input.orders[0].weight == Decimal("94.2")
    assert parsed.task_input.order_timing[0].duration_hours == Decimal(2)
    body["orders"][0]["weight"] = Decimal("47.1")
    smaller = parse(body)
    assert smaller.task_input.order_timing[0].duration_hours == Decimal(1)
    assert smaller.typed_request_fingerprint != parsed.typed_request_fingerprint


@pytest.mark.parametrize("field,value", [
    ("schedule_start_at", "2026-06-01T00:00:00"), ("schedule_start_at", "2026-02-30T00:00:00+08:00"),
    ("due_date", "2026-02-30"), ("due_date", None), ("furnace_speed_mpm", None),
    ("width", None), ("thickness", 0),
])
def test_invalid_timing_is_locatable(field, value):
    body = delivery_body()
    target = body if field == "schedule_start_at" else body["orders"][0]
    target[field] = value
    with pytest.raises(MonthSchedulingContractError) as error:
        parse(body)
    assert error.value.issues[0].field_path.endswith(field)


def test_timezones_fallback_speed_and_identity():
    body = delivery_body()
    body["schedule_start_at"] = "2026-06-01T00:30:00+00:00"
    body["orders"][0].update(furnace_speed_mpm=0, process_speed_mpm=100)
    parsed = parse(body)
    assert parsed.task_input.schedule_start_at == "2026-06-01T08:30:00.000+08:00"
    assert parsed.task_input.policy.numeric_semantics_key == INTEGER_NUMERIC_SEMANTICS_KEY
    assert parsed.task_input.order_timing[0].duration_hours > 0
    body["schedule_start_at"] = "2026-06-02T00:30:00+00:00"
    assert parsed.typed_request_fingerprint != parse(body).typed_request_fingerprint


def test_delivery_http_contract_and_configuration_errors(tmp_path):
    from fastapi.testclient import TestClient
    from apsgo_v7_service.app import create_app, MONTH_SOLVE_PATH
    database = tmp_path / "http.sqlite3"
    initialize_gqga4_rules(database, initial_grade_dictionary=sample_grade_dictionary())
    with TestClient(create_app(database, monthly_solve_policy=replace(policy(), total_time_limit_seconds=Decimal(300)))) as client:
        body = delivery_body()
        bad = dict(body)
        del bad["schedule_start_at"]
        rejected = client.post(MONTH_SOLVE_PATH, content=dumps_exact_json(bad), headers={"content-type": "application/json"})
        assert rejected.status_code == 400
        assert rejected.json()["error"]["issues"][0]["field_path"] == "schedule_start_at"
        conflict = client.post(MONTH_SOLVE_PATH, content=dumps_exact_json(body), headers={"content-type": "application/json"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "delivery_configuration_mismatch"
        enable_delivery_rules(database, tmp_path / "http-backup.sqlite3", expected_active_version_id=1)
        body["expected_active_version_id"] = 2
        solved = client.post(MONTH_SOLVE_PATH, content=dumps_exact_json(body), headers={"content-type": "application/json"})
        assert solved.status_code == 200
        result = solved.json()
        assert result["publishable"] and result["audit_summary"]["passed"]
        assert len(result["quality"]) == 9
        assert result["latest_dates"]["schedule_start_at"] == result["schedule_start_at"]


def test_upgrade_preserves_history_and_can_save_and_solve(tmp_path):
    database = tmp_path / "rules.sqlite3"
    old = initialize_gqga4_rules(database, initial_grade_dictionary=sample_grade_dictionary()).active_rules
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT compiled_rule_set_json FROM v7_rule_set_version WHERE id=1").fetchone()
    upgraded = enable_delivery_rules(database, tmp_path / "backup.sqlite3", expected_active_version_id=1).active_rules
    assert len(upgraded.rule_set_spec.quality_spec) == 9
    assert upgraded.rule_set_spec.rules[:-1] == old.rule_set_spec.rules
    assert upgraded.virtual_prototypes == old.virtual_prototypes
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT compiled_rule_set_json FROM v7_rule_set_version WHERE id=1").fetchone() == before
    saved = set_active_gqga4_rules(SetActiveRulesRequest(
        save_operation_id="29ee77ed-7dc9-40a0-8da0-d8453f056977", expected_active_version_id=2,
        rules=tuple(EditableRuleInput(r.rule_id, r.enabled, r.parameters) for r in upgraded.rule_set_spec.rules),
        virtual_prototypes=upgraded.virtual_prototypes, remark="保存新目标",
    ), database)
    assert len(saved.active_rules.rule_set_spec.quality_spec) == 9
    body = delivery_body()
    body["expected_active_version_id"] = 3
    parsed = parse(body)
    bound = bind_gqga4_scheduling_task(parsed.task_input, database, expected_active_version_id=3)
    assert bound.request.delivery_timing.schedule_start_at == parsed.task_input.schedule_start_at
    solved = solve_gqga4_scheduling_task(parsed.task_input, database, expected_active_version_id=3)
    result = json.loads(dumps_month_solve_response(parsed, solved, date_configuration_path="config/month_plan_dates.json"))
    assert result["publishable"]
    assert result["run_manifest"]["algorithm_version"] == "numeric-path-cover-local-search-v1"
    assert len(result["quality"]) == 9
    assert result["schedule_start_at"] == "2026-06-01T08:30:00.000+08:00"
    nodes = result["delivery_report"]["delivery_nodes"]
    assert result["latest_dates"]["rows"][0]["current_process_start_at"] == result["schedule_start_at"]
    assert [r["current_process_latest_at"] for r in result["latest_dates"]["rows"]] == [r["completion_at"] for r in nodes]
    assert [r["latest_dates"]["coating"] for r in result["latest_dates"]["rows"]] == [r["completion_at"] for r in nodes]
    assert get_active_gqga4_rules(tmp_path / "backup.sqlite3").active_version_id == 1
    # The selected time changes actual delivery classification and scoring, not just metadata.
    body["schedule_start_at"] = "2026-06-03T08:30:00+08:00"
    later = solve_gqga4_scheduling_task(parse(body).task_input, database, expected_active_version_id=3)
    original_summary = solved.delivery_report["delivery_summary"]
    later_summary = later.delivery_report["delivery_summary"]
    assert original_summary["old_backlog_order_count"] == 0
    assert later_summary["old_backlog_order_count"] == len(body["orders"])
    assert later_summary["old_backlog_last_completion_hours"] > original_summary["old_backlog_last_completion_hours"]
