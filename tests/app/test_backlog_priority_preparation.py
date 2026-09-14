"""Opt-in identity, report verification and named gates for the new nine objectives."""

from dataclasses import replace
from decimal import Decimal as D
import json
import sys
from types import SimpleNamespace

import pytest

from apsgo_scheduler.api.request import fingerprint_public_request
from apsgo_scheduler.app.delivery_report import build_delivery_report, delivery_plan_report
from apsgo_scheduler.app.delivery_request import prepare_delivery_request, with_delivery_objective
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec, load_rule_set
from apsgo_scheduler.app.service import solve_request
from apsgo_v7_service.diagnostics import _json_values
from tests.app.test_delivery_preparation import preparation
from tests.core.test_backlog_priority_objective import BURDEN, CLEARANCE, NEW_KEYS, tradeoff_case
from tools.profile_solver_search import load_request, measure
from tools.verify_solver_diagnostics import _write_json


@pytest.mark.parametrize("enabled", [False, True])
def test_preparation_round_trip_and_explicit_identity(tmp_path, enabled):
    old, rows = preparation()
    options = dict(schedule_start_at="2026-06-01T00:00:00+08:00", order_timing=rows, virtual_speed_mpm=D(100))
    legacy = prepare_delivery_request(old, **options)
    result = prepare_delivery_request(old, **options, include_backlog_clearance=enabled)
    assert result.orders == old.orders and result.policy == old.policy
    assert result.delivery_timing == legacy.delivery_timing
    assert (result == legacy) is (not enabled)
    assert (result.rule_set_spec.fingerprint != legacy.rule_set_spec.fingerprint) is enabled
    if enabled:
        assert tuple(c.metric_key for c in result.rule_set_spec.quality_spec) == NEW_KEYS
        assert result.rule_set_spec.rules[-1].version == "2"
    else:
        assert len(result.rule_set_spec.quality_spec) == 9
        assert result.rule_set_spec.rules[-1].parameters == {}
    path = tmp_path / "request.json"
    _write_json(path, dict(request=_json_values(result), request_fingerprint=fingerprint_public_request(result),
                           rule_set_fingerprint=result.rule_set_spec.fingerprint))
    assert load_request(path) == result
    with pytest.raises(ValueError, match="seven-level"):
        with_delivery_objective(result.rule_set_spec, include_backlog_clearance=enabled)


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_invalid_opt_in_type_rejected(value):
    old, _ = preparation()
    with pytest.raises(ValueError, match="must be boolean"):
        with_delivery_objective(old.rule_set_spec, include_backlog_clearance=value)


@pytest.mark.parametrize("change", ["old_order", "late_score", "flag_off", "sum", "projection", "direction"])
def test_same_length_or_wrong_metric_policy_cannot_be_misidentified(change):
    old, _ = preparation()
    spec = with_delivery_objective(old.rule_set_spec, include_backlog_clearance=True)
    criteria, rules = list(spec.quality_spec), spec.rules
    if change == "old_order":
        criteria = list(with_delivery_objective(old.rule_set_spec).quality_spec)
    elif change == "late_score":
        criteria[2] = replace(criteria[2], metric_key="newly_late_original_weight")
    elif change == "flag_off":
        rules = (*rules[:-1], replace(rules[-1], parameters={}))
    else:
        criteria[2] = replace(criteria[2], **{"sum": {"aggregation": "maximum"},
            "projection": {"numeric_projection": "underweight_gap_2dp"},
            "direction": {"direction": "maximize"}}[change])
    spec = replace(spec, rules=rules, quality_spec=tuple(criteria))
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    with pytest.raises(ValueError):
        load_rule_set(spec)


