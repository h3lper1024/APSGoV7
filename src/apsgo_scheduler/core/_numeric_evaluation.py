"""Complete integer plan evaluation before search and incremental wiring."""

from dataclasses import dataclass, field, replace
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
    evaluate_numeric_chain,
    evaluate_numeric_static_plan_rules,
)
from ._numeric_state import NumericPlan, NumericPlanOverlay, NumericTask, readonly
from ._numeric_units import (
    NumericValueError,
    checked_sum,
    int64,
    score_seconds,
    underweight_score,
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


def _delivery(task, plan, previous_plan=None, previous=None):
    if task.start_ms is None:
        raise NumericValueError("delivery", "task has no production start and duration input")
    rows = (
        plan.node_rows
        if isinstance(plan, NumericPlan)
        else np.concatenate(plan.chains)
    )
    ends = np.empty(rows.size, dtype=np.int64)
    prefix = suffix = 0
    if previous_plan is not None and previous is not None:
        limit = min(rows.size, previous_plan.node_rows.size)
        differences = np.flatnonzero(rows[:limit] != previous_plan.node_rows[:limit])
        prefix = int(differences[0]) if differences.size else limit
        remaining = limit - prefix
        if remaining:
            differences = np.flatnonzero(
                rows[-remaining:][::-1] != previous_plan.node_rows[-remaining:][::-1]
            )
            suffix = int(differences[0]) if differences.size else remaining

    def accumulate(start, stop, clock):
        if start == stop:
            return clock
        durations = task.nodes.duration_ms[rows[start:stop]]
        final = checked_sum((clock, sum(map(int, durations))), "production_clock")
        ends[start:stop] = np.cumsum(durations, dtype=np.int64)
        if clock:
            ends[start:stop] += clock
        return final

    clock = int(previous.node_end_ms[prefix - 1]) if prefix else 0
    if prefix:
        ends[:prefix] = previous.node_end_ms[:prefix]
    middle_stop = rows.size - suffix
    clock = accumulate(prefix, middle_stop, clock)
    if suffix:
        previous_start = previous_plan.node_rows.size - suffix
        previous_entry = int(previous.node_end_ms[previous_start - 1]) if previous_start else 0
        if clock == previous_entry:
            ends[middle_stop:] = previous.node_end_ms[previous_start:]
        else:
            accumulate(middle_stop, rows.size, clock)
    if isinstance(plan, NumericPlan):
        completion = ends[plan.source_last_position]
    else:
        completion = np.zeros(task.originals.weight.size, dtype=np.int64)
        owners = task.nodes.source[rows]
        real = owners >= 0
        np.maximum.at(completion, owners[real], ends[real])
    due = task.originals.due_ms
    late = (due > 0) & (completion > due)
    delayed_ms = np.maximum(completion - np.maximum(due, 0), 0)
    waits = delayed_ms // 1000 + (delayed_ms % 1000 >= 500)
    weights = task.originals.weight
    late_weight = int(weights[late].sum(dtype=np.int64))
    old = completion[task.originals.old_backlog]
    old_completion = int(old.max()) if old.size else 0
    clearance = score_seconds(old_completion, "old_backlog_last_completion")
    burden = int64(
        sum(int(weight) * int(wait) for weight, wait in zip(weights, waits)),
        "delivery_wait_burden",
    )
    return NumericDeliveryEvaluation(
        readonly(ends, np.int64),
        readonly(completion, np.int64),
        readonly(late, np.bool_),
        readonly(waits, np.int64),
        late_weight,
        clearance,
        burden,
    )


def _chain_rows(plan, chain):
    if isinstance(plan, NumericPlanOverlay):
        return plan.chains[chain]
    start, stop = int(plan.chain_offsets[chain]), int(plan.chain_offsets[chain + 1])
    return plan.node_rows[start:stop]


def _one_chain_facts(task, plan, chain):
    rows = _chain_rows(plan, chain)
    virtual = task.nodes.role[rows] == _GENERATED_VIRTUAL
    real_rows, virtual_rows = rows[~virtual], rows[virtual]
    real_weight = checked_sum(
        (int(task.nodes.weight[int(row)]) for row in real_rows), "chain_real_weight"
    )
    virtual_weight = checked_sum(
        (int(task.nodes.weight[int(row)]) for row in virtual_rows), "chain_virtual_weight"
    )
    borrowed_weight = checked_sum(
        (
            int(task.nodes.weight[int(row)])
            for row in real_rows
            if int(plan.chain_periods[chain]) < int(task.nodes.source_period[int(row)])
        ),
        "chain_borrowed_weight",
    )
    return (
        checked_sum((real_weight, virtual_weight), "chain_total_weight"),
        checked_sum((int(task.nodes.duration_ms[int(row)]) for row in rows), "chain_duration"),
        real_weight,
        virtual_weight,
        borrowed_weight,
    )


def _chain_facts(task, plan, previous_plan=None, previous=None):
    old_by_id = (
        {int(identity): index for index, identity in enumerate(previous_plan.chain_ids)}
        if previous_plan is not None and previous is not None
        else {}
    )
    columns = [[] for _ in range(5)]
    for chain, identity in enumerate(plan.chain_ids):
        old = old_by_id.get(int(identity))
        reusable = (
            old is not None
            and int(plan.chain_periods[chain]) == int(previous_plan.chain_periods[old])
            and np.array_equal(_chain_rows(plan, chain), _chain_rows(previous_plan, old))
        )
        values = (
            tuple(
                int(column[old])
                for column in (
                    previous.total_weight,
                    previous.duration_ms,
                    previous.real_weight,
                    previous.virtual_weight,
                    previous.borrowed_weight,
                )
            )
            if reusable
            else _one_chain_facts(task, plan, chain)
        )
        for column, value in zip(columns, values):
            column.append(value)
    return NumericChainFacts(*(readonly(column, np.int64) for column in columns))


def _node_metrics(task, program, plan):
    metrics = []
    for rule in program.rules:
        if rule.kind not in (
            NumericRuleKind.SYNTHETIC_PRIORITY,
            NumericRuleKind.STRATEGIC_PRIORITY,
        ):
            continue
        kind = (
            NumericMetricKind.SYNTHETIC_PRIORITY
            if rule.kind is NumericRuleKind.SYNTHETIC_PRIORITY
            else NumericMetricKind.STRATEGIC_PRIORITY
        )
        metrics.append(
            NumericMetric(
                rule.index,
                kind,
                checked_sum(
                    (
                        int(task.priority[rule.values[0], int(row)])
                        for chain in range(plan.chain_ids.size)
                        for row in _chain_rows(plan, chain)
                    ),
                    rule.rule_id,
                ),
            )
        )
    return tuple(metrics)


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


def _assemble_evaluation(
    task,
    rule_program,
    quality_program,
    plan,
    chain_results,
    chain_facts,
    delivery,
    node_metrics=None,
):
    real_weight = checked_sum(
        (int(value) for value in chain_facts.real_weight), "scheduled_real_weight"
    )
    virtual_weight = checked_sum(
        (int(value) for value in chain_facts.virtual_weight), "generated_virtual_weight"
    )
    borrowed_weight = checked_sum(
        (int(value) for value in chain_facts.borrowed_weight), "borrowed_future_weight"
    )
    static_plan = evaluate_numeric_static_plan_rules(
        task,
        rule_program,
        plan,
        chain_total_weights=chain_facts.total_weight,
        real_weight=real_weight,
        virtual_weight=virtual_weight,
    )
    delivery_rule = rule_program.for_kind(NumericRuleKind.DELIVERY)[0]
    delivery_metrics = (
        NumericMetric(
            delivery_rule.index,
            NumericMetricKind.NEWLY_LATE_ORIGINAL_WEIGHT,
            delivery.newly_late_weight,
        ),
        NumericMetric(
            delivery_rule.index,
            NumericMetricKind.OLD_BACKLOG_LAST_COMPLETION_SECONDS,
            delivery.old_backlog_last_completion_seconds,
        ),
        NumericMetric(
            delivery_rule.index,
            NumericMetricKind.DELIVERY_WAIT_BURDEN_WEIGHT_SECONDS,
            delivery.wait_burden_weight_seconds,
        ),
    )
    plan_result = NumericRuleResult(
        static_plan.violations,
        (*static_plan.metrics, *delivery_metrics),
    )
    violations = tuple(
        violation for result in (*chain_results, plan_result) for violation in result.violations
    )
    prohibited = tuple(value for value in violations if value.prohibited)
    prohibited_count = len(prohibited)
    prohibited_severity = checked_sum(
        (value.severity for value in prohibited), "prohibited_severity"
    )
    underweight = tuple(
        metric
        for result in chain_results
        for metric in result.metrics
        if metric.kind is NumericMetricKind.UNDERWEIGHT_TOTAL_GAP
    )
    underweight_count = checked_sum(
        (
            metric.numerator
            for result in chain_results
            for metric in result.metrics
            if metric.kind is NumericMetricKind.UNDERWEIGHT_CHAIN_COUNT
        ),
        "underweight_chain_count",
    )
    underweight_gap = checked_sum(
        (
            underweight_score(metric.numerator, task.units.weight, "underweight_total_gap")
            for metric in underweight
        ),
        "underweight_total_gap",
    )
    inter_chain_gap = checked_sum(
        (
            metric.numerator
            for metric in static_plan.metrics
            if metric.kind is NumericMetricKind.INTER_CHAIN_WIDTH_GAP
        ),
        "inter_chain_width_gap",
    )

    def objective_value(objective):
        if objective is NumericObjective.PROHIBITED_COUNT:
            return prohibited_count
        if objective is NumericObjective.PROHIBITED_SEVERITY:
            return prohibited_severity
        if objective is NumericObjective.UNDERWEIGHT_CHAIN_COUNT:
            return underweight_count
        if objective is NumericObjective.UNDERWEIGHT_TOTAL_GAP:
            return underweight_gap
        if objective is NumericObjective.OLD_BACKLOG_COMPLETION:
            return delivery.old_backlog_last_completion_seconds
        if objective is NumericObjective.DELIVERY_WAIT_BURDEN:
            return delivery.wait_burden_weight_seconds
        if objective is NumericObjective.INTER_CHAIN_WIDTH_GAP:
            return inter_chain_gap
        if objective is NumericObjective.GENERATED_VIRTUAL_WEIGHT:
            return virtual_weight
        if objective is NumericObjective.CHAIN_COUNT:
            return int(plan.chain_ids.size)
        raise NumericValueError("quality", "unsupported compiled objective")

    quality = readonly(
        [
            int64(objective_value(objective), objective.value)
            for objective in quality_program.objectives
        ],
        np.int64,
    )
    return NumericPlanEvaluation(
        task.fingerprint,
        rule_program.fingerprint,
        quality_program.fingerprint,
        plan.generation,
        plan.fingerprint if isinstance(plan, NumericPlan) else "candidate-overlay",
        chain_results,
        plan_result,
        _node_metrics(task, rule_program, plan) if node_metrics is None else node_metrics,
        violations,
        delivery,
        real_weight,
        virtual_weight,
        borrowed_weight,
        quality,
        chain_facts,
    )


def _evaluate_numeric_plan(task, rule_program, quality_program, plan):
    _validate_evaluation_inputs(task, rule_program, quality_program, plan)
    chain_results = tuple(
        evaluate_numeric_chain(task, rule_program, plan, chain)
        for chain in range(plan.chain_ids.size)
    )
    chain_facts = _chain_facts(task, plan)
    return _assemble_evaluation(
        task,
        rule_program,
        quality_program,
        plan,
        chain_results,
        chain_facts,
        _delivery(task, plan),
    )


def evaluate_numeric_plan(task, rule_program, quality_program, plan):
    if not isinstance(plan, NumericPlan):
        raise NumericValueError("evaluation", "formal numeric plan required")
    return _evaluate_numeric_plan(task, rule_program, quality_program, plan)


def _evaluate_numeric_candidate(
    task,
    rule_program,
    quality_program,
    plan,
    previous_task,
    previous_rule_program,
    previous_quality_program,
    previous_plan,
    previous_evaluation,
):
    """Reuse only facts proven unchanged; all aggregation follows the full evaluator."""
    _validate_evaluation_inputs(task, rule_program, quality_program, plan)
    _validate_evaluation_inputs(
        previous_task, previous_rule_program, previous_quality_program, previous_plan
    )
    if (
        not isinstance(previous_evaluation, NumericPlanEvaluation)
        or previous_evaluation.task_fingerprint != previous_task.fingerprint
        or previous_evaluation.rule_program_fingerprint != previous_rule_program.fingerprint
        or previous_evaluation.quality_program_fingerprint != previous_quality_program.fingerprint
        or previous_evaluation.plan_fingerprint != previous_plan.fingerprint
        or previous_evaluation.plan_generation != previous_plan.generation
        or len(previous_evaluation.chain_results) != previous_plan.chain_ids.size
        or previous_evaluation.delivery.node_end_ms.size != previous_plan.node_rows.size
        or previous_evaluation.delivery.original_completion_ms.size
        != previous_task.originals.weight.size
    ):
        raise NumericValueError("evaluation_reuse", "matching previous evaluation required")
    task_is_compatible = task is previous_task or (
        previous_task.fingerprint in task.ancestor_fingerprints
    )
    if (
        not task_is_compatible
        or rule_program.rules != previous_rule_program.rules
        or quality_program.objectives != previous_quality_program.objectives
    ):
        return _evaluate_numeric_plan(task, rule_program, quality_program, plan)

    old_by_id = {int(identity): index for index, identity in enumerate(previous_plan.chain_ids)}
    chain_results = []
    for chain, identity in enumerate(plan.chain_ids):
        old = old_by_id.get(int(identity))
        if (
            old is None
            or int(plan.chain_periods[chain]) != int(previous_plan.chain_periods[old])
            or not np.array_equal(_chain_rows(plan, chain), _chain_rows(previous_plan, old))
        ):
            chain_results.append(evaluate_numeric_chain(task, rule_program, plan, chain))
            continue
        result = previous_evaluation.chain_results[old]
        if old != chain:
            result = NumericRuleResult(
                tuple(replace(value, chain_index=chain) for value in result.violations),
                result.metrics,
            )
        chain_results.append(result)
    chain_facts = _chain_facts(task, plan, previous_plan, previous_evaluation.chain_facts)
    return _assemble_evaluation(
        task,
        rule_program,
        quality_program,
        plan,
        tuple(chain_results),
        chain_facts,
        _delivery(task, plan, previous_plan, previous_evaluation.delivery),
        (
            previous_evaluation.node_metrics
            if task is previous_task
            else _node_metrics(task, rule_program, plan)
        ),
    )


def evaluate_numeric_candidate(
    task,
    rule_program,
    quality_program,
    plan,
    previous_task,
    previous_rule_program,
    previous_quality_program,
    previous_plan,
    previous_evaluation,
):
    if not isinstance(plan, NumericPlan):
        raise NumericValueError("evaluation", "formal numeric candidate required")
    return _evaluate_numeric_candidate(
        task,
        rule_program,
        quality_program,
        plan,
        previous_task,
        previous_rule_program,
        previous_quality_program,
        previous_plan,
        previous_evaluation,
    )


def evaluate_numeric_overlay_candidate(
    task,
    rule_program,
    quality_program,
    plan,
    previous_task,
    previous_rule_program,
    previous_quality_program,
    previous_plan,
    previous_evaluation,
):
    if not isinstance(plan, NumericPlanOverlay):
        raise NumericValueError("evaluation", "candidate chain overlay required")
    return _evaluate_numeric_candidate(
        task,
        rule_program,
        quality_program,
        plan,
        previous_task,
        previous_rule_program,
        previous_quality_program,
        previous_plan,
        previous_evaluation,
    )


def preview_numeric_chain_order_quality(task, quality_program, plan, evaluation, chain_order):
    """Return the exact quality key for a pure chain-order candidate."""
    if (
        not isinstance(task, NumericTask)
        or not isinstance(quality_program, NumericQualityProgram)
        or not isinstance(plan, NumericPlan)
        or not isinstance(evaluation, NumericPlanEvaluation)
        or quality_program.task_fingerprint != task.fingerprint
        or plan.task_fingerprint != task.fingerprint
        or evaluation.plan_fingerprint != plan.fingerprint
    ):
        raise NumericValueError("chain_order_preview", "matching numeric state required")
    order = np.asarray(chain_order, dtype=np.int64)
    count = int(plan.chain_ids.size)
    if order.shape != (count,) or not np.array_equal(np.sort(order), np.arange(count)):
        raise NumericValueError("chain_order_preview", "complete chain permutation required")

    rows = np.concatenate(
        tuple(
            plan.node_rows[
                int(plan.chain_offsets[index]) : int(plan.chain_offsets[index + 1])
            ]
            for index in order
        )
    )
    ends = np.cumsum(task.nodes.duration_ms[rows], dtype=np.int64)
    owners = task.nodes.source[rows]
    real = owners >= 0
    completion = np.zeros(task.originals.weight.size, dtype=np.int64)
    np.maximum.at(completion, owners[real], ends[real])
    old_completion = int(completion[task.originals.old_backlog].max(initial=0))
    due = np.maximum(0, task.originals.due_ms)
    waits = (np.maximum(0, completion - due) + 500) // 1000
    burden = int64(
        int(np.dot(task.originals.weight, waits)), "delivery_wait_burden"
    )

    tail_rows = np.asarray(
        [int(plan.node_rows[int(plan.chain_offsets[index + 1]) - 1]) for index in order[:-1]],
        dtype=np.int64,
    )
    head_rows = np.asarray(
        [int(plan.node_rows[int(plan.chain_offsets[index])]) for index in order[1:]],
        dtype=np.int64,
    )
    if tail_rows.size and (
        not task.nodes.present[tail_rows, 0].all()
        or not task.nodes.present[head_rows, 0].all()
    ):
        raise NumericValueError(
            "inter_chain_width_gap_objective", "inter-chain boundary width is missing"
        )
    gap = int64(
        int(np.abs(task.nodes.width[tail_rows] - task.nodes.width[head_rows]).sum()),
        "inter_chain_width_gap",
    )
    values = [int(value) for value in evaluation.quality_key]
    replacements = {
        NumericObjective.OLD_BACKLOG_COMPLETION: score_seconds(
            old_completion, "old_backlog_last_completion"
        ),
        NumericObjective.DELIVERY_WAIT_BURDEN: burden,
        NumericObjective.INTER_CHAIN_WIDTH_GAP: gap,
    }
    for objective, value in replacements.items():
        values[quality_program.objectives.index(objective)] = value
    return tuple(values)


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
    from ._numeric_kernel import OK, CAPACITY, INVALID, NUMERIC_ERROR, CANCELLED
    code, rule, chain, position = map(int, result.status[:4])
    if code in (OK, CAPACITY):
        return
    reason = {
        INVALID: "invalid numeric layout or missing inter-chain boundary width",
        NUMERIC_ERROR: "integer is outside signed int64 or invalid ratio",
        CANCELLED: "numeric evaluation cancelled",
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
