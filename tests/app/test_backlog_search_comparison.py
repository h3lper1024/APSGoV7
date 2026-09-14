"""Small checks for read-only original-order clearance statistics."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from tools.compare_backlog_search_runs import backlog_summary
from tools.run_delivery_comparison import observe_recipes


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


def test_tail_includes_ties_without_mutating_input():
    rows = [order(str(i), "1", "10") for i in range(12)] + [order("early", "1", "01")]
    assert len(backlog_summary(rows)["tail"]) == 12
    assert rows[-1]["source_order_id"] == "early"


@pytest.mark.parametrize("accepted,raises", [(False, False), (True, False), (False, True)])
def test_probe_forwards_once_preserves_results_and_accounts_scanner_and_bridge_checks(accepted, raises):
    node = SimpleNamespace(source_order_id="old", node_id="piece")
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
