"""Private, deterministic scanning for the post-repair width optimization phase."""

from dataclasses import replace
from decimal import Decimal
from itertools import zip_longest

from .chain_order import chain_order_objective_index, stable_group_plan
from .contracts import SearchStopReason, sum_weights
from .model import MaterialRole, SchedulePlan
from .neighborhoods import (
    _boundary_join,
    _normalize_chain,
    _validate_search,
    try_complete_candidate,
)
from .rules.base import ChainRuleSubject, PlanRuleSubject, RuleDisposition
from .rules.concrete import ChainWeightRangeRule, InterChainWidthGapRule


def _width_boundaries(plan, context):
    cache = context.factory.cache
    rule = next(rule for rule in cache.rule_set.rules if isinstance(rule, InterChainWidthGapRule))
    # This rule reads only the ordered endpoints, not a second resource view.
    return rule.boundary_contributions(PlanRuleSubject("plan", plan, None), cache.context)


def _ranked_chain_indices(state, context):
    chains = state.current_plan.chains
    priorities = {chain.chain_id: Decimal(0) for chain in chains}
    for left, right, contribution in _width_boundaries(state.current_plan, context):
        priorities[left] = max(priorities[left], contribution.value)
        priorities[right] = max(priorities[right], contribution.value)
    return tuple(
        sorted(range(len(chains)), key=lambda i: (priorities[chains[i].chain_id].copy_negate(), i))
    )


def _node_recipes(state, context):
    """Yield indices only; even role/weight/connection rejection consumes a check."""
    chains = state.current_plan.chains
    ranked = _ranked_chain_indices(state, context)

    def moves():
        for i in ranked:
            for j in ranked:
                if i == j or len(chains[i].nodes) < 2:
                    continue
                for start in range(len(chains[i].nodes)):
                    for position in range(len(chains[j].nodes) + 1):
                        yield ("width_node_move", i, j, start, start + 1, position, position)

    def exchanges():
        for i in ranked:
            for j in ranked:
                if i >= j:
                    continue
                for left in range(len(chains[i].nodes)):
                    for right in range(len(chains[j].nodes)):
                        yield ("width_node_exchange", i, j, left, left + 1, right, right + 1)

    yield from _alternate_recipes(moves(), exchanges())


def _has_real(nodes):
    return any(node.material_role is not MaterialRole.GENERATED_VIRTUAL for node in nodes)


def _block_recipes(state, context):
    """Enumerate non-whole contiguous ranges without reversing their old nodes."""
    chains = state.current_plan.chains
    ranked = _ranked_chain_indices(state, context)

    def moves():
        for i in ranked:
            for j in ranked:
                if i == j:
                    continue
                for length in range(2, len(chains[i].nodes)):
                    for start in range(len(chains[i].nodes) - length + 1):
                        for position in range(len(chains[j].nodes) + 1):
                            yield (
                                "width_block_move",
                                i,
                                j,
                                start,
                                start + length,
                                position,
                                position,
                            )

    def exchanges():
        for i in ranked:
            for j in ranked:
                if i >= j:
                    continue
                left_size, right_size = len(chains[i].nodes), len(chains[j].nodes)
                if min(left_size, right_size) < 2:
                    continue
                for total in range(2, left_size + right_size - 1):
                    for left_length in range(max(1, total - right_size + 1), min(left_size, total)):
                        right_length = total - left_length
                        # One-for-one exchanges already belong to the node family.
                        if left_length == right_length == 1:
                            continue
                        for left in range(left_size - left_length + 1):
                            for right in range(right_size - right_length + 1):
                                yield (
                                    "width_block_exchange",
                                    i,
                                    j,
                                    left,
                                    left + left_length,
                                    right,
                                    right + right_length,
                                )

    yield from _alternate_recipes(moves(), exchanges())


def _width_improves(chains, state, context):
    cache = context.factory.cache
    index = chain_order_objective_index(cache.rule_set)
    if index is None or not context.factory.budget.allows_search():
        return False
    plan = stable_group_plan(SchedulePlan(chains), cache.context.period_index)
    width = sum_weights(item.value for _, _, item in _width_boundaries(plan, context))
    return width < state.current_evaluation.quality_key[index]


