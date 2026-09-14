"""Derive chain-order behavior from the existing rule and quality declarations."""

from decimal import Context, ROUND_HALF_EVEN, localcontext
from itertools import zip_longest

from .delivery_timing import evaluate_delivery
from .model import SchedulePlan
from .contracts import RuleScope, sum_weights
from .rules.base import NumericProjection, QualityAggregation, QualityDirection, RuleDisposition
from .rules.concrete import ChainWeightRangeRule, DeliveryDuePerformanceRule, InterChainWidthGapRule


def has_inter_chain_width_rule(rule_set) -> bool:
    return any(isinstance(rule, InterChainWidthGapRule) for rule in rule_set.rules)


def has_delivery_objective(rule_set) -> bool:
    return any(isinstance(rule, DeliveryDuePerformanceRule) for rule in rule_set.rules)


def has_backlog_priority(rule_set) -> bool:
    return any(isinstance(rule, DeliveryDuePerformanceRule) and rule.include_backlog_clearance
               for rule in rule_set.rules)


def has_second_precision_delivery(rule_set) -> bool:
    return any(isinstance(rule, DeliveryDuePerformanceRule) and rule.second_precision
               for rule in rule_set.rules)


def has_production_order_rule(rule_set) -> bool:
    return has_inter_chain_width_rule(rule_set) or has_delivery_objective(rule_set)


def refinement_admissible(evaluation, rule_set):
    """Only the explicitly publishable chain-weight deviation may enter delivery refinement."""
    if not evaluation.violations:
        return True
    if not has_delivery_objective(rule_set):
        return False
    weight_ids = {rule.rule_id for rule in rule_set.rules if type(rule) is ChainWeightRangeRule}
    return all(
        item.rule_id in weight_ids
        and item.scope is RuleScope.CHAIN
        and item.disposition is RuleDisposition.ALLOWED_FINAL_DEVIATION
        and item.reason_code == "chain_weight_below_minimum"
        and item.reason_code in rule_set.allowed_final_deviation_codes
        for item in evaluation.violations
    )


def refinement_candidate_allowed(before, after, rule_set):
    if not refinement_admissible(after, rule_set):
        return False
    if has_backlog_priority(rule_set):
        # The complete quality comparison owns the confirmed delivery/underweight tradeoff.
        return True
    return all(
        after.quality_key[i] <= before.quality_key[i]
        for i, criterion in enumerate(rule_set.quality_spec)
        if criterion.metric_key in ("underweight_chain_count", "underweight_total_gap")
    )


def delivery_chain_indices(chains, timing):
    """Earliest effective due date first, stable ties; this is enumeration, not a score."""
    return tuple(sorted(range(len(chains)), key=lambda index: min(
        max(0, timing.orders[node.source_order_id].due_hours)
        for node in chains[index].nodes if node.virtual_lineage is None
    )))


def delivery_node_positions(plan, timing, *, backlog_first=False):
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
            key=lambda key: (
                (-completion[key], -(timing.orders[key].weight * completion[key]))
                if backlog_first else -(timing.orders[key].weight * completion[key])
            ),
        )
    ordered = (
        [key for group in zip_longest(late, backlog, on_time) for key in group if key is not None]
        if backlog_first and backlog else
        late + [key for pair in zip_longest(on_time, backlog) for key in pair if key is not None]
    )
    return tuple(position for key in ordered for position in reversed(positions[key]))


def chain_order_objective_index(rule_set) -> int | None:
    if not has_production_order_rule(rule_set):
        return None
    metric = ("old_backlog_last_completion_hours" if has_backlog_priority(rule_set) else
              "newly_late_original_weight" if has_delivery_objective(rule_set) else "inter_chain_width_gap")
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


def critical_delivery_positions(plan, context):
    """One current-plan timing scan; potential only orders attempts, never scores them."""
    timing, period_index = context.delivery_timing, context.period_index
    performance = evaluate_delivery(plan, timing, details=True)
    completion = performance.original_completion_hours
    positions, potential = {}, {}
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        durations = {identity: end - start for identity, start, end in performance.node_times}
        for i, chain in enumerate(plan.chains):
            real = [node for node in chain.nodes if node.virtual_lineage is None]
            for j, node in enumerate(chain.nodes):
                if node.virtual_lineage is None:
                    positions.setdefault(node.source_order_id, []).append((i, j))
            earliest = min(period_index[node.source_period] for node in real)
            owners = {node.source_order_id for node in real if period_index[node.source_period] == earliest}
            if len(owners) != 1:
                continue
            owner = next(iter(owners))
            remaining = [node for node in chain.nodes if node.source_order_id != owner]
            real_remaining = [node for node in remaining if node.virtual_lineage is None]
            if not real_remaining:
                continue
            later = min((node.source_period for node in real_remaining), key=period_index.__getitem__)
            if period_index[later] <= earliest or any(
                node.split_lineage and node.split_lineage.target_assigned_period != later
                for node in real_remaining
            ):
                continue
            potential[owner] = max(potential.get(owner, 0), sum_weights(durations[node.node_id] for node in remaining))
        slack = {key: order.due_hours - completion[key] for key, order in timing.orders.items()}
        backlog = sorted((key for key, order in timing.orders.items() if order.due_hours <= 0),
                         key=lambda key: (-completion[key], -timing.orders[key].weight * completion[key]))
        releasable = sorted((key for key in timing.orders if key in potential),
                            key=lambda key: (-potential[key], -completion[key]))
        late = sorted((key for key, order in timing.orders.items() if order.due_hours > 0 and slack[key] < 0),
                      key=lambda key: (timing.orders[key].weight * slack[key], slack[key]))
        on_time = sorted((key for key, order in timing.orders.items() if order.due_hours > 0 and slack[key] >= 0),
                         key=slack.__getitem__)
    critical = tuple(dict.fromkeys(key for group in zip_longest(backlog, releasable, late)
                                  for key in group if key is not None))
    critical_ids = frozenset(critical)
    ordered = (*critical, *(key for key in on_time if key not in critical_ids))
    return tuple(position for key in ordered for position in reversed(positions[key])), critical


def stable_group_plan(plan: SchedulePlan, period_index) -> SchedulePlan:
    """Keep each period's local order; never change a chain or its assignment."""
    if any(chain.assigned_period not in period_index for chain in plan.chains):
        raise ValueError("chain order contains an unknown assigned period")
    chains = tuple(sorted(plan.chains, key=lambda chain: period_index[chain.assigned_period]))
    return plan if chains == plan.chains else SchedulePlan(chains)
