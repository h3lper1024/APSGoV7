"""Versioned month requests bind release times to a verified active rule snapshot."""
from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.rule_management import EditableRuleInput, SetActiveRulesRequest
from apsgo_v7_service.app import create_app, MONTH_SOLVE_PATH
from apsgo_v7_service.enable_delivery_rules import enable_delivery_rules
from apsgo_v7_service.month_scheduling import MonthSchedulingContractError, loads_month_solve_request
from apsgo_v7_service.rule_management import initialize_gqga4_rules, set_active_gqga4_rules, get_active_gqga4_rules
from apsgo_v7_service.restore_gqga4_rule_version import restore_gqga4_rule_version
from tests.service.grade_dictionary_support import sample_grade_dictionary
from tests.service.test_month_delivery_integration import delivery_body
from tests.service.test_month_scheduling import policy


def body(lower="2026-06-01T00:00:00+08:00"):
    value = delivery_body()
    value["contract_version"] = "v7-month-solve-v3"
    for order in value["orders"]:
        if lower is not None:
            order["earliest_start_at"] = lower
    return value


def parse(value):
    return loads_month_solve_request(dumps_exact_json(value), policy())


def initialize(database, *, enabled=True):
    initialize_gqga4_rules(database, initial_grade_dictionary=sample_grade_dictionary())
    old = enable_delivery_rules(database, database.with_suffix(".backup.sqlite3"), expected_active_version_id=1).active_rules
    request = SetActiveRulesRequest(save_operation_id="9bb82473-24ae-4221-a784-44c52d06b96f", expected_active_version_id=2,
        rules=tuple(EditableRuleInput(r.rule_id, r.enabled, r.parameters) for r in old.rule_set_spec.rules)
            + (EditableRuleInput("earliest_process_start", enabled, {}),),
        virtual_prototypes=old.virtual_prototypes, remark="最早开工规则隔离测试")
    saved = set_active_gqga4_rules(request, database)
    return old, request, saved


@pytest.mark.parametrize("lower", ("", "2026-06-01", "2026-06-01T00:00:00", "2026-06-01T00:00:00Z", "2026-06-01T00:00:00.001+08:00", 0))
def test_v3_malformed_lower_is_locatable_even_before_binding(lower):
    with pytest.raises(MonthSchedulingContractError) as error:
        parse(body(lower))
    assert error.value.issues[0].field_path == "orders[0].earliest_start_at"


def test_v3_optional_lower_and_old_strict_contracts():
    assert parse(body(None)).task_input.order_timing[0].earliest_start_at is None
    request = body()
    assert parse(request).task_input.order_timing[0].earliest_start_at == request["orders"][0]["earliest_start_at"]
    request["contract_version"] = "v7-month-solve-v2"
    with pytest.raises(MonthSchedulingContractError) as error:
        parse(request)
    assert error.value.issues[0].code == "unknown_field"


def test_rule_save_idempotence_conflict_and_history(tmp_path):
    from apsgo_v7_service.rule_management import RuleManagementServiceError
    database = tmp_path / "rules.sqlite3"
    old, request, saved = initialize(database)
    active = get_active_gqga4_rules(database)
    assert active.active_version_id == 3
    assert active.rule_set_spec.rules[:-1] == old.rule_set_spec.rules
    assert active.rule_set_spec.quality_spec == old.rule_set_spec.quality_spec
    assert set_active_gqga4_rules(request, database).active_rules.active_version_id == 3
    with pytest.raises(RuleManagementServiceError):
        set_active_gqga4_rules(replace(request, save_operation_id="5c9bd869-a81e-4fcb-b3c0-f96087472a8c"), database)
    assert get_active_gqga4_rules(database.with_suffix(".backup.sqlite3")).active_version_id == 1
    restored = restore_gqga4_rule_version(2, "b940b38c-60a1-46a7-aa28-ed700eb306cd", 3, database).active_rules
    assert restored.active_version_id == 4
    assert restored.rule_set_spec.rules == old.rule_set_spec.rules
    assert not any(r.rule_id == "earliest_process_start" for r in restored.rule_set_spec.rules)


def test_v3_http_hard_failure_success_and_disabled_missing(tmp_path):
    database = tmp_path / "http.sqlite3"
    _, _, saved = initialize(database)
    selected = replace(policy(), candidate_check_limit=0, total_time_limit_seconds=Decimal(600), finalization_reserve_seconds=Decimal(10))
    with TestClient(create_app(database, monthly_solve_policy=selected)) as client:
        def solve(value):
            value["expected_active_version_id"] = get_active_gqga4_rules(database).active_version_id
            response = client.post(MONTH_SOLVE_PATH, content=dumps_exact_json(value), headers={"content-type": "application/json"})
            return response
        old = delivery_body()
        rejected = solve(old)
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "earliest_start_configuration_mismatch"
        missing = solve(body(None))
        assert missing.status_code == 422
        assert any(i["code"] == "missing_earliest_start" for i in missing.json()["issues"])
        good = solve(body()).json()
        assert good["contract_version"] == "v7-month-solve-v3"
        assert good["publishable"] and good["audit_summary"]["passed"]
        assert len(good["quality"]) == 9
        bad = solve(body("2026-06-03T00:00:00+08:00")).json()
        assert not bad["publishable"] and not bad["rows"] and "latest_dates" not in bad
        assert any(v["reason_code"] == "earliest_process_start_violated" for v in bad["diagnostic_violations"])
        assert bad["delivery_report"]["kind"] == "diagnostic_candidate_not_publishable"
        assert bad["delivery_report"]["earliest_start_constraint_enabled"] is True
        assert bad["delivery_report"]["delivery_summary"]["early_start_node_count"] > 0
        active = get_active_gqga4_rules(database)
        set_active_gqga4_rules(SetActiveRulesRequest(
            save_operation_id="151ae598-c94f-4ee6-a787-7c503259ef97", expected_active_version_id=active.active_version_id,
            rules=tuple(EditableRuleInput(r.rule_id, False if r.rule_id == "earliest_process_start" else r.enabled, r.parameters)
                        for r in active.rule_set_spec.rules), virtual_prototypes=active.virtual_prototypes), database)
        disabled = solve(body(None)).json()
        assert disabled["publishable"]
        statistics_only = solve(body("2026-06-03T00:00:00+08:00")).json()
        assert statistics_only["publishable"]
        assert statistics_only["delivery_report"]["earliest_start_constraint_enabled"] is False
        assert statistics_only["delivery_report"]["delivery_summary"]["early_start_node_count"] > 0
