"""Validated boundaries for native summary, accepted details and independent audit."""

from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache

import numpy as np

from ._numeric_rules import (
    NumericMetric,
    NumericMetricKind,
    NumericRuleKind,
    NumericRuleProgram,
    NumericRuleResult,
    NumericViolation,
)
from ._numeric_state import (
    NumericPlan, NumericPlanOverlay, NumericTask, readonly,
    NumericCandidateWorkspace, NumericChainView,
)
from ._numeric_units import (
    NumericValueError,
    int64,
)
from .contracts import fingerprint
from .model import MaterialRole
from .rules.base import NumericProjection, QualityAggregation, QualityDirection
from .rules.rule_set import ProcessRuleSet

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)


class NumericObjective(str, Enum):
    PROHIBITED_COUNT = "prohibited_violation_count"
    PROHIBITED_SEVERITY = "prohibited_violation_severity"
    UNDERWEIGHT_CHAIN_COUNT = "underweight_chain_count"
    UNDERWEIGHT_TOTAL_GAP = "underweight_total_gap"
    OLD_BACKLOG_COMPLETION = "old_backlog_last_completion_hours"
    DELIVERY_WAIT_BURDEN = "delivery_wait_tardiness_tonne_hours"
    INTER_CHAIN_WIDTH_GAP = "inter_chain_width_gap"
    GENERATED_VIRTUAL_WEIGHT = "generated_virtual_weight"
    CHAIN_COUNT = "chain_count"


_QUALITY_SHAPES = {
    NumericObjective.PROHIBITED_COUNT: (
        QualityAggregation.NAMED_VALUE,
        NumericProjection.INTEGER_EXACT_V1,
    ),
    NumericObjective.PROHIBITED_SEVERITY: (
        QualityAggregation.SUM,
        NumericProjection.SEVERITY_ROUND_6_HALF_UP_PER_VIOLATION,
    ),
    NumericObjective.UNDERWEIGHT_CHAIN_COUNT: (
        QualityAggregation.SUM,
        NumericProjection.INTEGER_EXACT_V1,
    ),
    NumericObjective.UNDERWEIGHT_TOTAL_GAP: (
        QualityAggregation.SUM,
        NumericProjection.UNDERWEIGHT_GAP_ROUND_2_HALF_UP_PER_CHAIN,
    ),
    NumericObjective.OLD_BACKLOG_COMPLETION: (
        QualityAggregation.SUM,
        NumericProjection.DELIVERY_SECOND_HALF_UP,
    ),
    NumericObjective.DELIVERY_WAIT_BURDEN: (
        QualityAggregation.SUM,
        NumericProjection.DELIVERY_SECOND_HALF_UP,
    ),
    NumericObjective.INTER_CHAIN_WIDTH_GAP: (
        QualityAggregation.SUM,
        NumericProjection.INTEGER_EXACT_V1,
    ),
    NumericObjective.GENERATED_VIRTUAL_WEIGHT: (
        QualityAggregation.NAMED_VALUE,
        NumericProjection.INTEGER_EXACT_V1,
    ),
    NumericObjective.CHAIN_COUNT: (
        QualityAggregation.NAMED_VALUE,
        NumericProjection.INTEGER_EXACT_V1,
    ),
}