def _weight_rejects(chain, context, *, before_bridge=False):
    cache = context.factory.cache
    rule = next(
        (item for item in cache.rule_set.rules if isinstance(item, ChainWeightRangeRule)), None
    )
    if rule is None:
        return False
    contribution = rule.evaluate(ChainRuleSubject(chain.chain_id, chain), cache.context)
    # A necessary interface bridge can supply the remaining few tonnes, but cannot
    # reduce excess weight. Do not reject its potential final chain as underweight.
    return any(
        not before_bridge or violation.disposition is RuleDisposition.PROHIBITED
        for violation in contribution.violations
    )


def _try_segment_edit(state, context, recipe):
    """Build both changed chains privately; commit a complete improvement once."""
    budget = context.factory.budget
    if not budget.allows_search():
        return False
    action, i, j, start, stop, other_start, other_stop = recipe
    chains = state.current_plan.chains
    donor, target = chains[i], chains[j]
    moved, exchanged = donor.nodes[start:stop], target.nodes[other_start:other_stop]
    if not _has_real(moved) or (exchanged and not _has_real(exchanged)):
        return False
    parts = (
        (donor.nodes[:start], exchanged, donor.nodes[stop:]),
        (target.nodes[:other_start], moved, target.nodes[other_stop:]),
    )
    raw_nodes = tuple(tuple(node for part in pieces for node in part) for pieces in parts)
    if not all(_has_real(nodes) for nodes in raw_nodes):
        return False
    changed = tuple(
        _normalize_chain(replace(old, nodes=nodes), context)
        for old, nodes in zip((donor, target), raw_nodes)
    )
    candidate = list(chains)
    candidate[i], candidate[j] = changed
    # Normalize before screening: an interior move can move a whole chain to a
    # later period and change external boundaries without changing its endpoints.
    if not _width_improves(tuple(candidate), state, context):
        return False
    if any(_weight_rejects(chain, context, before_bridge=True) for chain in changed):
        return False
    sequence = state.virtual_sequence
    for index, raw, pieces in zip((i, j), changed, parts):
        nodes = ()
        for piece in pieces:
            joined = _boundary_join(nodes, piece, context, sequence)
            if joined is None:
                return False
            nodes, sequence = joined
        final = replace(raw, nodes=nodes)
        if not budget.allows_search() or _weight_rejects(final, context):
            return False
        candidate[index] = final
    return try_complete_candidate(
        state,
        context,
        tuple(candidate),
        affected_chain_ids=(donor.chain_id, target.chain_id),
        virtual_sequence=sequence,
        action_name=action,
        width_optimization_only=True,
    )


def _alternate_recipes(first, second):
    """Interleave lightweight move/exchange descriptions, never built candidates."""
    missing = object()
    for pair in zip_longest(first, second, fillvalue=missing):
        for recipe in pair:
            if recipe is not missing:
                yield recipe


def _scan_width_batch(state, context, recipes, try_recipe, allowance):
    """Return (accepted, exhausted); consume the shared quota before business work."""
    budget = context.factory.budget
    for _ in range(allowance):
        if not budget.allows_search():
            return False, False
        try:
            recipe = next(recipes)
        except StopIteration:
            return False, True
        if not budget.consume_candidate_check():
            return False, False
        if try_recipe(state, context, recipe):
            return True, False
    return False, False


def _scan_width_families(state, context, family_factories, try_recipe):
    """Share remaining checks, resume rejected scans and restart after a real commit.

    Factories only enumerate index/range/placement recipes. The private callback
    builds one candidate and delegates acceptance to the shared candidate entry.
    Only this scanner consumes the quota; no second search state is created.
    Natural-completion continuation belongs to the eventual phase entry, not here.
    """
    _validate_search(state, context)
    budget = context.factory.budget
    while budget.allows_search():
        pending = [iter(factory(state, context)) for factory in family_factories]
        accepted = False
        while pending and budget.allows_search():
            remaining = budget.candidate_check_limit - budget.candidate_check_count
            allowance = max(1, remaining // len(pending))
            unfinished = []
            for recipes in pending:
                accepted, exhausted = _scan_width_batch(
                    state, context, recipes, try_recipe, allowance
                )
                if accepted or budget.must_stop:
                    break
                if not exhausted:
                    unfinished.append(recipes)
            if accepted or budget.must_stop:
                break
            pending = unfinished
        if not accepted:
            if budget.allows_search():
                budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
            return state
        # Accepted edits invalidate every previous iterator and endpoint summary.
    return state
