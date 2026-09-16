"""Public solve lifecycle preserves release-time guards and no-wait semantics."""
from dataclasses import replace
from decimal import Decimal

from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.app.delivery_report import build_delivery_report
from tests.core.test_earliest_process_start_rule import early_request


def request(*, all_unavailable=False, enabled=True):
    value = early_request(enabled=enabled, lower="2026-06-01T03:00:00+08:00")
    timing = value.delivery_timing
    if all_unavailable:
        timing = replace(timing, orders=tuple(replace(order,
            earliest_start_at="2026-06-03T00:00:00+08:00") for order in timing.orders))
    return replace(value, delivery_timing=timing,
        orders=tuple(replace(order, width=Decimal(1000)) for order in value.orders),
        policy=replace(value.policy, candidate_check_limit=500,
            total_time_limit_seconds=Decimal(600), finalization_reserve_seconds=Decimal(10)))


def test_public_search_repairs_and_publishes_without_changing_start_or_duration():
    value = request()
    result = solve_request(value)
    assert result.release is not None
    report = build_delivery_report(value, result)
    assert report["delivery_summary"]["early_start_node_count"] == 0
    nodes = report["delivery_nodes"]
    assert nodes[0]["chain_id"] == result.release.plan.chains[0].chain_id
    assert nodes[0]["assigned_period"] == result.release.plan.chains[0].assigned_period
    assert nodes[0]["start_at"] == "2026-06-01T00:00:00.000+08:00"
    assert nodes[-1]["completion_at"] == "2026-06-01T07:00:00.000+08:00"
    assert all(left["completion_at"] == right["start_at"] for left, right in zip(nodes, nodes[1:]))


def test_public_search_cannot_publish_all_unavailable_or_insert_waiting():
    value = request(all_unavailable=True)
    result = solve_request(value)
    assert result.release is None and result.diagnostic_candidate is not None
    report = build_delivery_report(value, result)
    assert report["kind"] == "diagnostic_candidate_not_publishable"
    assert report["delivery_summary"]["early_start_node_count"] == 7
    assert report["delivery_nodes"][0]["start_at"] == "2026-06-01T00:00:00.000+08:00"
    assert report["delivery_nodes"][-1]["completion_at"] == "2026-06-01T07:00:00.000+08:00"


def test_disabled_bound_values_do_not_change_search_plan_or_quality():
    value = request(enabled=False)
    other = request(all_unavailable=True, enabled=False)
    left, right = solve_request(value), solve_request(other)
    assert left.release is not None and right.release is not None
    assert left.release.plan == right.release.plan
    assert left.release.evaluation.quality_key == right.release.evaluation.quality_key
