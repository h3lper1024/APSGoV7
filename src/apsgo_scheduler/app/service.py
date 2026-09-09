"""The sole public request-to-result service, with one entry-time budget."""

import logging
from dataclasses import fields, replace
from decimal import Decimal
from time import monotonic, perf_counter

from .. import core
from ..api.request import SchedulingRequest, fingerprint_public_request
from ..api.result import ResultAuditReport, ResultAuditStatus, RunManifest, SchedulingResult
from ..core.budget import SolveRuntimeBudget, _finite_time
from ..core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    SearchStopReason,
    SolveMetrics,
    SolverResult,
    SolveStatus,
    fingerprint,
)
from ..core.model import SchedulingProblem
from ..core.process_logging import audit_status, emit, stop_status
from ..core.rules.rule_set import ProcessRuleSet
from ..core.solver import _not_run_audit
from .contract_audit import audit_result_contract
from .input_normalizer import InputNormalizationError, normalize_input
from .result_assembler import (
    DraftSchedulingResult,
    assemble_draft_scheduling_result,
    seal_scheduling_result,
)
from .rule_set_loader import RuleSetLoadError, load_rule_set

logger = logging.getLogger(__name__)


def _unfinished_result_audit(
    status=ResultAuditStatus.NOT_RUN, *, failure_codes=(), draft_fingerprint=None
):
    values = dict(
        status=status,
        passed=False,
        failure_codes=failure_codes,
        plan_fingerprint=None,
        resource_fingerprint=None,
        draft_fingerprint=draft_fingerprint,
    )
    return ResultAuditReport(**values, report_fingerprint=fingerprint(values))


def _issue(code, phase, message, *, path=None, severity=DiagnosticSeverity.ERROR):
    return DiagnosticIssue(code, phase, path, None, message, severity)


