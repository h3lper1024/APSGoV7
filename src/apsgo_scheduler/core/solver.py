"""One ordered core solve and one audited, time-bounded release boundary."""

import logging
from dataclasses import fields, replace
from decimal import Decimal
from time import perf_counter

from .budget import SolveRuntimeBudget
from .chain_order import chain_order_objective_index, refinement_admissible
from .compatibility import RuleEdgeDecisionCache, _finite_projection, build_construction_dag
from .contracts import (
    AuditedCoreRelease,
    CoreAuditReport,
    CoreAuditStatus,
    CoreCandidateSnapshot,
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    SearchStopReason,
    SolveMetrics,
    SolverPolicy,
    SolverResult,
    SolveStatus,
    fingerprint,
    contract_values,
    freeze_tuple,
    sum_weights,
)
from .controlled_split import run_controlled_order_split
from .final_audit import CoreAuditOutcome, audit_core_without_search_cache
from .initial_solution import construct_initial_plan
from .model import Node, SchedulingProblem, SearchState
from .neighborhoods import AcceptedMoveTrace, SearchContext, run_local_search
from .path_cover import minimum_path_cover
from .process_logging import audit_status, emit, stop_status
from .rules.base import RuleEvaluationContext
from .rules.concrete import WEIGHT_EPSILON, ChainWeightRangeRule
from .rules.rule_set import ProcessRuleSet
from .virtual_material import VirtualFactory
from .width_optimization import run_width_optimization

logger = logging.getLogger(__name__)


def _values(value, excluded=()):
    return contract_values(value, excluded)


def _issue(code, message, *, phase=DiagnosticPhase.CONSTRUCTION, path=None, subject=None):
    return DiagnosticIssue(code, phase, path, subject, message, DiagnosticSeverity.ERROR)


def _require_arguments(problem, rule_set, policy, runtime):
    for value, expected in (
        (problem, SchedulingProblem),
        (rule_set, ProcessRuleSet),
        (policy, SolverPolicy),
        (runtime, SolveRuntimeBudget),
    ):
        if not isinstance(value, expected):
            raise ValueError(f"core solve requires {expected.__name__}")


def _problem_fingerprint(problem):
    return fingerprint(_values(problem, ("problem_id", "input_fingerprint")))


def _identity_issues(problem, rule_set, policy, runtime):
    issues = []
    if _problem_fingerprint(problem) != problem.input_fingerprint:
        issues.append(
            _issue(
                "problem_fingerprint_mismatch",
                "问题指纹与实际标准化内容不一致。",
                path="input_fingerprint",
            )
        )
    for name in ("product_line_code", "process_code", "scenario"):
        if getattr(problem, name) != getattr(rule_set, name):
            issues.append(
                _issue("rule_set_domain_mismatch", "问题与规则集的领域身份不一致。", path=name)
            )
    try:
        replace(policy)
        replace(runtime)
        expected = SolveRuntimeBudget.from_policy(
            policy, runtime.started_at_monotonic, runtime.cancellation, clock=runtime.clock
        )
        for name in (
            "candidate_check_limit",
            "search_deadline_monotonic",
            "final_deadline_monotonic",
        ):
            if getattr(runtime, name) != getattr(expected, name):
                issues.append(
                    _issue("runtime_policy_mismatch", "共享预算与策略或原始起点不一致。", path=name)
                )
    except ValueError as error:
        issues.append(
            _issue("invalid_solver_policy_or_runtime", f"策略或预算不合法：{error}", path="policy")
        )
    return issues


