"""Restore audited public carriers from the authoritative numeric state."""

from collections import defaultdict
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext

from ._numeric_evaluation import NumericObjective, NumericPlanEvaluation
from ._numeric_rules import NumericMetricKind, NumericReason
from ._numeric_state import NumericPlan, NumericTask
from ._numeric_units import SEVERITY_SCALE, WEIGHT_SCORE_SCALE, from_ticks
from .contracts import ControlledSplitMode
from .evaluation import ChainEvaluation, ChainSummary, PlanEvaluation
from .model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from .neighborhoods import AcceptedMoveTrace, _split_partition_id, _split_piece_weights
from .rules.base import (
    ControlledSplitRuleSubject,
    QualityAggregation,
    RuleDisposition,
    RuleEvaluationContext,
    RuleViolation,
)

_METRIC_KEYS = {
    NumericMetricKind.SYNTHETIC_WIDTH_INCREASE: "synthetic_width_increase",
    NumericMetricKind.SYNTHETIC_PRIORITY: "synthetic_node_priority",
    NumericMetricKind.STRATEGIC_PRIORITY: "strategic_customer_rank",
    NumericMetricKind.HIGH_SURFACE_RUN_MAX: "max_high_surface_run_count",
    NumericMetricKind.NARROW_WEIGHT_RUN_MAX: "max_if_narrow_real_run_weight",
    NumericMetricKind.SAME_SPEC_WEIGHT_RUN_MAX: "max_same_spec_real_run_weight",
    NumericMetricKind.UNDERWEIGHT_CHAIN_COUNT: "underweight_chain_count",
    NumericMetricKind.UNDERWEIGHT_TOTAL_GAP: "underweight_total_gap",
    NumericMetricKind.OVERWEIGHT_CHAIN_COUNT: "overweight_chain_count",
    NumericMetricKind.OVERWEIGHT_TOTAL_EXCESS: "overweight_total_excess",
    NumericMetricKind.CHAIN_TARGET_WEIGHT_DEVIATION: "chain_target_weight_deviation",
    NumericMetricKind.CONSECUTIVE_VIRTUAL_MAX: "max_consecutive_virtual_sphc",
    NumericMetricKind.REVERSE_WIDTH_COUNT: "reverse_width_count",
    NumericMetricKind.CONSECUTIVE_REVERSE_WIDTH_COUNT:
        "consecutive_reverse_width_violation_count",
    NumericMetricKind.LATE_PERIOD_COUNT: "late_original_due_period_move_count",
    NumericMetricKind.VIRTUAL_RATIO: "virtual_output_weight_ratio",
    NumericMetricKind.INTER_CHAIN_WIDTH_GAP: "inter_chain_width_gap",
    NumericMetricKind.FUTURE_FILL_TOTAL_GAP: "future_fill_total_gap",
    NumericMetricKind.NEWLY_LATE_ORIGINAL_WEIGHT: "newly_late_original_weight",
    NumericMetricKind.OLD_BACKLOG_LAST_COMPLETION_SECONDS:
        "old_backlog_last_completion_hours",
    NumericMetricKind.DELIVERY_WAIT_BURDEN_WEIGHT_SECONDS:
        "delivery_wait_tardiness_tonne_hours",
}

_REASON_CODES = {
    NumericReason.MISSING_WIDTH: "missing_width",
    NumericReason.SYNTHETIC_WIDTH_INCREASE: "synthetic_width_increase_exceeded",
    NumericReason.SOFT_HARD: "soft_hard_connection_not_allowed",
    NumericReason.TEMPERATURE: "temperature_overlap_below_minimum",
    NumericReason.THICKNESS: "thickness_transition_exceeded",
    NumericReason.WIDTH_TRANSITION: "width_transition_exceeded",
    NumericReason.VIRTUAL_BRIDGE_WIDTH: "virtual_bridge_reverse_width_exceeded",
    NumericReason.HIGH_SURFACE_RUN: "high_surface_run_count",
    NumericReason.NARROW_WEIGHT_RUN: "if_narrow_run_weight",
    NumericReason.SAME_SPEC_WEIGHT_RUN: "same_spec_run_weight",
    NumericReason.CHAIN_UNDERWEIGHT: "chain_weight_below_minimum",
    NumericReason.CHAIN_OVERWEIGHT: "chain_weight_above_maximum",
    NumericReason.VIRTUAL_RUN: "virtual_sphc_run",
    NumericReason.REVERSE_WIDTH_COUNT: "reverse_width_count",
    NumericReason.CONSECUTIVE_REVERSE_WIDTH: "consecutive_reverse_width",
    NumericReason.LATE_PERIOD: "late_original_due_period_move",
    NumericReason.VIRTUAL_RATIO: "virtual_budget",
    NumericReason.EARLY_START: "earliest_process_start_violated",
}

