"""Small checks for read-only original-order clearance statistics."""

from decimal import Decimal
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tools.compare_backlog_search_runs import backlog_summary
from tools.run_delivery_comparison import observe_recipes, observe_lane_batches


def order(key, weight, hours, backlog=True):
    return dict(source_order_id=key, original_weight=Decimal(weight), completion_hours=Decimal(hours),
                completion_at=f"2026-06-01T{hours}:00:00+08:00", was_backlog_at_start=backlog,
                delivery_wait_tardiness_tonne_hours=Decimal(weight) * Decimal(hours))


def test_clearance_counts_whole_originals_and_finishing_ties_together():
    rows = [order("late", "10", "12"), order("part1", "20", "01"), order("part2", "30", "01"),
            order("middle", "40", "05"), order("new", "900", "02", False)]
    result = backlog_summary(rows)
    assert result["original_weight"] == 100 and result["order_count"] == 4
    assert [result["clearance"][key]["completion_hours"] for key in ("50", "90", "100")] == [1, 5, 12]
    assert result["clearance"]["50"]["completed_original_weight"] == 50
    assert result["weighted_mean_wait_hours"] == Decimal("3.7")
    assert result["completed_original_weight_by_date"] == {"2026-06-01": Decimal(100)}


def test_no_backlog_has_no_clearance_or_average():
    result = backlog_summary([order("new", "10", "01", False)])
    assert result["order_count"] == result["original_weight"] == result["burden_tonne_hours"] == 0
    assert result["clearance"] == {} and result["tail"] == []
    assert result["weighted_mean_wait_hours"] is None


def test_lane_observer_preserves_actual_search_and_accounts_all_checks():
    from tests.core.test_backlog_priority_objective import tradeoff_case
    from apsgo_scheduler.core.width_optimization import run_width_optimization
    from apsgo_scheduler.core.contracts import fingerprint
    _, first, first_context = tradeoff_case(second_precision=True, minimum="1")
    _, second, second_context = tradeoff_case(second_precision=True, minimum="1")
    run_width_optimization(first, first_context)
    observations = {}
    with observe_lane_batches(observations):
        run_width_optimization(second, second_context)
    assert fingerprint(first) == fingerprint(second)
    assert first_context.factory.budget.candidate_check_count == second_context.factory.budget.candidate_check_count
    lanes = observations["lanes"]
    assert set(lanes) == {"critical", "normal", "cleanup"}
    assert lanes["cleanup"]["candidate_check_count"] == 0
    assert sum(row["candidate_check_count"] for row in lanes.values()) == second_context.factory.budget.candidate_check_count
    assert sum(row["accepted_move_count"] for row in lanes.values()) == second.accepted_move_count


def test_snapshot_high_watermark_survives_reclamation_and_legacy_cannot_resume_generation(tmp_path):
    from tools.replay_backlog_search_witnesses import load_case, read_json
    from tools.profile_solver_search import _snapshot
    from apsgo_scheduler.api.json_codec import dumps_exact_json
    from tests.app.test_backlog_priority_preparation import save_run
    from tests.core.test_backlog_priority_objective import tradeoff_case
    from tests.core.search.test_bridge_reclamation import case, attempt
    state, context, bridges = case()
    assert attempt(state, context, bridges)
    assert _snapshot(state, context)["virtual_sequence"] == 9
    request, _, _ = tradeoff_case(second_precision=True)
    run = tmp_path / "run"
    save_run(run, request)
    value = read_json(run / "measurement.json")
    value["final_search"]["virtual_sequence"] = 99
    (run / "measurement.json").write_text(dumps_exact_json(value))
    assert load_case(run, require_virtual_sequence=True)[0].virtual_sequence == 99
    for invalid in (-1, True):
        value["final_search"]["virtual_sequence"] = invalid
        (run / "measurement.json").write_text(dumps_exact_json(value))
        with pytest.raises(ValueError):
            load_case(run)
    value["final_search"].pop("virtual_sequence")
    (run / "measurement.json").write_text(dumps_exact_json(value))
    with pytest.raises(ValueError, match="generation cannot resume"):
        load_case(run, require_virtual_sequence=True)
    assert load_case(run)[1].policy.maximum_virtual_bridge_nodes == 0


def test_snapshot_compatibility_does_not_hide_present_sequence_mismatch():
    from tools.compare_backlog_search_runs import snapshots_equal
    assert snapshots_equal({"plan": 1}, {"plan": 1, "virtual_sequence": 9})
    assert not snapshots_equal({"plan": 1}, {"plan": 2, "virtual_sequence": 9})
    assert not snapshots_equal({"plan": 1, "virtual_sequence": 8}, {"plan": 1, "virtual_sequence": 9})