@pytest.mark.parametrize("field", [None, "score", "tonnage", "clearance", "timing"])
def test_public_report_audits_scored_values_and_unscored_statistics(field):
    request, _, _ = tradeoff_case()
    result = solve_request(request)
    assert result.release is not None and result.core_audit.passed and result.audit_report.passed
    evaluation = result.release.evaluation
    if field in ("score", "tonnage", "clearance"):
        if field == "score":
            evaluation = replace(evaluation, quality_key=(*evaluation.quality_key[:2], D(999), *evaluation.quality_key[3:]))
        else:
            key = CLEARANCE if field == "clearance" else "newly_late_original_weight"
            evaluation = replace(evaluation, metrics={**evaluation.metrics, key: evaluation.metrics[key] + 1})
        release = replace(result.release, evaluation=evaluation)
        with pytest.raises(ValueError, match="audited identities"):
            replace(result, release=release)
        # Public construction already rejects tampering; also exercise the report's own recomputation.
        result = SimpleNamespace(run_manifest=result.run_manifest, release=release,
                                 core_audit=result.core_audit, audit_report=result.audit_report)
    elif field == "timing":
        request = replace(request, delivery_timing=replace(request.delivery_timing, schedule_start_at="2026-06-02T00:00:00+08:00"))
    if field:
        with pytest.raises(ValueError):
            build_delivery_report(request, result)
    else:
        report = build_delivery_report(request, result)
        assert report["kind"] == "audited_release"
        assert report["delivery_summary"][CLEARANCE] == evaluation.quality_key[2]
        assert report["delivery_summary"][BURDEN] == evaluation.quality_key[3]
        assert report["delivery_summary"]["newly_late_weight"] == evaluation.metrics["newly_late_original_weight"]
        assert report["delivery_metric_roles"] == {CLEARANCE: "score", BURDEN: "score",
                                                   "newly_late_original_weight": "statistics_only"}


def test_old_report_shape_stays_unchanged():
    request, state, context = tradeoff_case(backlog_priority=False)
    raw = delivery_plan_report(state.current_plan, context.factory.cache.context.delivery_timing)
    assert CLEARANCE not in raw["delivery_summary"]
    result = solve_request(request)
    assert CLEARANCE not in build_delivery_report(request, result)["delivery_summary"]
    assert "delivery_metric_roles" not in build_delivery_report(request, result)


def save_run(path, request):
    path.mkdir()
    results = []
    measurement = measure(request, scope="full", result_observer=results.append)
    _write_json(path / "prepared_request.json", dict(request=_json_values(request),
        request_fingerprint=fingerprint_public_request(request), rule_set_fingerprint=request.rule_set_spec.fingerprint))
    _write_json(path / "input_binding.json", dict(code={}))
    (path / "execution.log").write_text("small integration run\n", encoding="utf-8")
    _write_json(path / "measurement.json", measurement)
    _write_json(path / "delivery_report.json", build_delivery_report(request, results[0]))


def test_new_nine_readback_and_named_underweight_gate(tmp_path, monkeypatch):
    from tools.compare_backlog_search_runs import read_run
    from tools.summarize_delivery_comparison import main
    request, _, _ = tradeoff_case(minimum="1")
    path = tmp_path / "run"
    save_run(path, request)
    _, _, report, summary = read_run(path)
    assert report["delivery_summary"][CLEARANCE] > 0
    assert summary["quality"]["underweight_chain_count"] == 0
    output = tmp_path / "summary.json"
    monkeypatch.setattr(sys, "argv", ["summary", "--old", str(path), "--new", str(path), "--repeat", str(path), "--output", str(output)])
    main()
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["acceptance"] == "PASS"
    assert data["summaries"][0]["named_quality"][CLEARANCE] > 0
    assert data["summaries"][0]["named_quality"]["underweight_chain_count"] == 0


@pytest.mark.parametrize("variant", ["delivery", "delivery-backlog-priority"])
def test_runner_variant_uses_real_preparation_and_bound_report(tmp_path, monkeypatch, variant):
    from tools.run_delivery_comparison import main
    old, rows = preparation()
    source = tmp_path / "input.json"
    _write_json(source, dict(request=_json_values(old), request_fingerprint=fingerprint_public_request(old),
                            rule_set_fingerprint=old.rule_set_spec.fingerprint))
    timing = tmp_path / "timing.json"
    _write_json(timing, dict(orders=[dict(source_order_id=o.source_order_id,
        weight=str(o.weight), width=str(o.width), thickness=str(o.thickness),
        due_date="2026-05-31", furnace_speed_mpm="100", process_speed_mpm=None) for o in old.orders]))
    output = tmp_path / "result"
    monkeypatch.setattr(sys, "argv", ["run", "--prepared-request", str(source), "--timing-source", str(timing),
        "--start", "2026-06-01T00:00:00+08:00", "--virtual-speed", "100", "--variant", variant,
        "--output-dir", str(output)])
    assert main() == 0
    request = load_request(output / "prepared_request.json")
    report = json.loads((output / "delivery_report.json").read_text(encoding="utf-8"))
    assert report["kind"] == "audited_release"
    assert (CLEARANCE in report["delivery_summary"]) is (variant == "delivery-backlog-priority")
    assert (tuple(c.metric_key for c in request.rule_set_spec.quality_spec) == NEW_KEYS) is (variant == "delivery-backlog-priority")