_WEIGHT_METRICS = frozenset(
    {
        NumericMetricKind.NARROW_WEIGHT_RUN_MAX,
        NumericMetricKind.SAME_SPEC_WEIGHT_RUN_MAX,
        NumericMetricKind.UNDERWEIGHT_TOTAL_GAP,
        NumericMetricKind.OVERWEIGHT_TOTAL_EXCESS,
        NumericMetricKind.CHAIN_TARGET_WEIGHT_DEVIATION,
        NumericMetricKind.FUTURE_FILL_TOTAL_GAP,
        NumericMetricKind.NEWLY_LATE_ORIGINAL_WEIGHT,
    }
)
_COUNT_METRICS = frozenset(
    {
        NumericMetricKind.SYNTHETIC_PRIORITY,
        NumericMetricKind.STRATEGIC_PRIORITY,
        NumericMetricKind.HIGH_SURFACE_RUN_MAX,
        NumericMetricKind.UNDERWEIGHT_CHAIN_COUNT,
        NumericMetricKind.OVERWEIGHT_CHAIN_COUNT,
        NumericMetricKind.CONSECUTIVE_VIRTUAL_MAX,
        NumericMetricKind.REVERSE_WIDTH_COUNT,
        NumericMetricKind.CONSECUTIVE_REVERSE_WIDTH_COUNT,
        NumericMetricKind.LATE_PERIOD_COUNT,
    }
)


def _divide(numerator, denominator):
    with localcontext(Context(prec=28, rounding=ROUND_HALF_UP)):
        return Decimal(numerator) / Decimal(denominator)


def _physical(task, row, name, present_index, scale):
    if not bool(task.nodes.present[row, present_index]):
        return None
    return from_ticks(int(getattr(task.nodes, name)[row]), scale, f"nodes[{row}].{name}")


def _split_lineages(task, problem, rule_set):
    context = RuleEvaluationContext(
        problem.period_order,
        {period: index for index, period in enumerate(problem.period_order)},
        tuple(item.prototype_id for item in problem.virtual_prototypes),
        problem.delivery_timing,
    )
    result = {}
    for group in range(task.split_groups.parent_row.size):
        parent_row = int(task.split_groups.parent_row[group])
        source = int(task.split_groups.source[group])
        parent = problem.nodes[source]
        origin = task.period_ids[int(task.split_groups.origin_period[group])]
        subject = ControlledSplitRuleSubject(
            parent.node_id,
            parent,
            origin,
            parent.source_period,
            int(task.split_groups.accepted_sequence[group]) - 1,
        )
        decision = rule_set.evaluate_controlled_split(subject, context)
        weights = _split_piece_weights(parent.weight, decision) if decision.eligible else ()
        if (
            not weights
            or parent_row != source
            or decision.rule_id != rule_set.rules[int(task.split_groups.rule_index[group])].rule_id
        ):
            raise ValueError(f"split group {group} cannot be independently authorized")
        partition = _split_partition_id(subject, decision, weights)
        result[group] = (partition, parent, decision, weights)
    return result


def numeric_plan_to_domain(task, plan, problem, rule_set):
    """Restore one immutable public plan without running an object evaluator."""
    if not isinstance(task, NumericTask) or not isinstance(plan, NumericPlan):
        raise ValueError("numeric task and plan required")
    groups = _split_lineages(task, problem, rule_set)
    original_count = len(problem.nodes)
    purposes = tuple(VirtualPurpose)
    modes = tuple(ControlledSplitMode)
    nodes = {}
    for row_value in plan.node_rows:
        row = int(row_value)
        source = int(task.nodes.source[row])
        if row < original_count:
            node = problem.nodes[source]
        elif source >= 0:
            group = int(task.nodes.split_group[row])
            partition, parent, decision, weights = groups[group]
            piece_index = int(task.nodes.piece_index[row])
            lineage = SplitLineage(
                partition,
                parent.node_id,
                parent.source_order_id,
                parent.source_resource_id,
                parent.source_period,
                task.period_ids[int(task.split_groups.origin_period[group])],
                modes[int(task.split_groups.mode[group])],
                task.period_ids[int(task.split_groups.target_period[group])],
                int(task.split_groups.accepted_sequence[group]),
                parent.weight,
                piece_index,
                len(weights),
                decision.rule_id,
                decision.rule_version,
                decision.decision_fingerprint,
                decision.reason_code,
            )
            node = Node(
                task.node_ids[row],
                parent.source_order_id,
                parent.source_resource_id,
                parent.source_period,
                from_ticks(int(task.nodes.weight[row]), task.units.weight, "split.weight"),
                parent.width,
                parent.thickness,
                parent.min_temperature,
                parent.max_temperature,
                parent.grade,
                MaterialRole.NORMAL_REAL,
                parent.rule_attributes,
                split_lineage=lineage,
            )
        else:
            prototype_index = int(task.nodes.prototype[row])
            prototype = problem.virtual_prototypes[prototype_index]
            group = int(task.nodes.split_group[row])
            node = Node(
                task.node_ids[row],
                None,
                None,
                None,
                from_ticks(int(task.nodes.weight[row]), task.units.weight, "virtual.weight"),
                _physical(task, row, "width", 0, task.units.width),
                _physical(task, row, "thickness", 1, task.units.thickness),
                _physical(task, row, "min_temperature", 2, task.units.temperature),
                _physical(task, row, "max_temperature", -1, task.units.temperature),
                prototype.grade,
                MaterialRole.GENERATED_VIRTUAL,
                prototype.rule_attributes,
                virtual_lineage=VirtualLineage(
                    prototype.prototype_id,
                    purposes[int(task.nodes.purpose[row])],
                    None if group < 0 else groups[group][0],
                    int(task.nodes.accepted_sequence[row]),
                ),
            )
        nodes[row] = node
    chains = []
    for chain in range(plan.chain_ids.size):
        start = int(plan.chain_offsets[chain])
        stop = int(plan.chain_offsets[chain + 1])
        chains.append(
            Chain(
                f"numeric-chain-{int(plan.chain_ids[chain]):06d}",
                tuple(nodes[int(row)] for row in plan.node_rows[start:stop]),
                task.period_ids[int(plan.chain_periods[chain])],
            )
        )
    return SchedulePlan(tuple(chains))


