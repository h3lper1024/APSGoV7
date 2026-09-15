"""Independent final audit for one completed numeric search state."""

import logging
from dataclasses import fields

import numpy as np

from ._numeric_boundary import numeric_evaluation_to_domain, numeric_plan_to_domain
from ._numeric_evaluation import evaluate_numeric_plan
from ._numeric_rules import NumericRuleProgram
from ._numeric_search import NumericSearchState
from ._numeric_state import (
    NumericDynamicNode,
    NumericPlan,
    NumericSplitGroup,
    NumericTask,
    extend_numeric_task,
)
from ._numeric_units import NumericValueError, allocate_piece_milliseconds
from .budget import SolveRuntimeBudget
from .contracts import CoreAuditStatus, CoreCandidateSnapshot, SearchStopReason
from .final_audit import (
    _audit_structure,
    _compare_evaluations,
    _outcome,
    _publication_issues,
    _record,
    derive_resource_facts_for_audit,
)
from .process_logging import emit
from .rules.base import RuleEvaluationContext
from .rules.rule_set import ProcessRuleSet


def _interrupted(candidate, problem, rule_set, budget, issues, invariants, authorizations):
    status = (
        CoreAuditStatus.CANCELLED
        if budget.stop_reason is SearchStopReason.USER_CANCELLED
        else CoreAuditStatus.TIME_LIMIT
        if budget.stop_reason is SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
        else CoreAuditStatus.ERROR
    )
    _record(
        issues,
        None,
        "core_audit_cancelled"
        if status is CoreAuditStatus.CANCELLED
        else "core_audit_time_limit"
        if status is CoreAuditStatus.TIME_LIMIT
        else "core_audit_stopped",
        "数值核心审计未完成，未返回部分审计事实或释放资格。",
    )
    return _outcome(candidate, problem, rule_set, status, issues, invariants, authorizations)


def _same_base(task, fresh):
    base = fresh.nodes.weight.size
    if (
        task.units != fresh.units
        or task.period_ids != fresh.period_ids
        or task.node_ids[:base] != fresh.node_ids
        or task.source_ids != fresh.source_ids
        or task.resource_ids != fresh.resource_ids
        or task.prototype_ids != fresh.prototype_ids
        or task.text_labels != fresh.text_labels
        or task.rule_set_fingerprint != fresh.rule_set_fingerprint
        or not np.array_equal(task.prototype_rows, fresh.prototype_rows)
    ):
        return False
    for column in fields(task.nodes):
        if not np.array_equal(getattr(task.nodes, column.name)[:base], getattr(fresh.nodes, column.name)):
            return False
    for column in fields(task.originals):
        if not np.array_equal(getattr(task.originals, column.name), getattr(fresh.originals, column.name)):
            return False
    for name in ("priority", "narrow_matches", "surface_matches", "same_spec_groups"):
        if not np.array_equal(getattr(task, name)[:, :base], getattr(fresh, name)):
            return False
    return True


def _split_group(task, index):
    return NumericSplitGroup(
        *(int(getattr(task.split_groups, column.name)[index]) for column in fields(task.split_groups))
    )


def _dynamic_node(task, row, base_size):
    source = int(task.nodes.source[row])
    prototype = int(task.nodes.prototype[row])
    template = source if source >= 0 else int(task.prototype_rows[prototype])
    return NumericDynamicNode(
        task.node_ids[row],
        template,
        int(task.nodes.weight[row]),
        int(task.nodes.duration_ms[row]),
        int(task.nodes.role[row]),
        source,
        int(task.nodes.resource[row]),
        int(task.nodes.source_period[row]),
        prototype,
        int(task.nodes.purpose[row]),
        int(task.nodes.split_group[row]),
        int(task.nodes.piece_index[row]),
        int(task.nodes.piece_count[row]),
        int(task.nodes.accepted_sequence[row]),
        int(task.nodes.min_temperature[row]),
        int(task.nodes.max_temperature[row]),
        bool(task.nodes.present[row, 2]),
        bool(task.nodes.present[row, -1]),
    )


