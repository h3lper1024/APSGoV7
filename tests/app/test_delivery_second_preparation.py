"""Versioned second-resolution request, result audit and controlled comparison."""

from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from apsgo_scheduler.api.request import fingerprint_public_request
from apsgo_scheduler.app.delivery_report import build_delivery_report
from apsgo_scheduler.app.delivery_request import prepare_delivery_request, with_delivery_objective
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec, load_rule_set
from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core.contracts import sum_weights
from apsgo_scheduler.core.delivery_timing import score_seconds_to_hours
from apsgo_v7_service.diagnostics import _json_values
from tests.app.test_backlog_priority_preparation import save_run
from tests.app.test_delivery_preparation import preparation
from tests.core.test_backlog_priority_objective import BURDEN, CLEARANCE, tradeoff_case
from tests.core.test_delivery_second_precision import SECOND_KEYS
from tools.compare_backlog_search_runs import compare, read_run, verify_comparison_requests
from tools.profile_solver_search import load_request
from tools.verify_solver_diagnostics import _write_json


def test_explicit_new_identity_roundtrips_and_old_modes_are_not_upgraded(tmp_path):
    old, rows = preparation()
    kwargs = dict(schedule_start_at="2026-06-01T00:00:00+08:00", order_timing=rows, virtual_speed_mpm=D(100))
    default = prepare_delivery_request(old, **kwargs)
    legacy = prepare_delivery_request(old, **kwargs, include_backlog_clearance=True)
    current = prepare_delivery_request(old, **kwargs, include_backlog_clearance=True, second_precision=True)
    assert len({r.rule_set_spec.fingerprint for r in (default, legacy, current)}) == 3
    assert tuple(c.metric_key for c in current.rule_set_spec.quality_spec) == SECOND_KEYS
    assert current.rule_set_spec.rules[-1].parameters == {"include_backlog_clearance": True, "score_time_unit": "second"}
    assert current.rule_set_spec.rules[-1].version == "3"
    assert replace(current, rule_set_spec=legacy.rule_set_spec) == legacy
    verify_comparison_requests(legacy, current, second_precision=True)
    path = tmp_path / "prepared.json"
    _write_json(path, dict(request=_json_values(current), request_fingerprint=fingerprint_public_request(current),
                          rule_set_fingerprint=current.rule_set_spec.fingerprint))
    assert load_request(path) == current
    assert prepare_delivery_request(old, **kwargs) == default


@pytest.mark.parametrize("value", [None, 0, 1, "second"])
def test_invalid_precision_option_rejected(value):
    old, _ = preparation()
    with pytest.raises(ValueError):
        with_delivery_objective(old.rule_set_spec, include_backlog_clearance=True, second_precision=value)


@pytest.mark.parametrize("change", ["historical_order", "version", "precision", "backlog", "late_score"])
def test_loader_does_not_confuse_equal_length_policy_versions(change):
    old, _ = preparation()
    spec = with_delivery_objective(old.rule_set_spec, include_backlog_clearance=True, second_precision=True)
    if change == "historical_order":
        spec = replace(spec, quality_spec=with_delivery_objective(old.rule_set_spec, include_backlog_clearance=True).quality_spec)
    elif change == "late_score":
        spec = replace(spec, quality_spec=with_delivery_objective(old.rule_set_spec).quality_spec)
    else:
        rule = spec.rules[-1]
        rule = replace(rule, **({"version": "2"} if change == "version" else {"parameters": {
            **rule.parameters, "score_time_unit" if change == "precision" else "include_backlog_clearance":
            "hour" if change == "precision" else False}}))
        spec = replace(spec, rules=(*spec.rules[:-1], rule))
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    with pytest.raises(ValueError):
        load_rule_set(spec)


def test_public_report_separates_raw_dates_and_seconds_scores_and_checks_tampering():
    request, _, _ = tradeoff_case(second_precision=True)
    request = replace(request, delivery_timing=replace(request.delivery_timing,
        orders=tuple(replace(o, duration_hours=o.duration_hours + D("0.000001")) for o in request.delivery_timing.orders)))
    result = solve_request(request)
    assert result.release and result.core_audit.passed and result.audit_report.passed
    report = build_delivery_report(request, result)
    rows, summary = report["delivery_orders"], report["delivery_summary"]
    assert summary["delivery_score_time_unit"] == "second"
    assert summary["raw_old_backlog_last_completion_hours"] != summary[CLEARANCE]
    assert score_seconds_to_hours(sum_weights(r["score_wait_tardiness_tonne_seconds"] for r in rows)) == summary[BURDEN]
    assert summary[CLEARANCE] == result.release.evaluation.quality_key[4]
    assert summary[BURDEN] == result.release.evaluation.quality_key[5]
    for field in (CLEARANCE, BURDEN, "newly_late_original_weight"):
        evaluation = result.release.evaluation
        forged = replace(evaluation, metrics={**evaluation.metrics, field:evaluation.metrics[field] + 1})
        fake = SimpleNamespace(run_manifest=result.run_manifest, release=replace(result.release,evaluation=forged),
                               core_audit=result.core_audit,audit_report=result.audit_report)
        with pytest.raises(ValueError):
            build_delivery_report(request,fake)


def test_complete_small_run_comparison_recomputes_both_plans_in_new_units(tmp_path):
    old, _, _ = tradeoff_case(minimum="1")
    new, _, _ = tradeoff_case(minimum="1", second_precision=True)
    old_path, new_path = tmp_path / "old", tmp_path / "new"
    save_run(old_path, old)
    save_run(new_path, new)
    summary, changes = compare(old_path, new_path, second_precision=True)
    assert len(changes) == 4 and summary["runs"][1]["both_audits_passed"]
    assert "second_precision_projection_not_original_score" in summary["runs"][0]
    repeat, _ = compare(new_path, new_path)
    assert repeat["final_search_equal"] and repeat["delivery_report_equal"]
    assert read_run(new_path)[0] == new
    with pytest.raises(ValueError):
        verify_comparison_requests(old, replace(new, policy=replace(new.policy, candidate_check_limit=101)), second_precision=True)
    with pytest.raises(ValueError):
        verify_comparison_requests(old,new)