def _metric_value(task, metric):
    kind = metric.kind
    if kind in _COUNT_METRICS:
        return int(metric.numerator)
    if kind in _WEIGHT_METRICS:
        return from_ticks(int(metric.numerator), task.units.weight, _METRIC_KEYS[kind])
    if kind in {
        NumericMetricKind.SYNTHETIC_WIDTH_INCREASE,
        NumericMetricKind.INTER_CHAIN_WIDTH_GAP,
    }:
        return from_ticks(int(metric.numerator), task.units.width, _METRIC_KEYS[kind])
    if kind is NumericMetricKind.VIRTUAL_RATIO:
        return _divide(metric.numerator, metric.denominator)
    if kind is NumericMetricKind.OLD_BACKLOG_LAST_COMPLETION_SECONDS:
        return _divide(metric.numerator, 3600)
    if kind is NumericMetricKind.DELIVERY_WAIT_BURDEN_WEIGHT_SECONDS:
        return _divide(metric.numerator, task.units.weight * 3600)
    raise ValueError(f"unsupported numeric metric {kind.name}")


def _aggregate(values, aggregation):
    if aggregation is QualityAggregation.MAXIMUM:
        return max(values, default=0)
    if aggregation is QualityAggregation.COUNT:
        return len(values)
    if aggregation is QualityAggregation.SUM:
        return sum(values, Decimal(0)) if any(isinstance(value, Decimal) for value in values) else sum(values)
    if aggregation is QualityAggregation.NAMED_VALUE and len(values) == 1:
        return values[0]
    raise ValueError("numeric metric aggregation is not uniquely defined")


def _metrics(task, metrics, declarations):
    grouped = defaultdict(list)
    for metric in metrics:
        grouped[_METRIC_KEYS[metric.kind]].append(_metric_value(task, metric))
    return {key: _aggregate(values, declarations[key]) for key, values in grouped.items()}


def _violation(task, program, plan, value):
    rule = program.rules[value.rule_index]
    chain_id = (
        "plan"
        if value.chain_index < 0
        else f"numeric-chain-{int(plan.chain_ids[value.chain_index]):06d}"
    )
    subject = (
        chain_id
        if value.start_position < 0
        else f"{chain_id}:{rule.rule_id}:{value.start_position}-{value.end_position}"
    )
    reason = _REASON_CODES[value.reason]
    message = f"{rule.rule_id} 数值规则违规：{reason}。"
    if value.reason is NumericReason.EARLY_START:
        row = int(plan.node_rows[int(plan.chain_offsets[value.chain_index]) + value.start_position])
        subject = task.node_ids[row]
        source = task.source_ids[int(task.nodes.source[row])]
        message = (f"节点 {subject}（来源订单 {source}）的计算开始时间比当前工序最早开始时间"
                   f"提前 {_divide(value.severity, SEVERITY_SCALE)} 秒，禁止发布。")
    return RuleViolation(
        rule.rule_id,
        rule.scope,
        subject,
        reason,
        message,
        RuleDisposition.PROHIBITED
        if value.prohibited
        else RuleDisposition.ALLOWED_FINAL_DEVIATION,
        _divide(value.severity, SEVERITY_SCALE),
    )