def _rebuild_task(problem, rule_set, timing_input, state):
    fresh = NumericTask.build(problem, rule_set, timing_input)
    if not _same_base(state.task, fresh):
        raise NumericValueError("audit.base_task", "numeric base columns differ from frozen input")
    base_size = fresh.nodes.weight.size
    rebuilt = fresh
    added_groups = set()
    for row in range(base_size, state.task.nodes.weight.size):
        group = int(state.task.nodes.split_group[row])
        split = None
        if group >= 0 and group not in added_groups:
            if group != len(added_groups):
                raise NumericValueError("audit.split_groups", "split groups are not in accepted order")
            split = _split_group(state.task, group)
            added_groups.add(group)
        rebuilt = extend_numeric_task(
            rebuilt,
            (_dynamic_node(state.task, row, base_size),),
            split_group=split,
        )
    if len(added_groups) != state.task.split_groups.parent_row.size:
        raise NumericValueError("audit.split_groups", "a split group has no material rows")
    for group in range(state.task.split_groups.parent_row.size):
        rows = np.flatnonzero(state.task.nodes.split_group == group)
        pieces = sorted(
            (int(row) for row in rows if int(state.task.nodes.source[row]) >= 0),
            key=lambda row: int(state.task.nodes.piece_index[row]),
        )
        expected = allocate_piece_milliseconds(
            int(state.task.split_groups.parent_weight[group]),
            int(state.task.split_groups.parent_duration_ms[group]),
            tuple(int(state.task.nodes.weight[row]) for row in pieces),
            f"audit.split_groups[{group}].duration",
        )
        if tuple(int(state.task.nodes.duration_ms[row]) for row in pieces) != expected:
            raise NumericValueError(
                f"audit.split_groups[{group}].duration",
                "split piece milliseconds do not conserve the frozen parent",
            )
    return rebuilt


def audit_numeric_core_without_search_cache(
    state,
    problem,
    rule_set,
    timing_input,
    budget,
):
    """Rebuild input columns and re-evaluate the final numeric plan without search caches."""
    if (
        not isinstance(state, NumericSearchState)
        or not isinstance(rule_set, ProcessRuleSet)
        or not isinstance(budget, SolveRuntimeBudget)
    ):
        raise ValueError("numeric audit requires state, problem, rule set and runtime budget")
    issues, invariants, authorizations = [], [], []
    candidate = None
    try:
        domain_plan = numeric_plan_to_domain(state.task, state.plan, problem, rule_set)
        search_evaluation = numeric_evaluation_to_domain(
            state.task,
            state.program,
            state.quality,
            state.plan,
            state.evaluation,
            domain_plan,
        )
        candidate = CoreCandidateSnapshot(domain_plan, search_evaluation)
        if not budget.allows_finalization():
            return _interrupted(
                candidate, problem, rule_set, budget, issues, invariants, authorizations
            )
        context = RuleEvaluationContext(
            problem.period_order,
            {period: index for index, period in enumerate(problem.period_order)},
            tuple(item.prototype_id for item in problem.virtual_prototypes),
            problem.delivery_timing,
        )
        partitions, counts = _audit_structure(
            domain_plan,
            problem,
            rule_set,
            context,
            budget,
            issues,
            invariants,
            authorizations,
        )
        if not budget.allows_finalization():
            return _interrupted(
                candidate, problem, rule_set, budget, issues, invariants, authorizations
            )
        audit_task = _rebuild_task(problem, rule_set, timing_input, state)
        audit_program = NumericRuleProgram.compile(audit_task, rule_set)
        audit_quality = state.quality.rebind(audit_task, audit_program)
        audit_plan = NumericPlan.build(
            audit_task,
            state.plan.node_rows,
            state.plan.chain_offsets,
            state.plan.chain_ids,
            state.plan.chain_periods,
            generation=state.plan.generation,
        )
        numeric_audited = evaluate_numeric_plan(
            audit_task, audit_program, audit_quality, audit_plan
        )
        audited = numeric_evaluation_to_domain(
            audit_task,
            audit_program,
            audit_quality,
            audit_plan,
            numeric_audited,
            domain_plan,
        )
        matches = _compare_evaluations(
            search_evaluation,
            audited,
            issues,
            ordered_chains=True,
        )
        _publication_issues(audited, rule_set, issues)
        facts = None
        if not invariants and not authorizations:
            facts = derive_resource_facts_for_audit(
                domain_plan, problem, context, partitions, budget
            )
            if facts is None:
                return _interrupted(
                    candidate, problem, rule_set, budget, issues, invariants, authorizations
                )
        if not budget.allows_finalization():
            return _interrupted(
                candidate, problem, rule_set, budget, issues, invariants, authorizations
            )
        return _outcome(
            candidate,
            problem,
            rule_set,
            CoreAuditStatus.COMPLETED,
            issues,
            invariants,
            authorizations,
            audited,
            facts,
            counts,
            matches,
        )
    except Exception as error:
        budget.stop_reason = SearchStopReason.SYSTEM_ERROR
        emit(
            logging.getLogger(__name__),
            "solver_stage_exception",
            stage="numeric_core_audit",
            status="error",
            exception_type=type(error).__name__,
            level=logging.ERROR,
            exc_info=True,
        )
        if candidate is None:
            raise
        _record(
            issues,
            None,
            "numeric_core_audit_error",
            f"数值核心审计异常，禁止发布：{type(error).__name__}: {error}",
        )
        return _outcome(
            candidate,
            problem,
            rule_set,
            CoreAuditStatus.ERROR,
            issues,
            invariants,
            authorizations,
        )
