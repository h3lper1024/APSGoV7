"""Monthly responses retain business findings but never release early-start results."""

from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.app.delivery_report import build_delivery_report
from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import Chain, SchedulePlan
from apsgo_scheduler.core.rules.base import RuleDisposition
from apsgo_v7_service.app import MONTH_SOLVE_PATH, create_app
from apsgo_v7_service.month_scheduling import build_month_solve_rows
from tests.app.test_business_violation_writeback import numeric_case
from tests.app.test_result_assembler import make_manifest
from apsgo_scheduler.app.result_assembler import assemble_draft_scheduling_result, seal_scheduling_result
from apsgo_scheduler.app.contract_audit import audit_result_contract
from tests.service.test_earliest_process_start_integration import body, initialize
from tests.service.test_month_scheduling import policy, real_node, violation


@pytest.mark.parametrize("early", (False, True))
def test_http_other_prohibition_is_publishable_only_without_early_start(tmp_path, early):
    database = tmp_path / "rules.sqlite3"
    initialize(database)
    value = body("2026-06-03T00:00:00+08:00" if early else "2026-06-01T00:00:00+08:00")
    value["expected_active_version_id"] = 3
    for order in value["orders"]:
        order["grade_class"] = "IF钢"  # Each atomic order exceeds the 500 t narrow-run rule.
    selected = replace(policy(), candidate_check_limit=0,
                       total_time_limit_seconds=Decimal(600), finalization_reserve_seconds=Decimal(10))
    with TestClient(create_app(database, monthly_solve_policy=selected)) as client:
        response = client.post(MONTH_SOLVE_PATH, content=dumps_exact_json(value),
                               headers={"content-type": "application/json"})
    assert response.status_code == 200
    data = response.json()
    assert data["audit_summary"]["core"]["integrity_passed"] is True
    assert data["audit_summary"]["passed"] is False
    assert data["business_rules_satisfied"] is False
    findings = data["diagnostic_violations"] if early else data["violations"]
    assert any(item["reason_code"] == "if_narrow_run_weight" for item in findings)
    if early:
        assert data["status"] == "complete_not_publishable" and not data["publishable"]
        assert data["audit_summary"]["writeback_blocking_violation_count"] > 0
        assert data["rows"] == [] and "latest_dates" not in data
        assert data["delivery_report"]["kind"] == "diagnostic_candidate_not_publishable"
    else:
        assert data["status"] == "publishable_with_violations" and data["publishable"]
        assert data["audit_summary"]["integrity_passed"] is True
        assert data["audit_summary"]["writeback_blocking_violation_count"] == 0
        assert len(data["quality"]) == 9 and data["metrics"]
        assert len(data["rows"]) == len(data["latest_dates"]["rows"]) == 2
        assert any(row["chain_warning"] for row in data["rows"])
        assert data["delivery_report"]["kind"] == "audited_release"
        assert data["latest_dates"]["solve_reference"]["bound_result_fingerprint"] == data["bound_result_fingerprint"]
        assert [row["current_process_latest_at"] for row in data["latest_dates"]["rows"]] == [
            row["completion_at"] for row in data["delivery_report"]["delivery_nodes"]]


def test_real_numeric_edge_findings_map_to_rows_and_form_audited_dates():
    request, problem, _, _, runtime, _, _, core = numeric_case(other=True)
    draft = assemble_draft_scheduling_result(request, core, make_manifest(request, core))
    report = audit_result_contract(draft, request, problem, core, runtime)
    result = seal_scheduling_result(draft, report, runtime)
    rows = build_month_solve_rows(result.release.plan, result.release.evaluation.violations)
    assert rows[1]["width_warning"]
    delivery = build_delivery_report(request, result)
    assert delivery["kind"] == "audited_release"
    assert len(delivery["delivery_nodes"]) == len(rows)


@pytest.mark.parametrize("scope,subject,reason,target,field", (
    (RuleScope.NODE, "a", "node_check", 0, "business_warning"),
    (RuleScope.PLAN, "b", "late_original_due_period_move", 1, "business_warning"),
    (RuleScope.EDGE, "c:a>b", "soft_hard_connection_not_allowed", 1, "business_warning"),
    (RuleScope.EDGE, "c:rule:0-1", "thickness_transition_exceeded", 1, "thickness_warning"),
    (RuleScope.EDGE, "c:rule:0-1", "temperature_overlap_below_minimum", 1, "temperature_warning"),
    (RuleScope.CHAIN, "c:rule:0-1", "same_spec_run_weight", 0, "chain_warning"),
    (RuleScope.CHAIN, "c", "chain_weight_above_maximum", 0, "chain_warning"),
))
def test_all_subject_scopes_and_both_evaluator_encodings_keep_warnings(scope, subject, reason, target, field):
    plan = SchedulePlan((Chain("c", (real_node("a", "oa", "P0"), real_node("b", "ob", "P0")), "P0"),))
    finding = violation("rule", scope, subject, reason, "完整业务警告", RuleDisposition.PROHIBITED)
    rows = build_month_solve_rows(plan, (finding,))
    assert rows[target][field] == finding.message


def test_global_findings_are_not_attached_to_an_arbitrary_order():
    plan = SchedulePlan((Chain("c", (real_node("a", "oa", "P0"),), "P0"),))
    finding = violation("ratio", RuleScope.PLAN, "plan", "virtual_budget", "方案比例违规", RuleDisposition.PROHIBITED)
    rows = build_month_solve_rows(plan, (finding,))
    assert all(rows[0][key] is None for key in rows[0] if key.endswith("_warning"))