def _quality_value(task, objective, value):
    if objective in {
        NumericObjective.PROHIBITED_COUNT,
        NumericObjective.UNDERWEIGHT_CHAIN_COUNT,
        NumericObjective.CHAIN_COUNT,
    }:
        return int(value)
    if objective is NumericObjective.PROHIBITED_SEVERITY:
        return _divide(value, SEVERITY_SCALE)
    if objective is NumericObjective.UNDERWEIGHT_TOTAL_GAP:
        return _divide(value, WEIGHT_SCORE_SCALE)
    if objective is NumericObjective.OLD_BACKLOG_COMPLETION:
        return _divide(value, 3600)
    if objective is NumericObjective.DELIVERY_WAIT_BURDEN:
        return _divide(value, task.units.weight * 3600)
    if objective is NumericObjective.INTER_CHAIN_WIDTH_GAP:
        return from_ticks(int(value), task.units.width, objective.value)
    if objective is NumericObjective.GENERATED_VIRTUAL_WEIGHT:
        return from_ticks(int(value), task.units.weight, objective.value)
    raise ValueError(f"unsupported objective {objective.value}")


def numeric_evaluation_to_domain(task, program, quality, plan, evaluation, domain_plan):
    """Restore public metrics from numeric results; never re-evaluate with old rules."""
    if not isinstance(evaluation, NumericPlanEvaluation):
        raise ValueError("numeric evaluation required")
    declarations = rule_set_declarations = {
        key: QualityAggregation.SUM for key in _METRIC_KEYS.values()
    }
    # Maximum metrics are the only non-sum rule contributions in the current registry.
    for kind in (
        NumericMetricKind.HIGH_SURFACE_RUN_MAX,
        NumericMetricKind.NARROW_WEIGHT_RUN_MAX,
        NumericMetricKind.SAME_SPEC_WEIGHT_RUN_MAX,
        NumericMetricKind.CONSECUTIVE_VIRTUAL_MAX,
    ):
        declarations[_METRIC_KEYS[kind]] = QualityAggregation.MAXIMUM
    chain_evaluations = []
    for index, (chain, result) in enumerate(zip(domain_plan.chains, evaluation.chain_results)):
        violations = tuple(_violation(task, program, plan, value) for value in result.violations)
        chain_evaluations.append(
            ChainEvaluation(
                chain.chain_id,
                ChainSummary(
                    chain.assigned_period,
                    from_ticks(int(evaluation.chain_facts.total_weight[index]), task.units.weight, "chain.total_weight"),
                    from_ticks(int(evaluation.chain_facts.real_weight[index]), task.units.weight, "chain.real_weight"),
                    from_ticks(int(evaluation.chain_facts.virtual_weight[index]), task.units.weight, "chain.virtual_weight"),
                    chain.real_node_count,
                    chain.virtual_node_count,
                    chain.split_piece_count,
                ),
                violations,
                _metrics(task, result.metrics, declarations),
            )
        )
    all_metrics = tuple(
        metric
        for result in (*evaluation.chain_results, evaluation.plan_result)
        for metric in result.metrics
    ) + evaluation.node_metrics
    metrics = _metrics(task, all_metrics, rule_set_declarations)
    prohibited = tuple(value for value in evaluation.violations if value.prohibited)
    metrics.update(
        prohibited_violation_count=len(prohibited),
        prohibited_violation_severity=_divide(
            sum(value.severity for value in prohibited), SEVERITY_SCALE
        ),
        chain_count=len(domain_plan.chains),
        generated_virtual_weight=from_ticks(
            evaluation.generated_virtual_weight, task.units.weight, "generated_virtual_weight"
        ),
        borrowed_future_weight=from_ticks(
            evaluation.borrowed_future_weight, task.units.weight, "borrowed_future_weight"
        ),
    )
    violations = tuple(_violation(task, program, plan, value) for value in evaluation.violations)
    quality_key = tuple(
        _quality_value(task, objective, int(value))
        for objective, value in zip(quality.objectives, evaluation.quality_key)
    )
    return PlanEvaluation(tuple(chain_evaluations), violations, metrics, quality_key)


def numeric_trace_to_domain(task, quality, moves):
    return tuple(
        AcceptedMoveTrace(
            sequence=index,
            action_name=move.action.value,
            affected_chain_ids=tuple(
                f"numeric-chain-{chain_id:06d}" for chain_id in move.affected_chain_ids
            ),
            affected_source_order_ids=tuple(
                task.source_ids[source] for source in move.affected_sources
            ),
            quality_before=tuple(
                _quality_value(task, objective, value)
                for objective, value in zip(quality.objectives, move.quality_before)
            ),
            quality_after=tuple(
                _quality_value(task, objective, value)
                for objective, value in zip(quality.objectives, move.quality_after)
            ),
            candidate_check_count=move.sequence,
        )
        for index, move in enumerate(moves, start=1)
    )
