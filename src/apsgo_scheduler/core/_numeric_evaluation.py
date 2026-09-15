"""Complete integer plan evaluation before search and incremental wiring."""

from dataclasses import dataclass
from enum import Enum

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
from ._numeric_state import NumericPlan, NumericTask, readonly
from ._numeric_units import (
    NumericValueError,
    checked_product,
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
                    criterion.criterion_id, "quality declaration does not use the new numeric standard"
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
            raise NumericValueError('quality', 'expanded task and rule program must match')
        identity = fingerprint(
            {
                'compiler': 1,
                'task': task.fingerprint,
                'rules': rule_program.fingerprint,
                'objectives': tuple(item.value for item in self.objectives),
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


def _delivery(task, plan):
    if task.start_ms is None:
        raise NumericValueError("delivery", "task has no production start and duration input")
    ends, clock = [], 0
    for row in plan.node_rows:
        clock = checked_sum((clock, int(task.nodes.duration_ms[int(row)])), "production_clock")
        ends.append(clock)
    completion = [int(ends[int(position)]) for position in plan.source_last_position]
    due = task.originals.due_ms
    late = [int(due[index]) > 0 and completion[index] > int(due[index])
            for index in range(len(completion))]
    waits = [
        score_seconds(max(0, completion[index] - max(0, int(due[index]))), "delivery_wait")
        for index in range(len(completion))
    ]
    late_weight = checked_sum(
        (int(task.originals.weight[index]) for index, value in enumerate(late) if value),
        "newly_late_weight",
    )
    old_completion = max(
        (completion[index] for index, value in enumerate(task.originals.old_backlog) if value),
        default=0,
    )
    clearance = score_seconds(old_completion, "old_backlog_last_completion")
    burden = checked_sum(
        (
            checked_product(int(task.originals.weight[index]), wait, "delivery_wait_burden")
            for index, wait in enumerate(waits)
        ),
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


def _node_metrics(task, program, plan):
    metrics = []
    for rule in program.rules:
        if rule.kind not in (NumericRuleKind.SYNTHETIC_PRIORITY, NumericRuleKind.STRATEGIC_PRIORITY):
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
                    (int(task.priority[rule.values[0], int(row)]) for row in plan.node_rows),
                    rule.rule_id,
                ),
            )
        )
    return tuple(metrics)


def evaluate_numeric_plan(task, rule_program, quality_program, plan):
    if (
        not isinstance(task, NumericTask)
        or not isinstance(rule_program, NumericRuleProgram)
        or not isinstance(quality_program, NumericQualityProgram)
        or not isinstance(plan, NumericPlan)
        or rule_program.task_fingerprint != task.fingerprint
        or quality_program.task_fingerprint != task.fingerprint
        or quality_program.rule_program_fingerprint != rule_program.fingerprint
        or plan.task_fingerprint != task.fingerprint
    ):
        raise NumericValueError("evaluation", "matching task, rules, quality and plan required")
    chain_results = tuple(
        evaluate_numeric_chain(task, rule_program, plan, chain)
        for chain in range(plan.chain_ids.size)
    )
    static_plan = evaluate_numeric_static_plan_rules(task, rule_program, plan)
    delivery = _delivery(task, plan)
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
        violation
        for result in (*chain_results, plan_result)
        for violation in result.violations
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
    real_weights, virtual_weights, borrowed_weights = [], [], []
    for row_value in plan.node_rows:
        row = int(row_value)
        if int(task.nodes.role[row]) == _GENERATED_VIRTUAL:
            virtual_weights.append(int(task.nodes.weight[row]))
            continue
        real_weights.append(int(task.nodes.weight[row]))
        chain = int(plan.row_to_chain[row])
        if int(plan.chain_periods[chain]) < int(task.nodes.source_period[row]):
            borrowed_weights.append(int(task.nodes.weight[row]))
    real_weight = checked_sum(real_weights, "scheduled_real_weight")
    virtual_weight = checked_sum(virtual_weights, "generated_virtual_weight")
    borrowed_weight = checked_sum(borrowed_weights, "borrowed_future_weight")

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
        [int64(objective_value(objective), objective.value) for objective in quality_program.objectives],
        np.int64,
    )
    return NumericPlanEvaluation(
        task.fingerprint,
        rule_program.fingerprint,
        quality_program.fingerprint,
        plan.generation,
        plan.fingerprint,
        chain_results,
        plan_result,
        _node_metrics(task, rule_program, plan),
        violations,
        delivery,
        real_weight,
        virtual_weight,
        borrowed_weight,
        quality,
    )
