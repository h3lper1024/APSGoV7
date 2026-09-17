"""Only audited early starts block business writeback; corrupt results always block."""

from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest

from apsgo_scheduler.app.contract_audit import audit_result_contract
from apsgo_scheduler.app.result_assembler import assemble_draft_scheduling_result, seal_scheduling_result
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec
from apsgo_scheduler.core import final_audit, solver
from apsgo_scheduler.core._numeric_audit import audit_numeric_core_without_search_cache
from apsgo_scheduler.core._numeric_boundary import numeric_evaluation_to_domain, numeric_plan_to_domain
from apsgo_scheduler.core._numeric_state import readonly
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import (
    CoreCandidateSnapshot, DiagnosticIssue, DiagnosticPhase, DiagnosticSeverity,
    SearchStopReason, SolveMetrics, SolveStatus,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, SchedulePlan, SearchState
from tests.app.test_result_assembler import make_manifest
from tests.core.test_earliest_process_start_rule import early_request
from tests.core.test_numeric_boundary_audit import numeric_state
from tests.core.test_solver_status_matrix import audited_case, report_identity
from tests.core.audit.test_final_audit_without_cache import evaluation_context
from tests.core.graph.test_construction_order import node, rules
from tests.core.search.test_whole_chain_neighborhood import weight_rule


def numeric_case(*, early=False, other=True, enabled=True, rule_id="custom-release-time"):
    request = early_request(enabled=enabled, lower=(
        "2026-06-01T00:00:01+08:00" if early else "2026-06-01T00:00:00+08:00"))
    spec = replace(request.rule_set_spec, rules=tuple(
        replace(rule, rule_id=rule_id) if rule.rule_type == "EarliestProcessStartRule" else rule
        for rule in request.rule_set_spec.rules))
    request = replace(request, rule_set_spec=replace(spec, fingerprint=fingerprint_rule_set_spec(spec)))
    if other:
        # Real fixed plan: its first edge exceeds the ordinary real width tolerance.
        request = replace(request, orders=(replace(request.orders[0], width=Decimal(500)), *request.orders[1:]))
    problem, ruleset, numeric = numeric_state(request)
    runtime = SolveRuntimeBudget.from_policy(request.policy, 0, None, clock=lambda: 1)
    runtime.stop_reason = SearchStopReason.CANDIDATE_LIMIT_REACHED
    plan = numeric_plan_to_domain(numeric.task, numeric.plan, problem, ruleset)
    evaluation = numeric_evaluation_to_domain(numeric.task, numeric.program, numeric.quality,
                                             numeric.plan, numeric.evaluation, plan)
    state = SearchState(plan, evaluation)
    audit = audit_numeric_core_without_search_cache(numeric, problem, ruleset, request.delivery_timing, runtime)
    core = solver.build_core_solver_result(problem, ruleset, request.policy, runtime,
        state=state, metrics=SolveMetrics(final_chain_count=1), audit=audit)
    return request, problem, ruleset, numeric, runtime, state, audit, core


@pytest.mark.parametrize("early", (False, True))
@pytest.mark.parametrize("other", (False, True))
@pytest.mark.parametrize("enabled", (False, True))
def test_three_way_numeric_audit_and_public_release(early, other, enabled):
    request, problem, _, _, runtime, state, audit, core = numeric_case(early=early, other=other, enabled=enabled)
    assert audit.report.integrity_passed
    assert audit.report.writeback_blocking_violation_count == int(early and enabled)
    assert audit.report.passed is (not other and not (early and enabled))
    assert core.diagnostic_candidate.search_evaluation.quality_key == state.current_evaluation.quality_key
    if early and enabled:
        assert core.status is SolveStatus.COMPLETE_NOT_PUBLISHABLE and core.release is None
        return
    assert core.status is (SolveStatus.PUBLISHABLE_WITH_VIOLATIONS if other else SolveStatus.SUCCESS)
    assert core.release.audited_evaluation == audit.audited_evaluation
    draft = assemble_draft_scheduling_result(request, core, make_manifest(request, core))
    report = audit_result_contract(draft, request, problem, core, runtime)
    assert report.passed
    result = seal_scheduling_result(draft, report, runtime)
    assert result.release.evaluation.violations == audit.audited_evaluation.violations
    assert result.release.resource_facts is audit.resource_facts
    assert not result.confirmation_required


@pytest.mark.parametrize("count", (None, 0))
def test_missing_or_forged_zero_blocking_count_never_releases(count):
    request, problem, ruleset, _, runtime, state, audit, _ = numeric_case(early=True)
    object.__setattr__(audit.report, "writeback_blocking_violation_count", count)
    object.__setattr__(audit.report, "report_fingerprint", report_identity(
        (problem, ruleset), state, audit))
    core = solver.build_core_solver_result(problem, ruleset, request.policy, runtime,
        state=state, metrics=SolveMetrics(final_chain_count=1), audit=audit)
    assert core.status is SolveStatus.FAILED and core.release is None
    assert any(i.code == "core_audit_writeback_binding_mismatch" for i in core.issues)


def test_independent_recompute_mismatch_blocks_even_with_business_findings():
    request, problem, ruleset, numeric, runtime, state, _, _ = numeric_case()
    quality = numeric.evaluation.quality_key.copy()
    quality[-1] += 1
    numeric.evaluation = replace(numeric.evaluation, quality_key=readonly(quality, np.int64))
    audit = audit_numeric_core_without_search_cache(numeric, problem, ruleset, request.delivery_timing, runtime)
    assert not audit.report.integrity_passed
    assert audit.report.writeback_blocking_violation_count is None
    assert any(i.code in {"prohibited_rule_violation", "unapproved_final_deviation"} for i in audit.issues)
    core = solver.build_core_solver_result(problem, ruleset, request.policy, runtime,
        state=state, metrics=SolveMetrics(final_chain_count=1), audit=audit)
    assert core.status is SolveStatus.FAILED and core.release is None


@pytest.mark.parametrize("damage", ("missing", "duplicate", "weight", "source", "physical"))
def test_structural_damage_and_business_violation_never_release(damage):
    active = replace(rules(), rules=(weight_rule(minimum="400", maximum="500"),))
    args, options = audited_case(nodes=(node("a", weight="110"), node("b", weight="110")), rule_set=active)
    a, b = args[0].nodes
    nodes = {
        "missing": (a,), "duplicate": (a, b, a),
        "weight": (replace(a, weight=Decimal(150)), b),
        "source": (replace(a, source_order_id="unknown-source"), b),
        "physical": (replace(a, width=Decimal(999)), b),
    }[damage]
    chain = Chain("damaged", (a, b), "period")
    plan = SchedulePlan((chain,))
    # Deliberately bypass the value carrier to verify the independent audit boundary.
    object.__setattr__(chain, "nodes", nodes)
    state = SearchState(plan, evaluate_plan(plan, active, evaluation_context(args[0])))
    audit = final_audit.audit_core_without_search_cache(CoreCandidateSnapshot(plan, state.current_evaluation),
                                                       args[0], active, args[-1])
    core = solver.build_core_solver_result(*args, state=state, metrics=SolveMetrics(final_chain_count=1), audit=audit)
    assert not core.core_audit.integrity_passed and core.release is None
    assert core.status is SolveStatus.FAILED
    assert any(i.code in {"prohibited_rule_violation", "unapproved_final_deviation"} for i in audit.issues)


@pytest.mark.parametrize("reason", (SearchStopReason.USER_CANCELLED, SearchStopReason.SYSTEM_ERROR,
                                    SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED))
def test_terminal_failure_with_business_findings_does_not_release(reason):
    request, problem, ruleset, _, runtime, state, audit, _ = numeric_case()
    runtime.stop_reason = reason
    result = solver.build_core_solver_result(problem, ruleset, request.policy, runtime,
        state=state, metrics=SolveMetrics(final_chain_count=1), audit=audit)
    assert result.release is None and result.stop_reason is reason


@pytest.mark.parametrize("code,phase", (("unknown_error", DiagnosticPhase.CORE_AUDIT),
    ("prohibited_rule_violation", DiagnosticPhase.RESULT_ASSEMBLY)))
def test_only_known_core_business_findings_can_be_nonfatal(code, phase):
    request, problem, ruleset, _, runtime, state, audit, _ = numeric_case()
    result = solver.build_core_solver_result(problem, ruleset, request.policy, runtime,
        state=state, metrics=SolveMetrics(final_chain_count=1), audit=audit,
        issues=(DiagnosticIssue(code, phase, None, None, "error", DiagnosticSeverity.ERROR),))
    assert result.release is None and result.status is SolveStatus.FAILED