def _material_issues(problem, rule_set, runtime):
    issues = []
    required = tuple(
        dict.fromkeys(name for rule in rule_set.rules for name in rule.required_fields())
    )
    prototype_required = tuple(
        dict.fromkeys(
            name
            for rule in rule_set.rules
            for name in rule.required_fields()
            if name in ("width", "thickness")
        )
    )
    node_fields = {part.name for part in fields(Node)} - {"rule_attributes"}
    maximum = next(
        (
            sum_weights((rule.parameters["max_weight"], WEIGHT_EPSILON))
            for rule in rule_set.rules
            if isinstance(rule, ChainWeightRangeRule)
        ),
        None,
    )
    for collection, needed, identity in (
        (problem.nodes, required, "node_id"),
        (problem.virtual_prototypes, prototype_required, "prototype_id"),
    ):
        for item in collection:
            if not runtime.allows_search():
                return issues
            subject = getattr(item, identity)
            for name in needed:
                value = (
                    getattr(item, name) if name in node_fields else item.rule_attributes.get(name)
                )
                if value is None or (isinstance(value, str) and not value.strip()):
                    issues.append(
                        _issue(
                            "missing_required_field",
                            f"启用规则要求字段 {name}。",
                            path=name,
                            subject=subject,
                        )
                    )
            physical = ("width", "thickness", "min_temperature", "max_temperature")
            for name in (*physical, "weight") if identity == "node_id" else physical:
                try:
                    _finite_projection(getattr(item, name), name)
                except ValueError as error:
                    issues.append(
                        _issue(
                            "non_finite_computation_value",
                            f"字段超出计算能力：{error}",
                            path=name,
                            subject=subject,
                        )
                    )
            if identity == "node_id":
                if maximum is not None and item.weight > maximum:
                    issues.append(
                        _issue(
                            "atomic_node_above_chain_maximum",
                            "原子订单超过启用链重上限及允许误差。",
                            path="weight",
                            subject=subject,
                        )
                    )
                try:
                    rule_set.construction_priority(item)
                except ValueError as error:
                    issues.append(
                        _issue(
                            "invalid_construction_priority",
                            f"节点优先级不合法：{error}",
                            path="rule_attributes",
                            subject=subject,
                        )
                    )
    return issues


def _not_run_audit():
    values = dict(
        status=CoreAuditStatus.NOT_RUN,
        passed=False,
        audited_evaluation_fingerprint=None,
        invariant_failure_codes=(),
        action_authorization_failure_codes=(),
        derived_resource_fingerprint=None,
        audited_split_count=None,
        audited_same_period_split_count=None,
        audited_future_borrow_return_count=None,
        search_evaluation_matches=None,
    )
    return CoreAuditReport(**values, report_fingerprint=fingerprint(values))


def _audit_binding_issues(candidate, problem, rule_set, state, metrics, audit):
    report, evaluation, facts = audit.report, audit.audited_evaluation, audit.resource_facts
    issues = []
    payload = dict(
        problem_fingerprint=problem.input_fingerprint,
        rule_set_fingerprint=rule_set.fingerprint,
        plan_fingerprint=fingerprint(candidate.plan),
        search_evaluation_fingerprint=fingerprint(candidate.search_evaluation),
        report=_values(report, ("report_fingerprint",)),
        issues=audit.issues,
    )
    if report.report_fingerprint != fingerprint(payload):
        issues.append(
            _issue(
                "core_audit_binding_mismatch",
                "审计报告未绑定当前问题、方案和搜索评价。",
                phase=DiagnosticPhase.CORE_AUDIT,
            )
        )
    state_counts = (
        state.split_sequence,
        state.accepted_same_period_split_count,
        state.accepted_future_borrow_return_count,
    )
    metric_counts = (
        metrics.accepted_split_count,
        metrics.accepted_same_period_split_count,
        metrics.accepted_future_borrow_return_count,
    )
    audit_counts = (
        report.audited_split_count,
        report.audited_same_period_split_count,
        report.audited_future_borrow_return_count,
    )
    if (
        state_counts != metric_counts
        or state_counts != audit_counts
        or any(value is None for value in audit_counts)
    ):
        issues.append(
            _issue(
                "split_count_mismatch",
                "搜索状态、运行指标和独立审计的拆单计数不一致。",
                phase=DiagnosticPhase.CORE_AUDIT,
            )
        )
    if evaluation is None or report.audited_evaluation_fingerprint != fingerprint(evaluation):
        issues.append(
            _issue(
                "audited_evaluation_identity_mismatch",
                "独立审计评价身份不一致或缺失。",
                phase=DiagnosticPhase.CORE_AUDIT,
            )
        )
    if facts is not None and (
        facts.facts_fingerprint != report.derived_resource_fingerprint
        or facts.facts_fingerprint != fingerprint(_values(facts, ("facts_fingerprint",)))
    ):
        issues.append(
            _issue(
                "audited_resource_identity_mismatch",
                "资源事实身份或完整内容不一致。",
                phase=DiagnosticPhase.CORE_AUDIT,
            )
        )
    if report.passed and facts is None:
        issues.append(
            _issue(
                "audited_resource_missing",
                "通过的审计缺少唯一资源事实。",
                phase=DiagnosticPhase.CORE_AUDIT,
            )
        )
    if (
        report.invariant_failure_codes
        or report.action_authorization_failure_codes
        or not report.search_evaluation_matches
    ):
        issues.append(
            _issue(
                "core_audit_integrity_failed",
                "结构、动作授权或搜索评价一致性审计失败。",
                phase=DiagnosticPhase.CORE_AUDIT,
            )
        )
    return issues


