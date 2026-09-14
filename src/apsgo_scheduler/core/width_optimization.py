"""Private, deterministic scanning for the post-repair width optimization phase."""

from dataclasses import replace
from collections import deque
from decimal import Decimal
from itertools import pairwise, permutations, zip_longest

from .chain_order import chain_order_objective_index, stable_group_plan, has_delivery_objective, delivery_chain_indices, delivery_node_positions, refinement_admissible, has_second_precision_delivery, critical_delivery_positions
from .contracts import SearchStopReason, fingerprint, sum_weights
from .model import MaterialRole, SchedulePlan
from .neighborhoods import (
    _boundary_join,
    _chain_order_positions,
    _normalize_chain,
    _relocate_chain,
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
    if has_delivery_objective(context.factory.cache.rule_set):
        return delivery_chain_indices(chains, context.factory.cache.context.delivery_timing)
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


def _intra_recipes(state, positions):
    for i, start in positions:
        for position in range(start):
            yield ("delivery_intra_move", i, start, position)


def _delivery_node_recipes(state, positions):
    """Try earlier slots first; late-deadline exchange partners are only a heuristic."""
    chains = state.current_plan.chains
    ranks = {position: rank for rank, position in enumerate(positions)}

    def moves():
        for i, start in positions:
            if len(chains[i].nodes) < 2:
                continue
            for j in (*range(i), *range(i + 1, len(chains))):
                for slot in range(len(chains[j].nodes) + 1):
                    yield ("width_node_move", i, j, start, start + 1, slot, slot)

    def exchanges():
        seen = set()
        for i, start in positions:
            partners = sorted(
                ((j, k) for j, k in positions if j != i),
                key=lambda p: (p[0] >= i, -ranks[p], p),
            )
            for j, slot in partners:
                pair = tuple(sorted(((i, start), (j, slot))))
                if pair in seen:
                    continue
                seen.add(pair)
                yield ("width_node_exchange", i, j, start, start + 1, slot, slot + 1)

    yield from _alternate_recipes(moves(), exchanges())


def _try_intra_move(state, context, recipe):
    """Remove and reinsert in one chain; repair both exposed interfaces atomically."""
    budget = context.factory.budget
    if not budget.allows_search():
        return False
    action, i, start, position = recipe
    old = state.current_plan.chains[i]
    if not 0 <= position < start < len(old.nodes):
        return False
    moved = old.nodes[start:start + 1]
    if not _has_real(moved):
        return False
    pieces = (old.nodes[:position], moved, old.nodes[position:start], old.nodes[start + 1:])
    nodes, sequence = (), state.virtual_sequence
    for piece in pieces:
        joined = _boundary_join(nodes, piece, context, sequence)
        if joined is None:
            return False
        nodes, sequence = joined
    changed = replace(old, nodes=nodes)
    if _weight_rejects(changed, context):
        return False
    candidate = list(state.current_plan.chains)
    candidate[i] = changed
    return try_complete_candidate(
        state, context, tuple(candidate), affected_chain_ids=(old.chain_id,),
        virtual_sequence=sequence, action_name=action, width_optimization_only=True,
    )


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


def _delivery_block_recipes(state, positions):
    """Short intact blocks around ranked real nodes; de-duplicate only this scan."""
    chains = state.current_plan.chains
    seen_blocks, seen_exchanges = set(), set()
    for i, anchor in positions:
        for length in range(2, len(chains[i].nodes)):
            for start in range(max(0, anchor - length + 1), min(anchor + 1, len(chains[i].nodes) - length + 1)):
                block = (i, start, start + length)
                if block in seen_blocks:
                    continue
                seen_blocks.add(block)
                for j in (*range(i), *range(i + 1, len(chains))):
                    def moves():
                        for slot in range(len(chains[j].nodes) + 1):
                            yield ("width_block_move", i, j, start, start + length, slot, slot)

                    def exchanges():
                        # Single-for-block is included here by symmetry; single-for-single
                        # is already covered by the node family.
                        for size in range(1, len(chains[j].nodes)):
                            for slot in range(len(chains[j].nodes) - size + 1):
                                pair = tuple(sorted((block, (j, slot, slot + size))))
                                if pair in seen_exchanges:
                                    continue
                                seen_exchanges.add(pair)
                                yield ("width_block_exchange", i, j, start, start + length, slot, slot + size)

                    yield from _alternate_recipes(moves(), exchanges())


def _delivery_iterators(state, context):
    """One timing scan per accepted plan, shared by all five refinement families."""
    timing = context.factory.cache.context.delivery_timing
    positions = delivery_node_positions(state.current_plan, timing)
    ranked = tuple(dict.fromkeys(i for i, _ in positions))

    def cuts():
        for source in ranked:
            size = len(state.current_plan.chains[source].nodes)
            anchors = [j for i, j in positions if i == source]
            ordered = dict.fromkeys(cut for j in anchors for cut in (j, j + 1) if 0 < cut < size)
            for cut in (*ordered, *(cut for cut in range(1, size) if cut not in ordered)):
                for prefix, suffix in permutations(range(len(state.current_plan.chains) + 1), 2):
                    yield ("width_chain_cut", source, cut, prefix, suffix)

    def orders():
        for source in ranked:
            for position in _chain_order_positions(state.current_plan.chains, source):
                yield ("width_chain_order_relocation", source, position)

    return [iter(_intra_recipes(state, positions)), iter(_delivery_node_recipes(state, positions)),
            iter(_delivery_block_recipes(state, positions)), iter(cuts()), iter(orders())]


def _round_robin(streams, budget, allowance=4):
    """Keep lazy proposal cursors, including empty streams, under the shared stop clock."""
    pending = deque(iter(stream) for stream in streams)
    while pending and budget.allows_search():
        stream = pending.popleft()
        for _ in range(allowance):
            if not budget.allows_search():
                return
            try:
                recipe = next(stream)
            except StopIteration:
                break
            yield recipe
        else:
            pending.append(stream)


def _direct_insertion_slots(node, target_nodes, positions, context):
    """Stable direct-first ordering, not a feasibility filter or a second evaluator."""
    cache, budget = context.factory.cache, context.factory.budget
    deferred = []
    for slot in positions:
        if not budget.allows_search():
            return
        left_allowed = slot == 0 or cache.allows(target_nodes[slot - 1], node)
        if not budget.allows_search():
            return
        direct = left_allowed and (slot == len(target_nodes) or cache.allows(node, target_nodes[slot]))
        if not budget.allows_search():
            return
        if direct:
            yield slot
        else:
            deferred.append(slot)
    for slot in deferred:
        if not budget.allows_search():
            return
        yield slot


def _critical_recipe(recipe, plan, critical_ids):
    action, source = recipe[:2]
    if action == "delivery_intra_move":
        return plan.chains[source].nodes[recipe[2]].source_order_id in critical_ids
    if action not in ("width_node_move", "width_node_exchange", "width_block_move", "width_block_exchange"):
        return False
    _, _, target, start, stop, other_start, other_stop = recipe
    if stop - start > 3 or other_stop - other_start > 3:
        return False
    nodes = plan.chains[source].nodes[start:stop] + plan.chains[target].nodes[other_start:other_stop]
    return any(node.source_order_id in critical_ids for node in nodes)


def _backlog_iterators(state, context, *, progress=None, positions=None, critical_ids=frozenset(),
                       critical_lane=None, revisit=()):
    """One queue slot per original, no candidate plans or rejection cache."""
    chains, budget = state.current_plan.chains, context.factory.budget
    if positions is None:
        positions = delivery_node_positions(state.current_plan, context.factory.cache.context.delivery_timing,
                                            backlog_first=True)
    ranks = {position: rank for rank, position in enumerate(positions)}
    originals = {}
    for i, j in positions:
        originals.setdefault(chains[i].nodes[j].source_order_id, []).append((i, j))
    progress = progress if progress is not None else [dict(after_order=None, targets={}) for _ in range(5)]
    chain_owners = {}
    for position in positions:
        chain_owners.setdefault(position[0], position)

    def rotate_after(values, previous):
        values = tuple(values)
        offset = values.index(previous) + 1 if previous in values else 0
        return values[offset:] + values[:offset]

    def targets(i, hint):
        earlier_first = (*range(i), *range(i + 1, len(chains)))
        previous = next((j for j in earlier_first if hint and chains[j].chain_id == hint[0]), None)
        return rotate_after(earlier_first, previous)

    def slots(j, values, hint):
        previous = next((k for k, node in enumerate(chains[j].nodes)
                         if hint and chains[j].chain_id == hint[0] and node.node_id == hint[1]), None)
        return rotate_after(values, previous)

    def node_target(i, anchor, j, hint):
        move_slots = (_direct_insertion_slots(chains[i].nodes[anchor], chains[j].nodes,
                      slots(j, range(len(chains[j].nodes) + 1), hint), context)
                      if len(chains[i].nodes) > 1 else ())
        moves = (("width_node_move", i, j, anchor, anchor + 1, slot, slot)
                 for slot in move_slots)
        # Canonical ownership replaces an ever-growing set of visited exchange pairs.
        partners = sorted((slot for source, slot in positions if source == j
                           and ranks[(i, anchor)] < ranks[(j, slot)]),
                          key=lambda slot: -ranks[(j, slot)])
        swaps = (("width_node_exchange", i, j, anchor, anchor + 1, slot, slot + 1) for slot in slots(j, partners, hint))
        yield from _alternate_recipes(moves, swaps)

    def block_owner(i, start, stop):
        return min((ranks[(i, j)] for j in range(start, stop) if (i, j) in ranks), default=len(ranks))

    def block_target(i, start, stop, j, hint):
        moves = (("width_block_move", i, j, start, stop, slot, slot)
                 for slot in slots(j, range(len(chains[j].nodes) + 1), hint))

        def swaps():
            maximum = min(4, len(chains[j].nodes)) if critical_lane else len(chains[j].nodes)
            for size in range(1, maximum):
                for slot in slots(j, range(len(chains[j].nodes) - size + 1), hint):
                    if not budget.allows_search():
                        return
                    # A singleton cannot be a source block; otherwise only the
                    # earlier canonical block owns this symmetric exchange.
                    if size > 1 and (block_owner(i, start, stop), i, start, stop) > (
                        block_owner(j, slot, slot + size), j, slot, slot + size
                    ):
                        continue
                    yield ("width_block_exchange", i, j, start, stop, slot, slot + size)
        yield from _alternate_recipes(moves, swaps())

    def proposals(kind, anchors, hint):
        for i, anchor in anchors:
            if not budget.allows_search():
                return
            if kind == "intra_move":
                for slot in slots(i, range(anchor), hint):
                    yield ("delivery_intra_move", i, anchor, slot)
            elif kind == "node_edit":
                yield from _round_robin((node_target(i, anchor, j, hint) for j in targets(i, hint)), budget, 2)
            elif kind == "block_edit":
                maximum = min(4, len(chains[i].nodes)) if critical_lane else len(chains[i].nodes)
                for length in range(2, maximum):
                    for start in range(max(0, anchor - length + 1), min(anchor + 1, len(chains[i].nodes) - length + 1)):
                        if not budget.allows_search():
                            return
                        if block_owner(i, start, start + length) == ranks[(i, anchor)]:
                            yield from _round_robin((block_target(i, start, start + length, j, hint)
                                                    for j in targets(i, hint)), budget, 2)
            elif chain_owners[i] == (i, anchor):
                if kind == "chain_cut":
                    size = len(chains[i].nodes)
                    ordered = dict.fromkeys(cut for source, j in positions if source == i
                                            for cut in (j, j + 1) if 0 < cut < size)
                    cuts = (*ordered, *(cut for cut in range(1, size) if cut not in ordered))
                    for cut in slots(i, cuts, hint):
                        for prefix, suffix in permutations(range(len(chains) + 1), 2):
                            if not budget.allows_search():
                                return
                            yield ("width_chain_cut", i, cut, prefix, suffix)
                else:
                    previous = next((j for j, chain in enumerate(chains) if hint and chain.chain_id == hint[0]), None)
                    for position in rotate_after(_chain_order_positions(chains, i), previous):
                        yield ("width_chain_order_relocation", i, position)

    def family_stream(family, kind):
        cursor = progress[family]
        keys = rotate_after(originals, cursor["after_order"])
        if critical_lane:
            keys = tuple(dict.fromkeys((*revisit, *keys)))
            keys = tuple(key for key in keys if key in originals and
                         (kind == "block_edit" or key in critical_ids))
            if kind in ("chain_cut", "chain_order"):
                return iter(())

        def original_stream(key):
            hint = cursor["targets"].get(key)
            for recipe in proposals(kind, originals[key], hint):
                if critical_lane is not None:
                    if not budget.allows_search():
                        return
                    if _critical_recipe(recipe, state.current_plan, critical_ids) != critical_lane:
                        continue
                cursor["after_order"] = key
                if kind == "intra_move":
                    _, target, _, slot = recipe
                elif kind in ("node_edit", "block_edit"):
                    _, _, target, _, _, slot, _ = recipe
                elif kind == "chain_cut":
                    _, target, slot, _, _ = recipe
                else:
                    _, _, target = recipe
                    slot = len(chains[target].nodes)
                nodes = chains[target].nodes
                cursor["targets"][key] = (chains[target].chain_id, nodes[slot].node_id if slot < len(nodes) else None)
                yield recipe

        return _round_robin((original_stream(key) for key in keys), budget)

    return [family_stream(index, kind) for index, kind in enumerate(
        ("intra_move", "node_edit", "block_edit", "chain_cut", "chain_order")
    )]


def _width_improves(chains, state, context):
    cache = context.factory.cache
    index = chain_order_objective_index(cache.rule_set)
    if index is None or not context.factory.budget.allows_search():
        return False
    if has_delivery_objective(cache.rule_set):
        return True
    plan = stable_group_plan(SchedulePlan(chains), cache.context.period_index)
    width = sum_weights(item.value for _, _, item in _width_boundaries(plan, context))
    return width < state.current_evaluation.quality_key[index]


def _cut_recipes(state, context):
    """Describe both final slots; period legality is checked after the debit."""
    chains = state.current_plan.chains
    for source in _ranked_chain_indices(state, context):
        for cut in range(1, len(chains[source].nodes)):
            for prefix_position, suffix_position in permutations(range(len(chains) + 1), 2):
                yield ("width_chain_cut", source, cut, prefix_position, suffix_position)


def _try_chain_cut(state, context, recipe):
    """Split only the chain, jointly place both intact parts and publish once."""
    budget, cache = context.factory.budget, context.factory.cache
    if not budget.allows_search():
        return False
    action, source, cut, prefix_position, suffix_position = recipe
    chains = state.current_plan.chains
    old = chains[source]
    pieces = (old.nodes[:cut], old.nodes[cut:])
    if not all(_has_real(nodes) for nodes in pieces):
        return False
    suffix_id = "width-cut-" + fingerprint(
        (action, old.chain_id, cut, tuple(node.node_id for node in old.nodes))
    )
    occupied = {chain.chain_id for chain in chains}
    while suffix_id in occupied:
        suffix_id = "_" + suffix_id
    prefix = _normalize_chain(replace(old, nodes=pieces[0]), context)
    suffix = _normalize_chain(replace(old, chain_id=suffix_id, nodes=pieces[1]), context)
    if _weight_rejects(prefix, context) or _weight_rejects(suffix, context):
        return False
    remaining = iter(chains[:source] + chains[source + 1 :])
    candidate = tuple(
        prefix
        if position == prefix_position
        else suffix
        if position == suffix_position
        else next(remaining)
        for position in range(len(chains) + 1)
    )
    periods = tuple(cache.context.period_index[chain.assigned_period] for chain in candidate)
    # Reject illegal raw slots: grouping them would evaluate a duplicate placement
    # and hide which two positions were actually enumerated and charged.
    if any(left > right for left, right in pairwise(periods)):
        return False
    if not _width_improves(candidate, state, context):
        return False
    return try_complete_candidate(
        state,
        context,
        candidate,
        affected_chain_ids=(old.chain_id,),
        virtual_sequence=state.virtual_sequence,
        action_name=action,
        width_optimization_only=True,
    )


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
        (not before_bridge and not has_delivery_objective(cache.rule_set))
        or violation.disposition is RuleDisposition.PROHIBITED
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


def _order_recipes(state, context):
    chains = state.current_plan.chains
    sources = _ranked_chain_indices(state, context) if has_delivery_objective(context.factory.cache.rule_set) else range(len(chains))
    for source in sources:
        for position in _chain_order_positions(chains, source):
            yield ("width_chain_order_relocation", source, position)


def _try_chain_order(state, context, recipe):
    if not context.factory.budget.allows_search():
        return False
    action, source, position = recipe
    chains = state.current_plan.chains
    candidate = _relocate_chain(chains, source, position)
    if not _width_improves(candidate, state, context):
        return False
    return try_complete_candidate(
        state,
        context,
        candidate,
        affected_chain_ids=(chains[source].chain_id,),
        virtual_sequence=state.virtual_sequence,
        action_name=action,
        chain_order_only=True,
        width_optimization_only=True,
    )


def _try_width_recipe(state, context, recipe):
    action = recipe[0]
    if action == "delivery_intra_move":
        return _try_intra_move(state, context, recipe)
    if action in (
        "width_node_move",
        "width_node_exchange",
        "width_block_move",
        "width_block_exchange",
    ):
        return _try_segment_edit(state, context, recipe)
    if action == "width_chain_cut":
        return _try_chain_cut(state, context, recipe)
    if action == "width_chain_order_relocation":
        return _try_chain_order(state, context, recipe)
    raise ValueError(f"Unknown width optimization action: {action}")


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


def _scan_width_families(state, context, family_factories, try_recipe, *, delivery_directed=False):
    """Share remaining checks, resume rejected scans and restart after a real commit.

    Factories only enumerate index/range/placement recipes. The private callback
    builds one candidate and delegates acceptance to the shared candidate entry.
    Only this scanner consumes the quota; no second search state is created.
    Natural-completion continuation belongs to the eventual phase entry, not here.
    """
    _validate_search(state, context)
    budget = context.factory.budget
    timing = context.factory.cache.context.delivery_timing
    if delivery_directed and has_second_precision_delivery(context.factory.cache.rule_set):
        return _scan_critical_families(state, context, try_recipe)
    if delivery_directed and any(order.due_hours <= 0 for order in timing.orders.values()):
        return _scan_backlog_families(state, context, try_recipe)
    while budget.allows_search():
        pending = (_delivery_iterators(state, context) if delivery_directed else
                   [iter(factory(state, context)) for factory in family_factories])
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


def _scan_backlog_families(state, context, try_recipe):
    """Restart on the new plan, retaining only stable-ID visit preferences."""
    budget = context.factory.budget
    progress = [dict(after_order=None, targets={}) for _ in range(5)]
    next_family = 0
    while budget.allows_search():
        streams = _backlog_iterators(state, context, progress=progress)
        pending = deque((next_family + offset) % 5 for offset in range(5))
        accepted = False
        while pending and budget.allows_search():
            family = pending.popleft()
            accepted, exhausted = _scan_width_batch(state, context, streams[family], try_recipe, 64)
            if accepted:
                next_family = (family + 1) % 5
                break
            if not exhausted:
                pending.append(family)
        if not accepted:
            if budget.allows_search():
                budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
            return state
        # Every old iterator (including its exhausted flags) is now discarded.
    return state


def _scan_critical_families(state, context, try_recipe):
    """Alternate bounded lanes; retain visit hints, never old indices or rejections."""
    budget = context.factory.budget
    progress = [[dict(after_order=None, targets={}) for _ in range(5)] for _ in range(2)]
    next_family, next_lane = [0, 0], 0
    previous_critical, affected = set(), set()
    while budget.allows_search():
        before = state.current_plan
        positions, critical = critical_delivery_positions(before, context.factory.cache.context)
        critical_ids = frozenset(critical)
        revisit = tuple(key for key in critical if key in affected or key not in previous_critical)
        for cursor in progress[0]:
            for key in revisit:
                cursor["targets"].pop(key, None)
        streams = [_backlog_iterators(state, context, progress=progress[lane], positions=positions,
                                     critical_ids=critical_ids, critical_lane=lane == 0,
                                     revisit=revisit if lane == 0 else ()) for lane in range(2)]
        sizes = (3, 5)
        pending = [deque((next_family[lane] + offset) % size for offset in range(size))
                   for lane, size in enumerate(sizes)]
        accepted = False
        while any(pending) and budget.allows_search():
            lane = next_lane if pending[next_lane] else 1 - next_lane
            family = pending[lane].popleft()
            accepted, exhausted = _scan_width_batch(state, context, streams[lane][family], try_recipe, 64)
            next_family[lane] = (family + 1) % sizes[lane]
            next_lane = 1 - lane
            if accepted:
                break
            if not exhausted:
                pending[lane].append(family)
        if not accepted:
            if budget.allows_search():
                budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
            return state
        previous_critical = critical_ids
        old = {chain.chain_id: chain for chain in before.chains}
        new = {chain.chain_id: chain for chain in state.current_plan.chains}
        changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
        affected = {node.source_order_id for chain in (*before.chains, *state.current_plan.chains)
                    if chain.chain_id in changed for node in chain.nodes if node.virtual_lineage is None}
    return state


def run_width_optimization(state, context):
    """Refine one compliant plan after the old repair/split sequence, before audit."""
    _validate_search(state, context)
    if (
        chain_order_objective_index(context.factory.cache.rule_set) is None
        or not refinement_admissible(state.current_evaluation, context.factory.cache.rule_set)
    ):
        return state
    budget = context.factory.budget
    if budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE:
        budget.stop_reason = None
    return _scan_width_families(
        state,
        context,
        (_node_recipes, _block_recipes, _cut_recipes, _order_recipes),
        _try_width_recipe,
        delivery_directed=has_delivery_objective(context.factory.cache.rule_set),
    )
