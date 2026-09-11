"""Derive chain-order behavior from the existing rule and quality declarations."""

from .model import SchedulePlan
from .rules.base import NumericProjection, QualityAggregation, QualityDirection
from .rules.concrete import DeliveryDuePerformanceRule, InterChainWidthGapRule


def has_inter_chain_width_rule(rule_set) -> bool:
    return any(isinstance(rule, InterChainWidthGapRule) for rule in rule_set.rules)


def has_delivery_objective(rule_set) -> bool:
    return any(isinstance(rule, DeliveryDuePerformanceRule) for rule in rule_set.rules)


def has_production_order_rule(rule_set) -> bool:
    return has_inter_chain_width_rule(rule_set) or has_delivery_objective(rule_set)


def delivery_chain_indices(chains, timing):
    """Earliest effective due date first, stable ties; this is enumeration, not a score."""
    return tuple(sorted(range(len(chains)), key=lambda index: min(
        max(0, timing.orders[node.source_order_id].due_hours)
        for node in chains[index].nodes if node.virtual_lineage is None
    )))


def chain_order_objective_index(rule_set) -> int | None:
    if not has_production_order_rule(rule_set):
        return None
    metric = "newly_late_original_weight" if has_delivery_objective(rule_set) else "inter_chain_width_gap"
    return next(
        (
            index
            for index, item in enumerate(rule_set.quality_spec)
            if item.metric_key == metric
            and item.direction is QualityDirection.MINIMIZE
            and item.aggregation is QualityAggregation.SUM
            and item.numeric_projection is NumericProjection.EXACT_DECIMAL
        ),
        None,
    )


def stable_group_plan(plan: SchedulePlan, period_index) -> SchedulePlan:
    """Keep each period's local order; never change a chain or its assignment."""
    if any(chain.assigned_period not in period_index for chain in plan.chains):
        raise ValueError("chain order contains an unknown assigned period")
    chains = tuple(sorted(plan.chains, key=lambda chain: period_index[chain.assigned_period]))
    return plan if chains == plan.chains else SchedulePlan(chains)