def _finalization_allowed(runtime, issues):
    try:
        # Terminal failures cannot reopen finalization, but an explicit cancellation still wins.
        if (
            runtime.stop_reason
            in {
                SearchStopReason.INPUT_INVALID,
                SearchStopReason.SYSTEM_ERROR,
                SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
            }
            and runtime._cancellation_is_requested()
        ):
            runtime.stop_reason = SearchStopReason.USER_CANCELLED
        return runtime.allows_finalization()
    except Exception as error:
        runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
        emit(
            logger,
            "solver_finalization_exception",
            status="error",
            exception_type=type(error).__name__,
            level=logging.ERROR,
            exc_info=True,
        )
        issues.append(
            _issue("runtime_check_error", f"运行边界检查失败：{type(error).__name__}: {error}")
        )
        return False


def build_core_solver_result(
    problem: SchedulingProblem,
    rule_set: ProcessRuleSet,
    policy: SolverPolicy,
    runtime: SolveRuntimeBudget,
    *,
    state: SearchState | None,
    metrics: SolveMetrics,
    trace: tuple[AcceptedMoveTrace, ...] = (),
    audit: CoreAuditOutcome | None = None,
    issues: tuple[DiagnosticIssue, ...] = (),
) -> SolverResult:
    """Bind the completed audit to this exact accepted state before signing any release."""
    _require_arguments(problem, rule_set, policy, runtime)
    if state is not None and not isinstance(state, SearchState):
        raise ValueError("state must be SearchState or None")
    if not isinstance(metrics, SolveMetrics) or (
        audit is not None and not isinstance(audit, CoreAuditOutcome)
    ):
        raise ValueError("result construction requires SolveMetrics and optional CoreAuditOutcome")
    trace = freeze_tuple(trace, AcceptedMoveTrace, "trace")
    found = list(freeze_tuple(issues, DiagnosticIssue, "issues"))
    identity_issues = _identity_issues(problem, rule_set, policy, runtime)
    found.extend(item for item in identity_issues if item not in found)
    if identity_issues and runtime.stop_reason is not SearchStopReason.USER_CANCELLED:
        runtime.stop_reason = SearchStopReason.INPUT_INVALID
    if runtime.stop_reason is None:
        runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
        found.append(
            _issue("search_stop_reason_missing", "求解阶段未提供停止原因，不能声明自然完成。")
        )
    candidate = (
        None
        if state is None
        else CoreCandidateSnapshot(state.current_plan, state.current_evaluation)
    )
    report = _not_run_audit() if audit is None else audit.report
    if audit is not None:
        found.extend(audit.issues)
    terminal = {
        SearchStopReason.INPUT_INVALID,
        SearchStopReason.SYSTEM_ERROR,
        SearchStopReason.USER_CANCELLED,
        SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
    }
    if runtime.stop_reason not in terminal and candidate is not None:
        if audit is None or report.status is CoreAuditStatus.NOT_RUN:
            runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
            found.append(
                _issue(
                    "core_audit_missing",
                    "完整候选没有完成独立审计。",
                    phase=DiagnosticPhase.CORE_AUDIT,
                )
            )
        elif report.status is CoreAuditStatus.CANCELLED:
            runtime.stop_reason = SearchStopReason.USER_CANCELLED
        elif report.status is CoreAuditStatus.TIME_LIMIT:
            runtime.stop_reason = SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
        elif report.status is CoreAuditStatus.ERROR:
            runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
        else:
            binding = _audit_binding_issues(candidate, problem, rule_set, state, metrics, audit)
            found.extend(binding)
            if binding:
                runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
    if metrics.candidate_check_count != runtime.candidate_check_count:
        found.append(_issue("candidate_count_mismatch", "运行指标与共享候选检查计数不一致。"))
        if runtime.stop_reason not in terminal:
            runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
    business_codes = {"prohibited_rule_violation", "unapproved_final_deviation"}
    if runtime.stop_reason not in terminal and any(
        item.severity is DiagnosticSeverity.ERROR and item.code not in business_codes
        for item in found
    ):
        runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
    allowed = _finalization_allowed(runtime, found)

    def status():
        if runtime.stop_reason is SearchStopReason.USER_CANCELLED:
            return SolveStatus.CANCELLED
        if runtime.stop_reason in {SearchStopReason.INPUT_INVALID, SearchStopReason.SYSTEM_ERROR}:
            return SolveStatus.FAILED
        if candidate is None:
            return SolveStatus.NO_COMPLETE_PLAN
        if (
            runtime.stop_reason is SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
            or not report.passed
        ):
            return SolveStatus.COMPLETE_NOT_PUBLISHABLE
        return (
            SolveStatus.SUCCESS
            if not audit.audited_evaluation.violations
            else SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION
        )

    release = None
    result_status = status()
    if allowed and result_status in {
        SolveStatus.SUCCESS,
        SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION,
    }:
        identities = dict(
            plan_fingerprint=fingerprint(candidate.plan),
            evaluation_fingerprint=report.audited_evaluation_fingerprint,
            resource_fingerprint=report.derived_resource_fingerprint,
            core_audit_fingerprint=report.report_fingerprint,
        )
        release = AuditedCoreRelease(
            candidate.plan,
            audit.audited_evaluation,
            audit.resource_facts,
            **identities,
            release_fingerprint=fingerprint(identities),
        )

    def result(value, signed_release):
        ordered_issues = tuple(
            sorted(found, key=lambda issue: tuple(DiagnosticPhase).index(issue.phase))
        )
        values = dict(
            status=value,
            stop_reason=runtime.stop_reason,
            diagnostic_candidate=candidate,
            release=signed_release,
            core_audit=report,
            metrics=metrics,
            issues=ordered_issues,
            trace=trace,
            problem_fingerprint=_problem_fingerprint(problem),
            rule_set_fingerprint=rule_set.fingerprint,
            policy_fingerprint=fingerprint(policy),
        )
        payload = {**values, "metrics": _values(metrics, ("stage_duration_seconds",))}
        return SolverResult(**values, core_result_fingerprint=fingerprint(payload))

    completed = result(result_status, release)
    # Hashing and value construction are part of the same bounded release operation.
    if not _finalization_allowed(runtime, found):
        return result(status(), None)
    return completed