def solve_request(request: SchedulingRequest, cancellation=None) -> SchedulingResult:
    """Freeze a typed request, solve once, and publish only after both audits pass."""
    try:
        started_at = monotonic()
    except Exception as error:
        emit(
            logger,
            "solver_entry_clock_exception",
            level=logging.ERROR,
            exc_info=True,
            exception_type=type(error).__name__,
        )
        started_at, clock_error = None, error
    else:
        clock_error = None
    if not isinstance(request, SchedulingRequest):
        raise ValueError("request must be SchedulingRequest")
    if cancellation is not None and not callable(getattr(cancellation, "is_cancelled", None)):
        raise ValueError("cancellation must provide a callable is_cancelled")
    request_fingerprint = fingerprint_public_request(request)
    runtime = rule_set = problem = core_result = None
    policy_fingerprint = None
    report = _unfinished_result_audit()
    issues, durations = [], {}
    stop_reason = None
    phase = DiagnosticPhase.REQUEST_VALIDATION
    stage, stage_started = "request_preparation", perf_counter()
    observed_stages = set()

    def log_stage(event, *, name=None, **details):
        name = stage if name is None else name
        observed_stages.add(name)
        emit(
            logger,
            event,
            stage=name,
            request_id=request.request_id,
            stop_reason=None if stop_reason is None else stop_reason.value,
            **details,
        )

    def measure(*, event="solver_stage_finished", status="completed", **details):
        durations[stage] = Decimal(str(perf_counter() - stage_started))
        log_stage(event, stage_seconds=durations[stage], status=status, **details)

    def boundary(*, finalizing=False):
        nonlocal stop_reason
        if runtime is None:
            if cancellation is not None:
                cancelled = cancellation.is_cancelled()
                if type(cancelled) is not bool:
                    raise ValueError("is_cancelled must return bool")
                if cancelled:
                    stop_reason = SearchStopReason.USER_CANCELLED
                    return False
            return True
        if (
            finalizing
            and runtime.stop_reason
            in {
                SearchStopReason.INPUT_INVALID,
                SearchStopReason.SYSTEM_ERROR,
                SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
            }
            and runtime._cancellation_is_requested()
        ):
            runtime.stop_reason = SearchStopReason.USER_CANCELLED
        allowed = runtime.allows_finalization() if finalizing else runtime.allows_search()
        stop_reason = runtime.stop_reason
        return allowed

    def ordered_issues():
        return tuple(sorted(issues, key=lambda item: tuple(DiagnosticPhase).index(item.phase)))

    def manifest():
        metrics = SolveMetrics() if core_result is None else core_result.metrics
        return RunManifest(
            algorithm_version="path-cover-local-search-v1",
            code_revision="unversioned",
            request_fingerprint=request_fingerprint,
            problem_fingerprint=None if problem is None else problem.input_fingerprint,
            rule_set_fingerprint=None if rule_set is None else rule_set.fingerprint,
            policy_fingerprint=policy_fingerprint,
            stop_reason=stop_reason,
            search_was_truncated=stop_reason.search_was_truncated,
            optimality_proven=False,
            counters={
                item.name: getattr(metrics, item.name)
                for item in fields(metrics)
                if item.name != "stage_duration_seconds"
            },
            diagnostic_codes=tuple(item.code for item in ordered_issues()),
            stage_duration_seconds={**metrics.stage_duration_seconds, **durations},
            trace_fingerprint=fingerprint(() if core_result is None else core_result.trace),
        )

    def unavailable(*, check_boundary=True):
        nonlocal stop_reason
        if check_boundary:
            boundary(finalizing=True)
        if stop_reason is None:
            stop_reason = SearchStopReason.SYSTEM_ERROR
            issues.append(_issue("service_stop_reason_missing", phase, "服务未取得明确停止原因。"))
        for name in (
            "request_preparation",
            "rule_loading",
            "input_normalization",
            "core_solve",
            "result_assembly",
            "result_contract_audit",
            "result_sealing",
        ):
            if name not in observed_stages:
                log_stage(
                    "solver_stage_skipped", name=name, status="skipped", reason="prior_stage_exit"
                )
        candidate = None if core_result is None else core_result.diagnostic_candidate
        if stop_reason is SearchStopReason.USER_CANCELLED:
            status = SolveStatus.CANCELLED
        elif stop_reason in {SearchStopReason.INPUT_INVALID, SearchStopReason.SYSTEM_ERROR}:
            status = SolveStatus.FAILED
        elif candidate is None:
            status = SolveStatus.NO_COMPLETE_PLAN
        else:
            status = SolveStatus.COMPLETE_NOT_PUBLISHABLE
        if stop_reason in {
            SearchStopReason.USER_CANCELLED,
            SearchStopReason.SEARCH_TIME_LIMIT_REACHED,
            SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
        } and not any(item.code == stop_reason.value for item in issues):
            issues.append(
                _issue(
                    stop_reason.value,
                    phase,
                    "用户已取消排产。"
                    if stop_reason is SearchStopReason.USER_CANCELLED
                    else "排产时间预算已耗尽，不能签发结果。",
                    severity=DiagnosticSeverity.WARNING,
                )
            )

        def result():
            return SchedulingResult(
                contract_version=request.contract_version,
                request_id=request.request_id,
                status=status,
                stop_reason=stop_reason,
                release=None,
                diagnostic_candidate=candidate,
                core_audit=_not_run_audit() if core_result is None else core_result.core_audit,
                audit_report=report,
                issues=ordered_issues(),
                run_manifest=manifest(),
            )

        completed = result()
        if check_boundary:
            previous = stop_reason
            boundary(finalizing=True)
            if stop_reason is not previous:
                return unavailable(check_boundary=False)
        return completed

    log_stage("solver_stage_started")
    try:
        if clock_error is not None:
            raise clock_error
        _finite_time(started_at, "monotonic()")
        try:
            replace(request.policy)
            runtime = SolveRuntimeBudget.from_policy(
                request.policy, started_at, cancellation, clock=monotonic
            )
            policy_fingerprint = fingerprint(request.policy)
        except ValueError as error:
            issues.append(
                _issue(
                    "invalid_solver_policy",
                    phase,
                    f"求解策略或截止时间不合法：{error}",
                    path="policy",
                )
            )
        measure(status="failed" if issues else "completed", issue_count=len(issues))
        if not boundary():
            return unavailable()
        phase, stage, stage_started = DiagnosticPhase.RULE_LOADING, "rule_loading", perf_counter()
        log_stage("solver_stage_started")
        try:
            loaded = load_rule_set(request.rule_set_spec)
            if not isinstance(loaded, ProcessRuleSet):
                raise ValueError("load_rule_set must return ProcessRuleSet")
            rule_set = loaded
        except RuleSetLoadError:
            # The normalizer repeats signed loading and collects safe, normalized input errors.
            pass
        measure(status="failed" if rule_set is None else "completed")
        if not boundary():
            return unavailable()
        phase, stage, stage_started = (
            DiagnosticPhase.INPUT_NORMALIZATION,
            "input_normalization",
            perf_counter(),
        )
        log_stage("solver_stage_started")
        try:
            normalized = normalize_input(request, rule_set)
            if not isinstance(normalized, SchedulingProblem):
                raise ValueError("normalize_input must return SchedulingProblem")
            problem = normalized
        except InputNormalizationError as error:
            issues.extend(error.issues)
        measure(status="failed" if issues else "completed", issue_count=len(issues))
        if issues:
            stop_reason = SearchStopReason.INPUT_INVALID
            if runtime is not None:
                runtime.stop_reason = stop_reason
            return unavailable()
        if not boundary():
            return unavailable()
        phase, stage, stage_started = DiagnosticPhase.CONSTRUCTION, "core_solve", perf_counter()
        log_stage("solver_stage_started")
        solved = core.solve(problem, rule_set, request.policy, runtime)
        if not isinstance(solved, SolverResult):
            raise ValueError("core.solve must return SolverResult")
        core_result = solved
        issues.extend(core_result.issues)
        stop_reason = core_result.stop_reason
        measure(
            status=stop_status(stop_reason),
            result_status=core_result.status.value,
            publishable=core_result.release is not None,
        )
        if core_result.release is None or not boundary(finalizing=True):
            return unavailable()
        phase, stage, stage_started = (
            DiagnosticPhase.RESULT_ASSEMBLY,
            "result_assembly",
            perf_counter(),
        )
        log_stage("solver_stage_started")
        assembled = assemble_draft_scheduling_result(request, core_result, manifest())
        if not isinstance(assembled, DraftSchedulingResult):
            raise ValueError("assemble_draft_scheduling_result must return DraftSchedulingResult")
        draft = assembled
        measure()
        if not boundary(finalizing=True):
            return unavailable()
        phase, stage, stage_started = (
            DiagnosticPhase.RESULT_AUDIT,
            "result_contract_audit",
            perf_counter(),
        )
        log_stage("solver_stage_started")
        checked = audit_result_contract(draft, request, problem, core_result, runtime)
        if not isinstance(checked, ResultAuditReport):
            raise ValueError("audit_result_contract must return ResultAuditReport")
        report = checked
        elapsed = _finite_time(monotonic(), "monotonic()") - started_at
        if elapsed < 0:
            raise ValueError("monotonic() moved before the service entry time")
        durations["service_to_result_audit_seconds"] = Decimal(str(elapsed))
        measure(
            status=audit_status(report),
            audit_status=report.status.value,
            audit_passed=report.passed,
        )
        if not report.passed:
            issues.extend(
                _issue(code, phase, f"结果契约自检未通过：{code}。")
                for code in report.failure_codes
            )
            stop_reason = {
                ResultAuditStatus.CANCELLED: SearchStopReason.USER_CANCELLED,
                ResultAuditStatus.TIME_LIMIT: SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
            }.get(report.status, SearchStopReason.SYSTEM_ERROR)
            runtime.stop_reason = stop_reason
            return unavailable()
        if not boundary(finalizing=True):
            return unavailable()
        draft = replace(draft, run_manifest=manifest())
        phase, stage, stage_started = (
            DiagnosticPhase.RESULT_ASSEMBLY,
            "result_sealing",
            perf_counter(),
        )
        log_stage("solver_stage_started")
        completed = seal_scheduling_result(draft, report, runtime)
        if not isinstance(completed, SchedulingResult):
            raise ValueError("seal_scheduling_result must return SchedulingResult")
        measure()
        # Durations are observational; this replacement preserves all semantic fingerprints.
        completed = replace(
            completed,
            run_manifest=replace(
                completed.run_manifest,
                stage_duration_seconds={
                    **completed.run_manifest.stage_duration_seconds,
                    **durations,
                },
            ),
        )
        if not boundary(finalizing=True):
            issues[:] = completed.issues
            return unavailable()
        return completed
    except Exception as error:
        measure(
            event="solver_stage_failed",
            status="error",
            exception_type=type(error).__name__,
            level=logging.ERROR,
            exc_info=True,
        )
        stop_reason = SearchStopReason.SYSTEM_ERROR
        if runtime is not None:
            runtime.stop_reason = stop_reason
        if stage == "result_contract_audit" and report.status is ResultAuditStatus.NOT_RUN:
            report = _unfinished_result_audit(
                ResultAuditStatus.ERROR,
                failure_codes=("result_audit_exception",),
                draft_fingerprint=draft.draft_fingerprint,
            )
        issues.append(
            _issue("service_exception", phase, f"排产服务执行异常：{type(error).__name__}: {error}")
        )
        failed = unavailable(check_boundary=False)
        # A valid cancellation still wins over failure; never retry a failed clock here.
        if cancellation is not None:
            try:
                cancelled = cancellation.is_cancelled()
                if type(cancelled) is not bool:
                    raise ValueError("is_cancelled must return bool")
            except Exception:
                emit(
                    logger,
                    "solver_cancellation_exception",
                    level=logging.ERROR,
                    exc_info=True,
                    request_id=request.request_id,
                )
            else:
                if cancelled:
                    stop_reason = SearchStopReason.USER_CANCELLED
                    if runtime is not None:
                        runtime.stop_reason = stop_reason
                    return unavailable(check_boundary=False)
        return failed