@dataclass(frozen=True, slots=True)
class NumericQualityProgram:
    task_fingerprint: str
    rule_program_fingerprint: str
    objectives: tuple[NumericObjective, ...]
    fingerprint: str

    def __post_init__(self):
        for name in ("task_fingerprint", "rule_program_fingerprint", "fingerprint"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise NumericValueError("quality", f"nonempty {name} required")
        if (
            not isinstance(self.objectives, tuple)
            or any(not isinstance(item, NumericObjective) for item in self.objectives)
            or len(set(self.objectives)) != len(self.objectives)
            or set(self.objectives) != set(NumericObjective)
        ):
            raise NumericValueError("quality", "all ordered unique numeric objectives required")

    @classmethod
    def compile(cls, task, rule_program, rule_set):
        if (
            not isinstance(task, NumericTask)
            or not isinstance(rule_program, NumericRuleProgram)
            or not isinstance(rule_set, ProcessRuleSet)
            or rule_program.task_fingerprint != task.fingerprint
            or rule_program.rule_set_fingerprint != rule_set.fingerprint
        ):
            raise NumericValueError("quality", "matching task, rules and compiled program required")
        objectives = []
        for criterion in rule_set.quality_spec:
            try:
                objective = NumericObjective(criterion.metric_key)
            except ValueError as error:
                raise NumericValueError(
                    criterion.criterion_id, "unsupported numeric quality metric"
                ) from error
            expected_aggregation, expected_projection = _QUALITY_SHAPES[objective]
            if (
                criterion.direction is not QualityDirection.MINIMIZE
                or criterion.aggregation is not expected_aggregation
                or criterion.numeric_projection is not expected_projection
            ):
                raise NumericValueError(
                    criterion.criterion_id,
                    "quality declaration does not use the new numeric standard",
                )
            objectives.append(objective)
        if set(objectives) != set(NumericObjective):
            raise NumericValueError("quality", "the approved nine numeric objectives are required")
        if len(rule_program.for_kind(NumericRuleKind.DELIVERY)) != 1:
            raise NumericValueError("quality", "one second-precision delivery rule is required")
        identity = fingerprint(
            {
                "compiler": 1,
                "task": task.fingerprint,
                "rules": rule_program.fingerprint,
                "objectives": tuple(item.value for item in objectives),
            }
        )
        return cls(task.fingerprint, rule_program.fingerprint, tuple(objectives), identity)

    def rebind(self, task, rule_program):
        if (
            not isinstance(task, NumericTask)
            or not isinstance(rule_program, NumericRuleProgram)
            or rule_program.task_fingerprint != task.fingerprint
        ):
            raise NumericValueError("quality", "expanded task and rule program must match")
        identity = fingerprint(
            {
                "compiler": 1,
                "task": task.fingerprint,
                "rules": rule_program.fingerprint,
                "objectives": tuple(item.value for item in self.objectives),
            }
        )
        return NumericQualityProgram(
            task.fingerprint, rule_program.fingerprint, self.objectives, identity
        )


@dataclass(frozen=True, slots=True, eq=False)
class NumericDeliveryEvaluation:
    node_end_ms: np.ndarray
    original_completion_ms: np.ndarray
    newly_late: np.ndarray
    wait_seconds: np.ndarray
    newly_late_weight: int
    old_backlog_last_completion_seconds: int
    wait_burden_weight_seconds: int

    def __post_init__(self):
        arrays = (
            (self.node_end_ms, np.int64),
            (self.original_completion_ms, np.int64),
            (self.newly_late, np.bool_),
            (self.wait_seconds, np.int64),
        )
        for value, dtype in arrays:
            if (
                not isinstance(value, np.ndarray)
                or value.ndim != 1
                or value.dtype != dtype
                or value.flags.writeable
                or not value.flags.c_contiguous
            ):
                raise NumericValueError("delivery", "read-only delivery columns required")
        original_count = self.original_completion_ms.size
        sizes = (
            self.newly_late.size,
            self.wait_seconds.size,
        )
        if any(size != original_count for size in sizes):
            raise NumericValueError("delivery", "one delivery result is required per original")
        for name in (
            "newly_late_weight",
            "old_backlog_last_completion_seconds",
            "wait_burden_weight_seconds",
        ):
            if int64(getattr(self, name), name) < 0:
                raise NumericValueError(name, "nonnegative delivery value required")


@dataclass(frozen=True, slots=True, eq=False)
class NumericChainFacts:
    total_weight: np.ndarray
    duration_ms: np.ndarray
    real_weight: np.ndarray
    virtual_weight: np.ndarray
    borrowed_weight: np.ndarray

    def __post_init__(self):
        values = (
            self.total_weight,
            self.duration_ms,
            self.real_weight,
            self.virtual_weight,
            self.borrowed_weight,
        )
        if (
            any(
                not isinstance(value, np.ndarray)
                or value.ndim != 1
                or value.dtype != np.int64
                or value.flags.writeable
                or not value.flags.c_contiguous
                for value in values
            )
            or len({value.size for value in values}) != 1
            or any(np.any(value < 0) for value in values)
        ):
            raise NumericValueError(
                "chain_facts", "matching read-only nonnegative columns required"
            )


@dataclass(frozen=True, slots=True, eq=False)
class NumericPlanEvaluation:
    task_fingerprint: str
    rule_program_fingerprint: str
    quality_program_fingerprint: str
    plan_generation: int
    plan_fingerprint: str
    chain_results: tuple[NumericRuleResult, ...]
    plan_result: NumericRuleResult
    node_metrics: tuple[NumericMetric, ...]
    violations: tuple[NumericViolation, ...]
    delivery: NumericDeliveryEvaluation
    scheduled_real_weight: int
    generated_virtual_weight: int
    borrowed_future_weight: int
    quality_key: np.ndarray
    chain_facts: NumericChainFacts
    kernel_result: tuple | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        for name in (
            "task_fingerprint",
            "rule_program_fingerprint",
            "quality_program_fingerprint",
            "plan_fingerprint",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise NumericValueError("evaluation", f"nonempty {name} required")
        if int64(self.plan_generation, "plan_generation") < 0:
            raise NumericValueError("plan_generation", "nonnegative generation required")
        if (
            not isinstance(self.chain_results, tuple)
            or any(not isinstance(value, NumericRuleResult) for value in self.chain_results)
            or not isinstance(self.plan_result, NumericRuleResult)
            or not isinstance(self.node_metrics, tuple)
            or any(not isinstance(value, NumericMetric) for value in self.node_metrics)
            or not isinstance(self.violations, tuple)
            or any(not isinstance(value, NumericViolation) for value in self.violations)
            or not isinstance(self.delivery, NumericDeliveryEvaluation)
            or not isinstance(self.chain_facts, NumericChainFacts)
            or self.chain_facts.total_weight.size != len(self.chain_results)
        ):
            raise NumericValueError("evaluation", "complete immutable numeric results required")
        for name in ("scheduled_real_weight", "generated_virtual_weight", "borrowed_future_weight"):
            if int64(getattr(self, name), name) < 0:
                raise NumericValueError(name, "nonnegative resource weight required")
        if (
            not isinstance(self.quality_key, np.ndarray)
            or self.quality_key.ndim != 1
            or self.quality_key.dtype != np.int64
            or self.quality_key.size != len(NumericObjective)
            or self.quality_key.flags.writeable
            or not self.quality_key.flags.c_contiguous
        ):
            raise NumericValueError("quality_key", "read-only int64 quality vector required")


def _validate_evaluation_inputs(task, rule_program, quality_program, plan):
    if (
        not isinstance(task, NumericTask)
        or not isinstance(rule_program, NumericRuleProgram)
        or not isinstance(quality_program, NumericQualityProgram)
        or not isinstance(plan, (NumericPlan, NumericPlanOverlay))
        or rule_program.task_fingerprint != task.fingerprint
        or quality_program.task_fingerprint != task.fingerprint
        or quality_program.rule_program_fingerprint != rule_program.fingerprint
        or plan.task_fingerprint != task.fingerprint
    ):
        raise NumericValueError("evaluation", "matching task, rules, quality and plan required")


def evaluate_numeric_plan(task, rule_program, quality_program, plan):
    if not isinstance(plan, NumericPlan):
        raise NumericValueError("evaluation", "formal numeric plan required")
    return materialize_numeric_evaluation(task, rule_program, quality_program, plan)


def evaluate_numeric_candidate(task, rule_program, quality_program, plan,
                               previous_task, previous_rule_program, previous_quality_program,
                               previous_plan, previous_evaluation):
    if not isinstance(plan, NumericPlan):
        raise NumericValueError("evaluation", "formal numeric candidate required")
    summary = summarize_numeric_candidate(task, rule_program, quality_program, plan,
        previous_task, previous_rule_program, previous_quality_program, previous_plan,
        previous_evaluation)
    return materialize_numeric_evaluation(task, rule_program, quality_program, plan, summary)


def evaluate_numeric_overlay_candidate(task, rule_program, quality_program, plan,
                                       previous_task, previous_rule_program, previous_quality_program,
                                       previous_plan, previous_evaluation):
    # Detail boundary retained for controlled migration diagnostics, not search rejection.
    if not isinstance(plan, NumericPlanOverlay):
        raise NumericValueError("evaluation", "candidate chain overlay required")
    summary = summarize_numeric_candidate(task, rule_program, quality_program, plan,
        previous_task, previous_rule_program, previous_quality_program, previous_plan,
        previous_evaluation)
    return materialize_numeric_evaluation(task, rule_program, quality_program, plan, summary)


def preview_numeric_chain_order_quality(task, quality_program, plan, evaluation, chain_order):
    from ._numeric_kernel import chain_order_kernel, task_columns
    if (
        not isinstance(task, NumericTask) or not isinstance(quality_program, NumericQualityProgram)
        or not isinstance(plan, NumericPlan) or not isinstance(evaluation, NumericPlanEvaluation)
        or quality_program.task_fingerprint != task.fingerprint
        or plan.task_fingerprint != task.fingerprint
        or evaluation.plan_fingerprint != plan.fingerprint
    ):
        raise NumericValueError("chain_order_preview", "matching numeric state required")
    order = np.asarray(chain_order, dtype=np.int64)
    count = int(plan.chain_ids.size)
    if order.shape != (count,) or not np.array_equal(np.sort(order), np.arange(count)):
        raise NumericValueError("chain_order_preview", "complete chain permutation required")
    output = chain_order_kernel(task_columns(task), plan.node_rows, plan.chain_offsets,
                                order, evaluation.quality_key, _objective_order(quality_program.objectives))
    _check_kernel_status(output)
    return tuple(map(int, output.quality))


@lru_cache(maxsize=32)
def _objective_order(objectives):
    return readonly([tuple(NumericObjective).index(value) for value in objectives], np.int64)


def _kernel_inputs(plan):
    if isinstance(plan, NumericPlan):
        return plan.node_rows, plan.chain_offsets
    lengths = np.fromiter((chain.size for chain in plan.chains), np.int64, len(plan.chains))
    offsets = np.empty(lengths.size + 1, dtype=np.int64)
    offsets[0] = 0
    np.cumsum(lengths, out=offsets[1:])
    rows = np.concatenate(plan.chains)
    rows.setflags(write=False)
    offsets.setflags(write=False)
    return rows, offsets


def _check_kernel_status(result):
    from ._numeric_kernel import OK, CAPACITY, INVALID, NUMERIC_ERROR, CANCELLED, STALE
    code, rule, chain, position = map(int, result.status[:4])
    if code in (OK, CAPACITY):
        return
    reason = {
        INVALID: "invalid numeric layout or missing inter-chain boundary width",
        NUMERIC_ERROR: "integer is outside signed int64 or invalid ratio",
        CANCELLED: "numeric evaluation cancelled",
        STALE: "stale numeric candidate generation",
    }.get(code, "unknown numeric kernel status")
    raise NumericValueError(f"kernel.rules[{rule}].chains[{chain}].positions[{position}]", reason)


def _native_result(task, program, quality, plan, *, detail=False, reuse=None):
    from ._numeric_kernel import evaluate_kernel, task_columns, rule_tables, CAPACITY
    _validate_evaluation_inputs(task, program, quality, plan)
    if task.start_ms is None:
        raise NumericValueError("delivery", "task has no production start and duration input")
    if np.any(plan.chain_periods[1:] < plan.chain_periods[:-1]):
        raise NumericValueError("chain_periods", "production chains must follow task period order")
    rows, offsets = _kernel_inputs(plan)
    # Start bounded. Only the boundary retries if detail buffers prove too small.
    v_capacity = 16 if detail else 0
    m_capacity = 32 if detail else 0
    while True:
        output = evaluate_kernel(
            task_columns(task), rule_tables(program.rules), rows, offsets,
            plan.chain_periods, _objective_order(quality.objectives), detail,
            v_capacity, m_capacity, False, plan.chain_ids, reuse,
        )
        _check_kernel_status(output)
        if output.status[0] != CAPACITY:
            for array in output:
                array.setflags(write=False)
            return output
        v_capacity, m_capacity = map(int, output.counts[:2])


def _validated_reuse(task, program, quality, previous_task, previous_program,
                     previous_quality, previous_plan, previous_evaluation):
    from ._numeric_kernel import ReuseColumns
    _validate_evaluation_inputs(previous_task, previous_program, previous_quality, previous_plan)
    if (
        not isinstance(previous_evaluation, NumericPlanEvaluation)
        or previous_evaluation.task_fingerprint != previous_task.fingerprint
        or previous_evaluation.rule_program_fingerprint != previous_program.fingerprint
        or previous_evaluation.quality_program_fingerprint != previous_quality.fingerprint
        or previous_evaluation.plan_fingerprint != previous_plan.fingerprint
        or previous_evaluation.plan_generation != previous_plan.generation
        or len(previous_evaluation.chain_results) != previous_plan.chain_ids.size
        or previous_evaluation.delivery.node_end_ms.size != previous_plan.node_rows.size
        or previous_evaluation.delivery.original_completion_ms.size != previous_task.originals.weight.size
    ):
        raise NumericValueError("evaluation_reuse", "matching previous evaluation required")
    compatible = (task is previous_task or previous_task.fingerprint in task.ancestor_fingerprints)
    cached = previous_evaluation.kernel_result
    if (not compatible or program.rules != previous_program.rules
            or quality.objectives != previous_quality.objectives or cached is None):
        return None
    return ReuseColumns(previous_plan.node_rows, previous_plan.chain_offsets,
                        previous_plan.chain_ids, previous_plan.chain_periods,
                        cached.facts, cached.scores, cached.hits, cached.event_counts)


def summarize_numeric_candidate(task, program, quality, plan, previous_task,
                                previous_program, previous_quality, previous_plan,
                                previous_evaluation):
    """Fixed native outputs only: no rule result, violation or metric objects."""
    reuse = _validated_reuse(task, program, quality, previous_task, previous_program,
                             previous_quality, previous_plan, previous_evaluation)
    return _native_result(task, program, quality, plan, reuse=reuse)


@dataclass(frozen=True, slots=True, eq=False)
class NumericEvaluationContext:
    """Immutable generation inputs; candidate-specific checks still run per view."""
    task: object
    program: object
    quality: object
    plan: object
    previous_evaluation: object
    columns: object = field(init=False)
    rules: object = field(init=False)
    objectives: object = field(init=False)
    reuse: object = field(init=False)
    base_view: object = field(init=False)
    base_nodes: object = field(init=False)
    base_derived: object = field(init=False)
    base_groups: object = field(init=False)

    def __post_init__(self):
        from ._numeric_kernel import task_columns, rule_tables
        from ._numeric_state import PrivateNodeColumns, PrivateDerivedColumns, PrivateSplitColumns
        _validate_evaluation_inputs(self.task, self.program, self.quality, self.plan)
        reuse = None if self.previous_evaluation is None else _validated_reuse(
            self.task, self.program, self.quality, self.task, self.program,
            self.quality, self.plan, self.previous_evaluation)
        object.__setattr__(self, "columns", task_columns(self.task))
        object.__setattr__(self, "rules", rule_tables(self.program.rules))
        object.__setattr__(self, "objectives", _objective_order(self.quality.objectives))
        object.__setattr__(self, "reuse", reuse)
        plan, task = self.plan, self.task
        private = np.zeros(plan.chain_ids.size, np.bool_)
        private.setflags(write=False)
        object.__setattr__(self, "base_view", NumericChainView(plan.node_rows, plan.node_rows[:0],
            plan.chain_offsets[:-1], plan.chain_offsets[1:], private, plan.chain_ids,
            plan.chain_periods, plan.chain_ids.size, 0))
        for field_name, column_type, source in (("base_nodes", PrivateNodeColumns, task.nodes),
                ("base_derived", PrivateDerivedColumns, task), ("base_groups", PrivateSplitColumns, task.split_groups)):
            object.__setattr__(self, field_name, column_type(*(getattr(source, name) for name in column_type._fields)))

    def require_current(self, workspace, program, quality, previous_evaluation):
        if (workspace.task is not self.task or workspace.plan is not self.plan
                or program is not self.program or quality is not self.quality
                or previous_evaluation is not self.previous_evaluation):
            raise NumericValueError("evaluation_context", "stale generation inputs")


def evaluate_numeric_view(workspace, program, quality, *, view=None,
                          previous_evaluation=None, detail=False, context=None):
    """Common private summary/detail boundary; does not materialize a formal plan."""
    from ._numeric_kernel import (
        private_task_columns, evaluate_view_kernel, CAPACITY,
    )
    if not isinstance(workspace, NumericCandidateWorkspace):
        raise NumericValueError("evaluation_view", "numeric candidate workspace required")
    task, plan = workspace.task, workspace.plan
    if context is None:
        context = NumericEvaluationContext(task, program, quality, plan, previous_evaluation)
    if not isinstance(context, NumericEvaluationContext):
        raise NumericValueError("evaluation_context", "validated generation inputs required")
    context.require_current(workspace, program, quality, previous_evaluation)
    if task.start_ms is None:
        raise NumericValueError("delivery", "task has no production start and duration input")
    view = workspace.view() if view is None else view
    workspace.require_view(view)
    if (view.count <= 0 or np.unique(view.ids[:view.count]).size != view.count
            or np.any(view.ids[:view.count] < 0) or np.any(view.periods[:view.count] < 0)
            or np.any(view.periods[:view.count] >= len(task.period_ids))):
        raise NumericValueError("evaluation_view", "active chains and task periods must match")
    # Editing primitives see capacity; evaluation must see only initialized rows.
    active = NumericChainView(view.base_rows, view.changed_rows[:workspace.changed_count],
        view.starts, view.stops, view.private, view.ids, view.periods, view.count, view.epoch)
    columns = private_task_columns(context.columns, workspace.nodes, workspace.derived,
                                   workspace.node_count)
    violations, metrics = (16, 32) if detail else (0, 0)
    while True:
        result = evaluate_view_kernel(columns, context.rules, active,
            context.objectives, detail, violations, metrics, False, context.reuse)
        _check_kernel_status(result)
        if result.status[0] != CAPACITY:
            for array in result:
                array.setflags(write=False)
            return result
        violations, metrics = map(int, result.counts[:2])


def _map_kernel_details(task, program, quality, plan, output):
    from ._numeric_rules import NumericReason
    violations = tuple(NumericViolation(
        int(row[0]), NumericReason(int(row[1])), int(row[2]), int(row[3]),
        int(row[4]), int(row[5]), bool(row[6]),
    ) for row in output.violations[:int(output.counts[0])])
    metrics = tuple(NumericMetric(
        int(row[0]), NumericMetricKind(int(row[1])), int(row[3]), int(row[4]),
    ) for row in output.metrics[:int(output.counts[1])])
    chain_results = []
    vi = mi = 0
    for nv, nm in output.event_counts:
        next_v, next_m = vi + int(nv), mi + int(nm)
        chain_results.append(NumericRuleResult(violations[vi:next_v], metrics[mi:next_m]))
        vi, mi = next_v, next_m
    plan_metrics = tuple(m for m, row in zip(metrics, output.metrics) if row[2] == -1)
    node_metrics = tuple(m for m, row in zip(metrics, output.metrics) if row[2] == -2)
    delivery = NumericDeliveryEvaluation(
        output.ends, output.completion, output.late, output.waits,
        *map(int, output.totals[3:6]),
    )
    facts = NumericChainFacts(*(readonly(output.facts[:, i], np.int64) for i in range(5)))
    return NumericPlanEvaluation(
        task.fingerprint, program.fingerprint, quality.fingerprint, plan.generation,
        plan.fingerprint if isinstance(plan, NumericPlan) else "candidate-overlay",
        tuple(chain_results), NumericRuleResult(violations[vi:], plan_metrics),
        node_metrics, violations, delivery, *map(int, output.totals[:3]),
        output.quality, facts, output,
    )


def materialize_numeric_evaluation(task, program, quality, plan, summary=None):
    """Accepted candidate/audit boundary; recompute details from the same primitives."""
    output = _native_result(task, program, quality, plan, detail=True)
    if summary is not None:
        for name in ("quality", "facts", "scores", "hits", "totals",
                     "ends", "completion", "late", "waits", "event_counts"):
            if not np.array_equal(getattr(output, name), getattr(summary, name)):
                raise NumericValueError("candidate_overlay", f"summary and detail differ: {name}")
    return _map_kernel_details(task, program, quality, plan, output)
