"""Policy and diagnostic counters stay explicit, immutable and internally consistent."""

from dataclasses import MISSING, FrozenInstanceError, fields, replace
from decimal import Decimal

import pytest

from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    CoreAuditReport,
    CoreAuditStatus,
    SolveMetrics,
    SolverPolicy,
)


def policy(**changes):
    values = {
        "seed": 7,
        "total_time_limit_seconds": Decimal("12"),
        "finalization_reserve_seconds": Decimal("2"),
        "candidate_check_limit": 5,
        "construction_order_key": CONSTRUCTION_ORDER_KEY,
        "numeric_semantics_key": NUMERIC_SEMANTICS_KEY,
        "whole_chain_pair_scan_slack_weight": Decimal("0"),
    }
    return SolverPolicy(**(values | changes))


@pytest.mark.parametrize("bridge_limit", [0, 1, 2])
def test_zero_candidates_signed_seed_and_supported_bridges(bridge_limit):
    result = policy(seed=-17, candidate_check_limit=0, maximum_virtual_bridge_nodes=bridge_limit)
    assert result.seed == -17
    assert result.candidate_check_limit == 0
    assert result.maximum_virtual_bridge_nodes == bridge_limit
    with pytest.raises(FrozenInstanceError):
        result.candidate_check_limit = 1


def test_policy_has_no_hidden_benchmark_defaults():
    for field in fields(SolverPolicy):
        if field.name == "maximum_virtual_bridge_nodes":
            assert field.default == 2
        else:
            assert field.default is MISSING
            assert field.default_factory is MISSING


@pytest.mark.parametrize(
    "name,value",
    [
        ("seed", True),
        ("seed", 1.0),
        ("candidate_check_limit", -1),
        ("candidate_check_limit", False),
        ("candidate_check_limit", Decimal("1")),
        ("total_time_limit_seconds", Decimal("0")),
        ("finalization_reserve_seconds", Decimal("0")),
        ("finalization_reserve_seconds", Decimal("12")),
        ("finalization_reserve_seconds", Decimal("13")),
        ("whole_chain_pair_scan_slack_weight", Decimal("-0.01")),
        ("maximum_virtual_bridge_nodes", -1),
        ("maximum_virtual_bridge_nodes", True),
        ("maximum_virtual_bridge_nodes", 3),
        ("construction_order_key", "unregistered_order"),
        ("numeric_semantics_key", "unregistered_numbers"),
    ],
)
def test_policy_rejects_invalid_values(name, value):
    with pytest.raises(ValueError):
        policy(**{name: value})


@pytest.mark.parametrize(
    "name",
    [
        "total_time_limit_seconds",
        "finalization_reserve_seconds",
        "whole_chain_pair_scan_slack_weight",
    ],
)
def test_policy_authoritative_numbers_must_be_finite_decimals(name):
    for value in (1, 1.0, "1", True, Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            policy(**{name: value})


def test_metrics_preserve_split_counts_and_copy_duration_mapping():
    durations = {"construction": Decimal("0.2")}
    result = SolveMetrics(
        accepted_split_count=3,
        accepted_same_period_split_count=2,
        accepted_future_borrow_return_count=1,
        stage_duration_seconds=durations,
    )
    durations["construction"] = Decimal("99")
    assert result.stage_duration_seconds == {"construction": Decimal("0.2")}
    with pytest.raises(TypeError):
        result.stage_duration_seconds["search"] = Decimal("1")
    with pytest.raises(FrozenInstanceError):
        result.accepted_split_count = 4
    with pytest.raises(ValueError):
        replace(result, accepted_split_count=4)
    assert SolveMetrics().stage_duration_seconds == {}


def test_metrics_reject_invalid_counters_and_duration_values():
    for field in fields(SolveMetrics):
        if field.name != "stage_duration_seconds":
            for value in (-1, True, Decimal("1")):
                with pytest.raises(ValueError):
                    SolveMetrics(**{field.name: value})
    for value in (1, True, Decimal("-1"), Decimal("NaN"), {"nested": Decimal("1")}):
        with pytest.raises(ValueError):
            SolveMetrics(stage_duration_seconds={"search": value})


def audit_report(**changes):
    values = {
        "status": CoreAuditStatus.NOT_RUN,
        "passed": False,
        "integrity_passed": False,
        "writeback_blocking_violation_count": None,
        "audited_evaluation_fingerprint": None,
        "invariant_failure_codes": (),
        "action_authorization_failure_codes": (),
        "derived_resource_fingerprint": None,
        "audited_split_count": None,
        "audited_same_period_split_count": None,
        "audited_future_borrow_return_count": None,
        "search_evaluation_matches": None,
        "report_fingerprint": "audit-report",
    }
    return CoreAuditReport(**(values | changes))


@pytest.mark.parametrize(
    "status",
    [
        CoreAuditStatus.NOT_RUN,
        CoreAuditStatus.CANCELLED,
        CoreAuditStatus.TIME_LIMIT,
        CoreAuditStatus.ERROR,
    ],
)
def test_unfinished_audit_cannot_claim_zero_counts_or_completed_comparison(status):
    result = audit_report(status=status)
    assert result.audited_split_count is None
    assert result.audited_same_period_split_count is None
    assert result.audited_future_borrow_return_count is None
    assert result.search_evaluation_matches is None
    for name, value in (
        ("audited_split_count", 0),
        ("audited_same_period_split_count", 0),
        ("audited_future_borrow_return_count", 0),
        ("search_evaluation_matches", False),
        ("passed", True),
        ("integrity_passed", True),
        ("writeback_blocking_violation_count", 0),
        ("audited_evaluation_fingerprint", "evaluation"),
        ("derived_resource_fingerprint", "resources"),
    ):
        with pytest.raises(ValueError):
            replace(result, **{name: value})


def test_completed_audit_counts_and_success_conditions():
    result = audit_report(
        status=CoreAuditStatus.COMPLETED,
        passed=True,
        integrity_passed=True,
        writeback_blocking_violation_count=0,
        audited_evaluation_fingerprint="evaluation",
        derived_resource_fingerprint="resources",
        audited_split_count=3,
        audited_same_period_split_count=2,
        audited_future_borrow_return_count=1,
        search_evaluation_matches=True,
    )
    for name, value in (
        ("audited_split_count", 2),
        ("audited_split_count", None),
        ("audited_same_period_split_count", True),
        ("audited_future_borrow_return_count", -1),
        ("search_evaluation_matches", None),
        ("search_evaluation_matches", False),
        ("audited_evaluation_fingerprint", None),
        ("derived_resource_fingerprint", None),
        ("invariant_failure_codes", ("weight_not_conserved",)),
        ("action_authorization_failure_codes", ("split_not_authorized",)),
        ("integrity_passed", False),
        ("writeback_blocking_violation_count", None),
        ("writeback_blocking_violation_count", 1),
        ("writeback_blocking_violation_count", True),
        ("writeback_blocking_violation_count", -1),
    ):
        with pytest.raises(ValueError):
            replace(result, **{name: value})
    failures = ["evaluation_mismatch"]
    failed = replace(
        result, passed=False, integrity_passed=False, writeback_blocking_violation_count=None,
        search_evaluation_matches=False, invariant_failure_codes=failures
    )
    failures.append("later_mutation")
    assert failed.invariant_failure_codes == ("evaluation_mismatch",)
    with pytest.raises(FrozenInstanceError):
        failed.passed = True
