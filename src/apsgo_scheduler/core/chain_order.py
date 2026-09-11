"""Derive chain-order behavior from the existing rule and quality declarations."""

from decimal import Context, ROUND_HALF_EVEN, localcontext
from itertools import zip_longest

from .delivery_timing import evaluate_delivery
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


def delivery_node_positions(plan, timing):
    """Rank original orders, then last fragments first; never change the score."""
    completion = evaluate_delivery(plan, timing).original_completion_hours
    positions = {}
    for i, chain in enumerate(plan.chains):
        for j, node in enumerate(chain.nodes):
            if node.virtual_lineage is None:
                positions.setdefault(node.source_order_id, []).append((i, j))
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        slack = {key: order.due_hours - completion[key] for key, order in timing.orders.items()}
        late = sorted(
            (key for key, order in timing.orders.items() if order.due_hours > 0 and slack[key] < 0),
            key=lambda key: (slack[key], timing.orders[key].due_hours),
        )
        on_time = sorted(
            (key for key, order in timing.orders.items() if order.due_hours > 0 and slack[key] >= 0),
            key=slack.__getitem__,
        )
        backlog = sorted(
            (key for key, order in timing.orders.items() if order.due_hours <= 0),
            key=lambda key: -(timing.orders[key].weight * completion[key]),
        )
    ordered = late + [key for pair in zip_longest(on_time, backlog) for key in pair if key is not None]
    return tuple(position for key in ordered for position in reversed(positions[key]))


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