def test_tail_includes_ties_without_mutating_input():
    rows = [order(str(i), "1", "10") for i in range(12)] + [order("early", "1", "01")]
    assert len(backlog_summary(rows)["tail"]) == 12
    assert rows[-1]["source_order_id"] == "early"


@pytest.mark.parametrize("accepted,raises", [(False, False), (True, False), (False, True)])
def test_probe_forwards_once_preserves_results_and_accounts_scanner_and_bridge_checks(accepted, raises):
    node = SimpleNamespace(source_order_id="old", node_id="piece", virtual_lineage=None)
    chains = [SimpleNamespace(chain_id="source", nodes=(node,)), SimpleNamespace(chain_id="target", nodes=())]
    state = SimpleNamespace(current_plan=SimpleNamespace(chains=chains), accepted_move_count=0)
    budget = SimpleNamespace(candidate_check_count=1)
    timing = SimpleNamespace(orders={"old": SimpleNamespace(due_hours=Decimal(0))})
    context = SimpleNamespace(complete_candidate_evaluation_count=0, factory=SimpleNamespace(
        budget=budget, cache=SimpleNamespace(context=SimpleNamespace(delivery_timing=timing))))
    recipe = ("width_node_move", 0, 1, 0, 1, 0, 0)
    calls, stats = [], {}

    def original(actual_state, actual_context, actual_recipe):
        calls.append((actual_state, actual_context, actual_recipe))
        budget.candidate_check_count += 2
        context.complete_candidate_evaluation_count += 1
        state.accepted_move_count += int(accepted)
        if raises:
            raise RuntimeError("original failure")
        return accepted

    observed = observe_recipes(original, stats)
    if raises:
        with pytest.raises(RuntimeError, match="original failure"):
            observed(state, context, recipe)
    else:
        assert observed(state, context, recipe) is accepted
    assert calls == [(state, context, recipe)]
    assert stats["actions"]["width_node_move"] == dict(attempted_proposals=1, candidate_checks=3,
        complete_evaluations=1, accepted=int(accepted), backlog_originals={"old": True})
    assert stats["first_backlog_node_moves"]["old"]["at_candidate_check"] == 1


@pytest.mark.parametrize("tamper", [None, "quality", "candidate_check_count", "date"])
def test_real_small_audited_run_is_recomputed_and_inconsistent_summaries_rejected(tmp_path, tamper):
    from apsgo_scheduler.api.json_codec import dumps_exact_json
    from apsgo_scheduler.api.request import fingerprint_public_request
    from apsgo_scheduler.app.delivery_report import build_delivery_report
    from apsgo_v7_service.diagnostics import _json_values
    from tests.core.test_delivery_search_audit import search_case
    from tools.compare_backlog_search_runs import read_run
    from tools.profile_solver_search import measure
    from tools.verify_solver_diagnostics import _write_json

    request, _, _ = search_case()
    results = []
    measurement = measure(request, scope="full", result_observer=results.append)
    report = build_delivery_report(request, results[0])
    _write_json(tmp_path / "prepared_request.json", dict(request=_json_values(request),
        request_fingerprint=fingerprint_public_request(request), rule_set_fingerprint=request.rule_set_spec.fingerprint))
    _write_json(tmp_path / "input_binding.json", dict(code={}))
    (tmp_path / "execution.log").write_text("small audited test only\n", encoding="utf-8")
    if tamper == "quality":
        measurement["final_search"]["quality"][0] += 1
    elif tamper == "candidate_check_count":
        measurement["final_search"]["candidate_check_count"] += 1
    elif tamper == "date":
        report["delivery_orders"][0]["completion_hours"] += 1
    _write_json(tmp_path / "measurement.json", measurement)
    (tmp_path / "delivery_report.json").write_text(dumps_exact_json(report), encoding="utf-8")
    if tamper:
        with pytest.raises(ValueError, match="differ"):
            read_run(tmp_path)
    else:
        restored, _, actual, summary = read_run(tmp_path)
        assert restored == request and len(actual["delivery_orders"]) == len(request.orders)
        assert summary["both_audits_passed"] and summary["source_conservation_passed"]