def solve(
    problem: SchedulingProblem,
    rule_set: ProcessRuleSet,
    policy: SolverPolicy,
    runtime: SolveRuntimeBudget,
) -> SolverResult:
    """Run the fixed phases once, preserving the accepted state on every exit path."""
    _require_arguments(problem, rule_set, policy, runtime)
    state, context, audit = None, None, None
    metrics, issues, durations = SolveMetrics(), [], {}
    phase = "input_validation"
    observed_phases = set()

    def log_phase(event, *, name=None, **details):
        name = phase if name is None else name
        observed_phases.add(name)
        evaluation = None if state is None else state.current_evaluation
        emit(
            logger,
            event,
            stage=name,
            stop_reason=None if runtime.stop_reason is None else runtime.stop_reason.value,
            candidate_check_count=runtime.candidate_check_count,
            complete_candidate_evaluation_count=(
                0 if context is None else context.complete_candidate_evaluation_count
            ),
            accepted_move_count=0 if state is None else state.accepted_move_count,
            chain_count=None if state is None else len(state.current_plan.chains),
            quality=None
            if evaluation is None
            else tuple(
                (criterion.metric_key, value)
                for criterion, value in zip(rule_set.quality_spec, evaluation.quality_key)
            ),
            **details,
        )

    def finish():
        nonlocal metrics
        for name in (
            "input_validation",
            "construction_graph",
            "minimum_path_cover",
            "initial_solution",
            "local_search",
            "controlled_split_and_replay",
            "width_optimization",
            "core_audit",
        ):
            if name not in observed_phases:
                log_phase(
                    "solver_stage_skipped", name=name, status="skipped", reason="prior_stage_exit"
                )
        values = dict(
            candidate_check_count=runtime.candidate_check_count, stage_duration_seconds=durations
        )
        if context is not None:
            values["complete_candidate_evaluation_count"] = (
                context.complete_candidate_evaluation_count
            )
        if state is not None:
            values.update(
                accepted_move_count=state.accepted_move_count,
                accepted_split_count=state.split_sequence,
                accepted_same_period_split_count=state.accepted_same_period_split_count,
                accepted_future_borrow_return_count=state.accepted_future_borrow_return_count,
                final_chain_count=len(state.current_plan.chains),
            )
        try:
            metrics = replace(metrics, **values)
        except ValueError as error:
            runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
            emit(
                logger,
                "solver_counter_exception",
                status="error",
                exception_type=type(error).__name__,
                level=logging.ERROR,
                exc_info=True,
            )
            issues.append(
                _issue(
                    "solver_counter_invalid",
                    f"求解计数不一致：{error}",
                    phase=DiagnosticPhase.SEARCH,
                )
            )
        return build_core_solver_result(
            problem,
            rule_set,
            policy,
            runtime,
            state=state,
            metrics=metrics,
            trace=() if context is None else context.accepted_move_traces,
            audit=audit,
            issues=tuple(issues),
        )

    started = perf_counter()
    try:
        log_phase("solver_stage_started")
        issues.extend(_identity_issues(problem, rule_set, policy, runtime))
        if not issues:
            issues.extend(_material_issues(problem, rule_set, runtime))
        durations[phase] = Decimal(str(perf_counter() - started))
        if issues:
            if runtime.stop_reason is not SearchStopReason.USER_CANCELLED:
                runtime.stop_reason = SearchStopReason.INPUT_INVALID
            log_phase(
                "solver_stage_finished",
                stage_seconds=durations[phase],
                status=stop_status(runtime.stop_reason),
                issue_count=len(issues),
            )
            return finish()
        if not runtime.allows_search():
            log_phase(
                "solver_stage_finished",
                stage_seconds=durations[phase],
                status=stop_status(runtime.stop_reason),
            )
            return finish()
        log_phase("solver_stage_finished", stage_seconds=durations[phase], status="completed")
        rule_context = RuleEvaluationContext(
            problem.period_order,
            {period: index for index, period in enumerate(problem.period_order)},
            tuple(item.prototype_id for item in problem.virtual_prototypes),
            problem.delivery_timing,
        )
        cache = RuleEdgeDecisionCache(problem, rule_set, rule_context, policy.numeric_semantics_key)
        factory = VirtualFactory(cache, runtime)
        phase, started = "construction_graph", perf_counter()
        log_phase("solver_stage_started")
        graph = build_construction_dag(problem, cache, runtime, seed=policy.seed)
        durations[phase] = Decimal(str(perf_counter() - started))
        metrics = replace(
            metrics,
            graph_edge_check_count=graph.checked_edge_count,
            graph_allowed_edge_count=graph.allowed_edge_count,
        )
        log_phase(
            "solver_stage_finished",
            stage_seconds=durations[phase],
            status="completed" if graph.complete else stop_status(runtime.stop_reason),
            complete=graph.complete,
            graph_edge_check_count=graph.checked_edge_count,
            graph_allowed_edge_count=graph.allowed_edge_count,
        )
        if not graph.complete:
            return finish()
        phase, started = "minimum_path_cover", perf_counter()
        log_phase("solver_stage_started")
        cover = minimum_path_cover(graph, runtime)
        durations[phase] = Decimal(str(perf_counter() - started))
        metrics = replace(
            metrics, matching_edge_count=cover.matched_edge_count, path_count=cover.path_count
        )
        log_phase(
            "solver_stage_finished",
            stage_seconds=durations[phase],
            status="completed" if cover.complete else stop_status(runtime.stop_reason),
            complete=cover.complete,
            matching_edge_count=cover.matched_edge_count,
            path_count=cover.path_count,
        )
        if not cover.complete:
            return finish()
        phase, started = "initial_solution", perf_counter()
        log_phase("solver_stage_started")
        initial = construct_initial_plan(problem, graph, cover, cache, runtime)
        durations[phase] = Decimal(str(perf_counter() - started))
        if not initial.complete:
            log_phase(
                "solver_stage_finished",
                stage_seconds=durations[phase],
                status=stop_status(runtime.stop_reason),
                complete=False,
            )
            return finish()
        state = SearchState(initial.candidate.plan, initial.candidate.search_evaluation)
        metrics = replace(metrics, initial_chain_count=len(state.current_plan.chains))
        log_phase(
            "solver_stage_finished",
            stage_seconds=durations[phase],
            status="completed",
            complete=True,
        )
        context = SearchContext(factory, policy)
        phase, started = "local_search", perf_counter()
        log_phase("solver_stage_started")
        run_local_search(state, context)
        durations[phase] = Decimal(str(perf_counter() - started))
        log_phase(
            "solver_stage_finished",
            stage_seconds=durations[phase],
            status=stop_status(runtime.stop_reason),
        )
        phase, started = "controlled_split_and_replay", perf_counter()
        split_stopped = runtime.stop_reason not in (None, SearchStopReason.LOCAL_SEARCH_COMPLETE)
        log_phase(
            "solver_stage_skipped" if split_stopped else "solver_stage_started",
            **({"status": "skipped", "reason": "search_already_stopped"} if split_stopped else {}),
        )
        # This stage owns the optional single replay of the original local search.
        run_controlled_order_split(state, context)
        durations[phase] = Decimal(str(perf_counter() - started))
        if not split_stopped:
            log_phase(
                "solver_stage_finished",
                stage_seconds=durations[phase],
                status=stop_status(runtime.stop_reason),
            )
        width_objective = chain_order_objective_index(rule_set)
        if (
            width_objective is not None
            and refinement_admissible(state.current_evaluation, rule_set)
            and runtime.stop_reason in (None, SearchStopReason.LOCAL_SEARCH_COMPLETE)
        ):
            phase, started = "width_optimization", perf_counter()
            log_phase("solver_stage_started")
            run_width_optimization(state, context)
            durations[phase] = Decimal(str(perf_counter() - started))
            log_phase(
                "solver_stage_finished",
                stage_seconds=durations[phase],
                status=stop_status(runtime.stop_reason),
            )
        else:
            log_phase(
                "solver_stage_skipped",
                name="width_optimization",
                status="skipped",
                reason="objective_not_enabled"
                if width_objective is None
                else "candidate_has_violations"
                if state.current_evaluation.violations
                else "search_already_stopped",
            )
        phase, started = "core_audit", perf_counter()
        log_phase("solver_stage_started")
        snapshot = CoreCandidateSnapshot(state.current_plan, state.current_evaluation)
        audit = audit_core_without_search_cache(snapshot, problem, rule_set, runtime)
        durations[phase] = Decimal(str(perf_counter() - started))
        log_phase(
            "solver_stage_finished",
            stage_seconds=durations[phase],
            status=audit_status(audit.report),
            audit_status=audit.report.status.value,
            audit_passed=audit.report.passed,
            issue_count=len(audit.issues),
        )
    except Exception as error:
        durations[phase] = Decimal(str(perf_counter() - started))
        runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
        log_phase(
            "solver_stage_failed",
            stage_seconds=durations[phase],
            status="error",
            exception_type=type(error).__name__,
            level=logging.ERROR,
            exc_info=True,
        )
        issues.append(
            _issue(
                "core_solver_error",
                f"求解阶段 {phase} 异常：{type(error).__name__}: {error}",
                phase=DiagnosticPhase.CORE_AUDIT
                if phase == "core_audit"
                else DiagnosticPhase.SEARCH
                if context is not None
                else DiagnosticPhase.CONSTRUCTION,
            )
        )
    return finish()
