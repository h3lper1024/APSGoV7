"""Numeric results cross the public boundary only after an independent audit."""

from dataclasses import replace
from decimal import Decimal

import numpy as np

from apsgo_scheduler.app.delivery_report import (
    build_delivery_report,
    numeric_delivery_plan_report,
)
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core._numeric_audit import audit_numeric_core_without_search_cache
from apsgo_scheduler.core._numeric_boundary import (
    numeric_evaluation_to_domain,
    numeric_plan_to_domain,
)
from apsgo_scheduler.core._numeric_construction import NumericInitialSolution
from apsgo_scheduler.core._numeric_evaluation import (
    NumericQualityProgram,
    evaluate_numeric_plan,
)
from apsgo_scheduler.core._numeric_rules import NumericRuleProgram
from apsgo_scheduler.core._numeric_search import (
    NumericSearchState,
    run_numeric_search_with_split_replay,
)
from apsgo_scheduler.core._numeric_state import NumericPlan, NumericTask, readonly
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import (
    INTEGER_NUMERIC_SEMANTICS_KEY,
    CoreAuditStatus,
    SearchStopReason,
    SolveStatus,
)
from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput
from tests.app.test_input_normalizer import make_order, make_request
from tests.core.test_numeric_evaluation import numeric_quality_spec
from tests.core.test_numeric_rules import attributes, numeric_spec

D = Decimal


def numeric_request(*, compatible=True):
    orders = tuple(
        make_order(
            index,
            weight=D("100"),
            width=D(1000 - 10 * index),
            grade=f"G{index}",
            source_period="P0",
            rule_attributes=attributes(
                surface_grade="",
                grade_class="ordinary",
                customer_name="ordinary",
            ),
        )
        for index in range(7)
    )
    spec = numeric_quality_spec() if compatible else numeric_spec()
    request = make_request(rule_set_spec=spec, orders=orders)
    timing = DeliveryTimingInput(
        "2026-06-01T00:00:00+08:00",
        tuple(
            OrderTimingInput(order.source_order_id, "2026-06-30", D("1"))
            for order in orders
        ),
        {item.prototype_id: D("0.1") for item in request.virtual_prototypes},
    )
    return replace(
        request,
        delivery_timing=timing,
        policy=replace(
            request.policy,
            numeric_semantics_key=INTEGER_NUMERIC_SEMANTICS_KEY,
            candidate_check_limit=0,
        ),
    )


def numeric_state(request):
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    task = NumericTask.build(problem, rules, request.delivery_timing)
    program = NumericRuleProgram.compile(task, rules)
    quality = NumericQualityProgram.compile(task, program, rules)
    plan = NumericPlan.build(
        task,
        range(len(problem.nodes)),
        (0, len(problem.nodes)),
        (10,),
        (0,),
    )
    evaluation = evaluate_numeric_plan(task, program, quality, plan)
    return problem, rules, NumericSearchState(task, program, quality, plan, evaluation)


def audit_budget():
    return SolveRuntimeBudget(0, 100, 110, 0, 0, None, clock=lambda: 1)


def test_numeric_boundary_restores_public_values_and_independent_audit_passes():
    request = numeric_request()
    problem, rules, state = numeric_state(request)
    plan = numeric_plan_to_domain(state.task, state.plan, problem, rules)
    evaluation = numeric_evaluation_to_domain(
        state.task,
        state.program,
        state.quality,
        state.plan,
        state.evaluation,
        plan,
    )
    outcome = audit_numeric_core_without_search_cache(
        state, problem, rules, request.delivery_timing, audit_budget()
    )

    assert evaluation.quality_key == (
        0,
        D("0"),
        0,
        D("0"),
        D("0"),
        D("0"),
        D("0"),
        D("0"),
        1,
    )
    assert outcome.report.status is CoreAuditStatus.COMPLETED
    assert outcome.report.passed and outcome.report.search_evaluation_matches
    assert outcome.audited_evaluation == evaluation


def test_numeric_audit_detects_tampered_search_quality_without_reusing_cache():
    request = numeric_request()
    problem, rules, state = numeric_state(request)
    changed = state.evaluation.quality_key.copy()
    changed[8] += 1
    changed.setflags(write=False)
    state.evaluation = replace(
        state.evaluation,
        quality_key=readonly(changed, np.int64),
    )

    outcome = audit_numeric_core_without_search_cache(
        state, problem, rules, request.delivery_timing, audit_budget()
    )

    assert outcome.report.status is CoreAuditStatus.COMPLETED
    assert not outcome.report.passed and outcome.report.search_evaluation_matches is False
    assert "search_evaluation_quality_mismatch" in {
        issue.code for issue in outcome.issues
    }


