"""Derive chain-order behavior from the existing rule and quality declarations."""

from .model import SchedulePlan
from .rules.base import NumericProjection, QualityAggregation, QualityDirection
from .rules.concrete import InterChainWidthGapRule


def has_inter_chain_width_rule(rule_set) -> bool:
    return any(isinstance(rule, InterChainWidthGapRule) for rule in rule_set.rules)


def chain_order_objective_index(rule_set) -> int | None:
    if not has_inter_chain_width_rule(rule_set):
        return None
    return next(
        (
            index
            for index, item in enumerate(rule_set.quality_spec)
            if item.metric_key == "inter_chain_width_gap"
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
