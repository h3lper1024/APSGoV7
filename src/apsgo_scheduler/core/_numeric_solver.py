"""Public core orchestration for the integer numeric search standard."""

import logging
from decimal import Decimal
from time import perf_counter

from ._numeric_audit import audit_numeric_core_without_search_cache
from ._numeric_boundary import (
    numeric_evaluation_to_domain,
    numeric_plan_to_domain,
    numeric_trace_to_domain,
)
from ._numeric_construction import (
    build_numeric_construction_graph,
    construct_numeric_initial_plan,
    numeric_minimum_path_cover,
)
from ._numeric_evaluation import NumericQualityProgram
from ._numeric_refinement import run_numeric_serial_search
from ._numeric_rules import NumericRuleProgram
from ._numeric_state import NumericTask
from ._numeric_units import NumericValueError, to_ticks
from .contracts import (
    ControlledSplitMode,
    DiagnosticPhase,
    SearchStopReason,
    SolveMetrics,
)
from .model import SearchState
from .process_logging import emit
from .solver import (
    _identity_issues,
    _issue,
    _material_issues,
    build_core_solver_result,
)

logger = logging.getLogger(__name__)


def solve_numeric(problem, rule_set, policy, runtime, timing_input):
    """Run one numeric request; incompatible declarations fail without legacy fallback."""
    state = numeric_state = audit = graph = cover = initial = None
    issues, durations = [], {}
    metrics = SolveMetrics()
    phase = "numeric_input_preparation"

    def measure(started):
        durations[phase] = Decimal(str(perf_counter() - started))

    def finish():
        nonlocal metrics
        split_modes = () if numeric_state is None else tuple(
            int(value) for value in numeric_state.task.split_groups.mode
        )
        values = dict(
            graph_edge_check_count=0 if graph is None else graph.checked_edge_count,
            graph_allowed_edge_count=0 if graph is None else graph.allowed_edge_count,
            matching_edge_count=0
            if cover is None
            else sum(int(value) >= 0 for value in cover.matching_successor),
            path_count=0 if cover is None else cover.path_count,
            initial_chain_count=0
            if initial is None or initial.plan is None
            else int(initial.plan.chain_ids.size),
            candidate_check_count=runtime.candidate_check_count,
            complete_candidate_evaluation_count=0
            if numeric_state is None
            else numeric_state.complete_candidate_evaluation_count,
            accepted_move_count=0
            if numeric_state is None
            else numeric_state.accepted_move_count,
            accepted_split_count=len(split_modes),
            accepted_same_period_split_count=sum(
                mode == tuple(ControlledSplitMode).index(ControlledSplitMode.SAME_PERIOD_SPLIT)
                for mode in split_modes
            ),
            accepted_future_borrow_return_count=sum(
                mode == tuple(ControlledSplitMode).index(ControlledSplitMode.FUTURE_BORROW_RETURN)
                for mode in split_modes
            ),
            final_chain_count=0 if numeric_state is None else int(numeric_state.plan.chain_ids.size),
            stage_duration_seconds=durations,
        )
        metrics = SolveMetrics(**values)
        trace = () if numeric_state is None else numeric_trace_to_domain(
            numeric_state.task, numeric_state.quality, numeric_state.accepted_moves
        )
        return build_core_solver_result(
            problem,
            rule_set,
            policy,
            runtime,
            state=state,
            metrics=metrics,
            trace=trace,
            audit=audit,
            issues=tuple(issues),
        )

    started = perf_counter()
    try:
        issues.extend(_identity_issues(problem, rule_set, policy, runtime))
        if not issues:
            issues.extend(_material_issues(problem, rule_set, runtime))
        if issues:
            runtime.stop_reason = SearchStopReason.INPUT_INVALID
            measure(started)
            return finish()
        task = NumericTask.build(problem, rule_set, timing_input)
        program = NumericRuleProgram.compile(task, rule_set)
        quality = NumericQualityProgram.compile(task, program, rule_set)
        measure(started)
        if not runtime.allows_search():
            return finish()

        phase, started = "numeric_construction_graph", perf_counter()
        graph = build_numeric_construction_graph(task, program, runtime, seed=policy.seed)
        measure(started)
        if not graph.complete:
            return finish()

        phase, started = "numeric_minimum_path_cover", perf_counter()
        cover = numeric_minimum_path_cover(graph, runtime)
        measure(started)
        if not cover.complete:
            return finish()

        phase, started = "numeric_initial_solution", perf_counter()
        initial = construct_numeric_initial_plan(task, program, quality, graph, cover, runtime)
        measure(started)
        if not initial.complete:
            return finish()

        phase, started = "numeric_serial_search", perf_counter()
        numeric_state, _ = run_numeric_serial_search(
            task,
            program,
            quality,
            initial,
            runtime,
            pair_scan_slack_weight=to_ticks(
                policy.whole_chain_pair_scan_slack_weight,
                task.units.weight,
                "whole_chain_pair_scan_slack_weight",
            ),
            maximum_virtual_bridge_nodes=policy.maximum_virtual_bridge_nodes,
        )
        measure(started)
        domain_plan = numeric_plan_to_domain(
            numeric_state.task, numeric_state.plan, problem, rule_set
        )
        domain_evaluation = numeric_evaluation_to_domain(
            numeric_state.task,
            numeric_state.program,
            numeric_state.quality,
            numeric_state.plan,
            numeric_state.evaluation,
            domain_plan,
        )
        modes = tuple(int(value) for value in numeric_state.task.split_groups.mode)
        state = SearchState(
            domain_plan,
            domain_evaluation,
            accepted_move_count=numeric_state.accepted_move_count,
            virtual_sequence=numeric_state.virtual_sequence,
            split_sequence=len(modes),
            accepted_same_period_split_count=sum(value == 0 for value in modes),
            accepted_future_borrow_return_count=sum(value == 1 for value in modes),
        )

        phase, started = "numeric_core_audit", perf_counter()
        audit = audit_numeric_core_without_search_cache(
            numeric_state, problem, rule_set, timing_input, runtime
        )
        measure(started)
    except NumericValueError as error:
        measure(started)
        if state is None:
            runtime.stop_reason = SearchStopReason.INPUT_INVALID
            issues.append(
                _issue(
                    "numeric_standard_incompatible",
                    f"数值标准输入或规则不适配：{error}",
                    phase=DiagnosticPhase.INPUT_NORMALIZATION,
                    path=error.path,
                )
            )
        else:
            runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
            issues.append(
                _issue(
                    "numeric_solver_error",
                    f"数值求解异常：{error}",
                    phase=DiagnosticPhase.CORE_AUDIT,
                    path=error.path,
                )
            )
    except Exception as error:
        measure(started)
        runtime.stop_reason = SearchStopReason.SYSTEM_ERROR
        emit(
            logger,
            "numeric_solver_stage_failed",
            stage=phase,
            status="error",
            exception_type=type(error).__name__,
            level=logging.ERROR,
            exc_info=True,
        )
        issues.append(
            _issue(
                "numeric_solver_error",
                f"数值求解阶段 {phase} 异常：{type(error).__name__}: {error}",
                phase=DiagnosticPhase.CORE_AUDIT
                if phase == "numeric_core_audit"
                else DiagnosticPhase.SEARCH,
            )
        )
    return finish()