def test_numeric_audit_rebuilds_split_rows_and_verifies_allocated_duration():
    request = numeric_request()
    source = replace(
        request.orders[0],
        weight=D("600"),
        rule_attributes=attributes(
            surface_grade="",
            grade_class="IF钢",
            customer_name="ordinary",
        ),
    )
    timing = replace(
        request.delivery_timing,
        orders=(OrderTimingInput(source.source_order_id, "2026-06-30", D("1")),),
    )
    request = replace(request, orders=(source,), delivery_timing=timing)
    problem, rules, original = numeric_state(request)
    initial = NumericInitialSolution(
        "graph",
        "cover",
        original.plan,
        original.evaluation,
        None,
        0.0,
        "initial",
    )
    runtime = SolveRuntimeBudget(0, 100, 110, 1000, 0, None, clock=lambda: 1)
    state, _ = run_numeric_search_with_split_replay(
        original.task,
        original.program,
        original.quality,
        initial,
        runtime,
        pair_scan_slack_weight=0,
    )

    outcome = audit_numeric_core_without_search_cache(
        state, problem, rules, timing, runtime
    )
    plan = numeric_plan_to_domain(state.task, state.plan, problem, rules)
    report = numeric_delivery_plan_report(plan, problem.delivery_timing, timing)
    split_ids = {
        node.node_id
        for chain in plan.chains
        for node in chain.nodes
        if node.split_lineage is not None
    }

    assert state.split_sequence == 1
    assert outcome.report.status is CoreAuditStatus.COMPLETED
    assert outcome.report.audited_split_count == 1
    assert outcome.report.search_evaluation_matches
    assert sum(
        row["completion_milliseconds"] - row["start_milliseconds"]
        for row in report["delivery_nodes"]
        if row["node_id"] in split_ids
    ) == 3_600_000


def test_public_service_uses_numeric_identity_and_both_audits():
    request = numeric_request()
    result = solve_request(request)

    assert result.status is SolveStatus.SUCCESS
    assert result.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert result.run_manifest.algorithm_version == "numeric-path-cover-local-search-v1"
    assert result.release is not None
    assert result.core_audit.passed and result.audit_report.passed
    assert not result.issues
    report = build_delivery_report(request, result)
    assert report["delivery_summary"]["delivery_score_rounding"] == "half_up"
    assert report["delivery_summary"]["timing_semantics"] == INTEGER_NUMERIC_SEMANTICS_KEY
    assert report["numeric_conversion"]["physical_units"]["weight"] == 100
    assert report["numeric_conversion"]["rounding"] == "ROUND_HALF_UP"
    assert (
        report["delivery_summary"]["delivery_wait_tardiness_tonne_hours"]
        == result.release.evaluation.metrics["delivery_wait_tardiness_tonne_hours"]
    )


def test_numeric_delivery_report_keeps_half_up_second_boundary():
    request = numeric_request()
    first = request.delivery_timing.orders[0]
    timing = replace(
        request.delivery_timing,
        orders=(
            replace(
                first,
                due_date="2026-05-31",
                duration_hours=D("0.0001388888888888888888888888889"),
            ),
            *request.delivery_timing.orders[1:],
        ),
    )
    request = replace(request, delivery_timing=timing)
    result = solve_request(request)
    report = build_delivery_report(request, result)

    assert result.release.evaluation.quality_key[4] == D("0.0002777777777777777777777777778")
    assert report["delivery_summary"]["old_backlog_last_completion_hours"] == D(
        "0.0002777777777777777777777777778"
    )
    assert report["delivery_orders"][0]["completion_milliseconds"] == 500


def test_incompatible_numeric_declarations_fail_without_legacy_fallback():
    result = solve_request(numeric_request(compatible=False))

    assert result.status is SolveStatus.FAILED
    assert result.stop_reason is SearchStopReason.INPUT_INVALID
    assert result.release is None
    assert result.run_manifest.algorithm_version == "numeric-path-cover-local-search-v1"
    assert [issue.code for issue in result.issues] == ["numeric_standard_incompatible"]