@pytest.mark.parametrize("change", [None, "budget", "identity", "order", "rule", "version"])
def test_score_comparison_only_accepts_the_exact_approved_change(change):
    from apsgo_scheduler.app.delivery_request import with_delivery_objective
    from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec
    from tests.core.test_delivery_search_audit import search_case
    from tools.compare_backlog_search_runs import verify_comparison_requests
    request, _, _ = search_case(delivery=False)
    old = replace(request, rule_set_spec=with_delivery_objective(request.rule_set_spec))
    new = replace(request, rule_set_spec=with_delivery_objective(request.rule_set_spec, include_backlog_clearance=True))
    if change == "budget":
        new = replace(new, policy=replace(new.policy, candidate_check_limit=101))
    elif change == "identity":
        new = replace(new, request_id="another-request")
    elif change == "order":
        new = replace(new, orders=tuple(reversed(new.orders)))
    elif change in ("rule", "version"):
        spec = new.rule_set_spec
        if change == "rule":
            spec = replace(spec, rules=(replace(spec.rules[0], parameters={**spec.rules[0].parameters, "min_weight": Decimal(2)}), *spec.rules[1:]))
        else:
            spec = replace(spec, version="unapproved-version")
        new = replace(new, rule_set_spec=replace(spec, fingerprint=fingerprint_rule_set_spec(spec)))
    with pytest.raises(ValueError):
        verify_comparison_requests(old, new)
    if change:
        with pytest.raises(ValueError):
            verify_comparison_requests(old, new, backlog_priority=True)
    else:
        verify_comparison_requests(old, new, backlog_priority=True)
        verify_comparison_requests(new, new)


@pytest.mark.parametrize("tamper", [None, "label", "candidate", "score"])
def test_nonpublishable_comparison_is_explicit_and_never_claims_passing_audits(tmp_path, tamper):
    from apsgo_scheduler.api.request import RuleDefinitionSpec
    from apsgo_scheduler.core.contracts import RuleScope
    from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec
    from tests.core.test_backlog_priority_objective import tradeoff_case
    from tests.app.test_backlog_priority_preparation import save_run
    from tools.compare_backlog_search_runs import read_run, compare
    from tools.replay_backlog_search_witnesses import read_json
    from tools.profile_solver_search import load_request
    from tools.verify_solver_diagnostics import _write_json
    from apsgo_v7_service.diagnostics import _json_values
    from apsgo_scheduler.api.request import fingerprint_public_request
    from apsgo_scheduler.api.json_codec import dumps_exact_json
    request, _, _ = tradeoff_case()
    spec = replace(request.rule_set_spec, rules=(*request.rule_set_spec.rules, RuleDefinitionSpec(
        "narrow", "ContinuousNarrowSteelWeightRule", "窄钢连续", RuleScope.CHAIN, True, "1",
        {"grade_class": "IF", "width_upper_exclusive": Decimal(1200), "max_real_weight": Decimal(500)})))
    request = replace(request, rule_set_spec=replace(spec, fingerprint=fingerprint_rule_set_spec(spec)),
                      orders=tuple(replace(o, rule_attributes={**o.rule_attributes, "grade_class": "IF"}) for o in request.orders))
    # Mirror the actual CLI's stored-request boundary, including numeric rendering in violation messages.
    canonical = tmp_path / "canonical.json"
    _write_json(canonical, dict(request=_json_values(request), request_fingerprint=fingerprint_public_request(request),
                               rule_set_fingerprint=request.rule_set_spec.fingerprint))
    request = load_request(canonical)
    path = tmp_path / "failed"
    save_run(path, request)
    with pytest.raises(ValueError, match="audits must pass"):
        read_run(path)
    if tamper == "label":
        report = read_json(path / "delivery_report.json")
        report["kind"] = "audited_release"
        (path / "delivery_report.json").write_text(dumps_exact_json(report), encoding="utf-8")
    elif tamper:
        run = read_json(path / "measurement.json")
        if tamper == "candidate":
            run["result"]["diagnostic_candidate"] = None
        else:
            run["final_search"]["quality"][0] = 0
        (path / "measurement.json").write_text(dumps_exact_json(run), encoding="utf-8")
    if tamper:
        with pytest.raises(ValueError):
            read_run(path, allow_diagnostic=True)
    else:
        restored, _, _, summary = read_run(path, allow_diagnostic=True)
        assert restored == request and summary["quality_gate"] == "NOT_PUBLISHABLE"
        assert not summary["both_audits_passed"] and summary["source_conservation_passed"]
        repeated, _ = compare(path, path, allow_diagnostic=True)
        assert repeated["includes_non_publishable_diagnostic"] and repeated["final_search_equal"]


def test_scoring_comparison_projects_by_name_without_rewriting_old_score(tmp_path):
    from tests.app.test_backlog_priority_preparation import save_run
    from tests.core.test_backlog_priority_objective import tradeoff_case, CLEARANCE, NEW_KEYS
    from tools.compare_backlog_search_runs import compare
    old, _, _ = tradeoff_case(backlog_priority=False)
    new, _, _ = tradeoff_case()
    save_run(tmp_path / "old", old)
    save_run(tmp_path / "new", new)
    summary, changes = compare(tmp_path / "old", tmp_path / "new", backlog_priority=True)
    assert not summary["identical_request"] and len(changes) == 4
    a, b = summary["runs"]
    assert CLEARANCE not in a["quality"]
    assert tuple(a["backlog_priority_projection_not_original_score"]) == NEW_KEYS
    assert b["backlog_priority_projection_not_original_score"] == b["quality"]
