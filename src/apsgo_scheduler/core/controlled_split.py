"""Rule-authorized complete weight partitions and one post-split search replay."""

from dataclasses import replace
from itertools import pairwise

from .contracts import ControlledSplitMode, SearchStopReason, sum_weights
from .model import Chain, MaterialRole, SchedulePlan, SearchState
from .neighborhoods import (
    SearchContext,
    _normalize_chain,
    _split_lineage,
    _split_partition_id,
    _split_piece_weights,
    _validate_search,
    run_local_search,
    try_complete_candidate,
)
from .resource_facts import derive_evaluation_resource_view
from .rules.base import ControlledSplitRuleSubject, PlanRuleSubject, RuleDisposition
from .rules.concrete import (
    WEIGHT_EPSILON,
    ConsecutiveVirtualMaterialRule,
    VirtualOutputRatioRule,
)


def _prepare_split(state, context, donor, parent, subject, decision, piece_prefix):
    """Build the whole private partition before consuming a complete-candidate check."""
    budget, cache = context.factory.budget, context.factory.cache
    index = cache.context.period_index
    if subject.source_period not in index or subject.origin_assigned_period not in index:
        raise ValueError("controlled split authorization contains an unknown period")
    source, origin = index[subject.source_period], index[subject.origin_assigned_period]
    mode = (
        ControlledSplitMode.SAME_PERIOD_SPLIT
        if source == origin
        else ControlledSplitMode.FUTURE_BORROW_RETURN
    )
    if (
        source < origin
        or decision.mode is not mode
        or decision.target_assigned_period != subject.source_period
        or subject.accepted_split_source_count >= decision.maximum_accepted_source_count
    ):
        raise ValueError("controlled split authorization contradicts its subject")
    weights = _split_piece_weights(parent.weight, decision)
    if not weights:
        return None
    consecutive_limit = min(
        (
            rule.parameters["max_count"]
            for rule in cache.rule_set.rules
            if isinstance(rule, ConsecutiveVirtualMaterialRule)
        ),
        default=None,
    )
    if consecutive_limit is not None:
        parent_position = next(
            index for index, node in enumerate(donor.nodes) if node.node_id == parent.node_id
        )
        adjacent_runs = []
        for nodes in (
            reversed(donor.nodes[:parent_position]),
            donor.nodes[parent_position + 1 :],
        ):
            count = 0
            for node in nodes:
                if node.material_role is not MaterialRole.GENERATED_VIRTUAL:
                    break
                count += 1
            adjacent_runs.append(count)
        if all(adjacent_runs) and sum(adjacent_runs) > consecutive_limit:
            return None
    partition_id = _split_partition_id(subject, decision, weights)
    lineage = _split_lineage(subject, decision, weights, partition_id)
    pieces = []
    for position, weight in enumerate(weights, start=1):
        if not budget.allows_search():
            return None
        pieces.append(
            replace(
                parent,
                node_id=f"{piece_prefix}{partition_id}:piece:{position:04d}",
                weight=weight,
                split_lineage=replace(lineage, piece_index=position),
            )
        )
    returned, separators = [pieces[0]], []
    for left, right in pairwise(pieces):
        if not budget.allows_search():
            return None
        separator = context.factory.separator(
            left,
            right,
            first_sequence=state.virtual_sequence + len(separators) + 1,
            related_partition_id=partition_id,
        )
        if separator is None or not budget.allows_search():
            return None
        separators.append(separator)
        returned.extend((separator, right))
    if sum_weights(node.weight for node in separators) > sum_weights(
        (decision.maximum_separator_weight, WEIGHT_EPSILON)
    ):
        return None
    remaining = tuple(node for node in donor.nodes if node.node_id != parent.node_id)
    if remaining and all(
        node.material_role is MaterialRole.GENERATED_VIRTUAL for node in remaining
    ):
        return None
    candidate = []
    for chain in state.current_plan.chains:
        if not budget.allows_search():
            return None
        if chain.chain_id != donor.chain_id:
            candidate.append(chain)
        elif remaining:
            candidate.append(_normalize_chain(replace(chain, nodes=remaining), context))
    chain_id = f"split-chain-{partition_id}"
    current_ids = {chain.chain_id for chain in state.current_plan.chains}
    while chain_id in current_ids:
        chain_id = "_" + chain_id
    candidate.append(Chain(chain_id, tuple(returned), decision.target_assigned_period))
    return tuple(candidate), state.virtual_sequence + len(separators)


def run_controlled_order_split(state: SearchState, context: SearchContext) -> SearchState:
    """Continue a naturally finished round, split completely, then replay at most once."""
    _validate_search(state, context)
    budget, cache = context.factory.budget, context.factory.cache
    if budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE:
        budget.stop_reason = None
    if not budget.allows_search():
        return state
    starting_sequence = state.split_sequence
    piece_prefix = "split-"
    while any(node.node_id.startswith(piece_prefix) for node in cache.problem.nodes):
        piece_prefix = "_" + piece_prefix
    ratio_rule = next(
        (rule for rule in cache.rule_set.rules if isinstance(rule, VirtualOutputRatioRule)), None
    )
    while budget.allows_search():
        accepted = False
        for donor in state.current_plan.chains:
            if not budget.allows_search():
                return state
            for parent in donor.nodes:
                if not budget.allows_search():
                    return state
                if (
                    parent.material_role is not MaterialRole.NORMAL_REAL
                    or parent.split_lineage is not None
                ):
                    continue
                subject = ControlledSplitRuleSubject(
                    parent.node_id,
                    parent,
                    donor.assigned_period,
                    parent.source_period,
                    state.split_sequence,
                )
                decision = cache.rule_set.evaluate_controlled_split(subject, cache.context)
                if not budget.allows_search():
                    return state
                if not decision.eligible:
                    continue
                prepared = _prepare_split(
                    state, context, donor, parent, subject, decision, piece_prefix
                )
                if not budget.allows_search():
                    return state
                if prepared is None:
                    continue
                candidate, sequence = prepared
                if ratio_rule is not None:
                    plan = SchedulePlan(candidate)
                    resources = derive_evaluation_resource_view(plan, cache.context)
                    if not budget.allows_search():
                        return state
                    contribution = ratio_rule.evaluate(
                        PlanRuleSubject("plan", plan, resources), cache.context
                    )
                    if not budget.allows_search():
                        return state
                    if any(
                        item.disposition is RuleDisposition.PROHIBITED
                        for item in contribution.violations
                    ):
                        continue
                if not budget.consume_candidate_check():
                    return state
                if try_complete_candidate(
                    state,
                    context,
                    candidate,
                    affected_chain_ids=(donor.chain_id,),
                    virtual_sequence=sequence,
                    action_name="controlled_order_split",
                    split_subject=subject,
                    split_decision=decision,
                ):
                    accepted = True
                    break
                if budget.must_stop:
                    return state
            if accepted:
                break
        if not accepted:
            break
    if budget.allows_search():
        if state.split_sequence > starting_sequence:
            run_local_search(state, context)
        else:
            budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
    return state
