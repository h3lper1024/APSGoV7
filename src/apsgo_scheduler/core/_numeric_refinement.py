"""Deterministic post-search structural refinement on the numeric state."""

from collections import deque
from itertools import permutations, zip_longest
from time import perf_counter

from ._numeric_resources import NumericResourceExtension
from ._numeric_rules import NumericRuleKind
from ._numeric_search import (
    NumericCandidateEdit,
    NumericSearchAction,
    NumericSearchCheckpoint,
    NumericSearchState,
    _assigned_period,
    _build_plan,
    _chain_rows,
    _chain_weight,
    _edge_allowed,
    _join_with_bridge,
    _layout,
    _ordinary_bridge,
    _run_numeric_local_search,
    _try_prepared_candidate,
    _validate_search_inputs,
    improve_numeric_controlled_split,
)
from ._numeric_units import NumericValueError, checked_product
from .budget import SolveRuntimeBudget
from .contracts import SearchStopReason
from .model import MaterialRole

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)
_PROPOSALS_PER_SOURCE = 4
_PROPOSALS_PER_FAMILY = 64
_CRITICAL_FAMILY_COUNT = len(("intra", "node", "block"))
_REGULAR_FAMILY_COUNT = 6
_FAMILIES = ("intra", "node", "block", "cut", "order", "reclaim")


def _has_real(task, rows):
    return any(int(task.nodes.role[row]) != _GENERATED_VIRTUAL for row in rows)


def _source_positions(state):
    result = {source: [] for source in range(state.task.originals.weight.size)}
    for chain in range(state.plan.chain_ids.size):
        for position, row in enumerate(_chain_rows(state.plan, chain)):
            source = int(state.task.nodes.source[row])
            if source >= 0:
                result[source].append((chain, position))
    return result


def _delivery_sources(state):
    task, delivery = state.task, state.evaluation.delivery
    due = task.originals.due_ms
    completion = delivery.original_completion_ms
    slack = [int(due[i]) - int(completion[i]) for i in range(due.size)]
    late = sorted(
        (i for i in range(due.size) if int(due[i]) > 0 and slack[i] < 0),
        key=lambda i: (slack[i], int(due[i]), i),
    )
    on_time = sorted(
        (i for i in range(due.size) if int(due[i]) > 0 and slack[i] >= 0),
        key=lambda i: (slack[i], i),
    )
    backlog = sorted(
        (i for i in range(due.size) if bool(task.originals.old_backlog[i])),
        key=lambda i: (
            -int(completion[i]),
            -checked_product(int(task.originals.weight[i]), int(completion[i]), "backlog_priority"),
            i,
        ),
    )
    if backlog:
        return tuple(
            value
            for group in zip_longest(late, backlog, on_time)
            for value in group
            if value is not None
        )
    return tuple(late + on_time)


def _critical_sources(state):
    task, plan, delivery = state.task, state.plan, state.evaluation.delivery
    completion = delivery.original_completion_ms
    due = task.originals.due_ms
    positions = _source_positions(state)
    potential = {}
    for chain in range(plan.chain_ids.size):
        rows = _chain_rows(plan, chain)
        real = tuple(row for row in rows if int(task.nodes.source[row]) >= 0)
        earliest = min(int(task.nodes.source_period[row]) for row in real)
        owners = {
            int(task.nodes.source[row])
            for row in real
            if int(task.nodes.source_period[row]) == earliest
        }
        if len(owners) != 1:
            continue
        owner = next(iter(owners))
        remaining = tuple(row for row in rows if int(task.nodes.source[row]) != owner)
        real_remaining = tuple(row for row in remaining if int(task.nodes.source[row]) >= 0)
        if not real_remaining:
            continue
        later = min(int(task.nodes.source_period[row]) for row in real_remaining)
        if later <= earliest:
            continue
        if any(
            int(task.nodes.split_group[row]) >= 0
            and int(task.split_groups.target_period[int(task.nodes.split_group[row])]) != later
            for row in real_remaining
        ):
            continue
        potential[owner] = max(
            potential.get(owner, 0),
            sum(int(task.nodes.duration_ms[row]) for row in remaining),
        )
    slack = [int(due[i]) - int(completion[i]) for i in range(due.size)]
    backlog = sorted(
        (i for i in positions if bool(task.originals.old_backlog[i])),
        key=lambda i: (
            -int(completion[i]),
            -checked_product(int(task.originals.weight[i]), int(completion[i]), "critical_backlog"),
            i,
        ),
    )
    releasable = sorted(
        potential,
        key=lambda i: (-potential[i], -int(completion[i]), i),
    )
    late = sorted(
        (i for i in positions if int(due[i]) > 0 and slack[i] < 0),
        key=lambda i: (
            checked_product(int(task.originals.weight[i]), slack[i], "critical_late"),
            slack[i],
            i,
        ),
    )
    return tuple(
        dict.fromkeys(
            value
            for group in zip_longest(backlog, releasable, late)
            for value in group
            if value is not None
        )
    )


def _chain_order(state):
    task, plan = state.task, state.plan

    def due(chain):
        owners = (
            int(task.nodes.source[row])
            for row in _chain_rows(plan, chain)
            if int(task.nodes.source[row]) >= 0
        )
        return min(max(0, int(task.originals.due_ms[source])) for source in owners)

    return tuple(sorted(range(plan.chain_ids.size), key=lambda chain: (due(chain), chain)))


def _rotate_after(values, previous):
    values = tuple(values)
    offset = values.index(previous) + 1 if previous in values else 0
    return values[offset:] + values[:offset]


def _round_robin(streams, budget, allowance):
    pending = deque(iter(stream) for stream in streams)
    while pending and budget.allows_search():
        stream = pending.popleft()
        for _ in range(allowance):
            if not budget.allows_search():
                return
            try:
                yield next(stream)
            except StopIteration:
                break
        else:
            pending.append(stream)


def _direct_slots(state, row, target, positions):
    direct, repaired = [], []
    for position in positions:
        left = position == 0 or _edge_allowed(state.task, state.program, target[position - 1], row)
        right = position == len(target) or _edge_allowed(
            state.task, state.program, row, target[position]
        )
        (direct if left and right else repaired).append(position)
    return tuple((*direct, *repaired))


def _alternate(first, second):
    missing = object()
    for values in zip_longest(first, second, fillvalue=missing):
        for value in values:
            if value is not missing:
                yield value


def _source_recipe_stream(state, source, family, *, ordered_sources=None, critical_lane=False):
    plan = state.plan
    chains, chain_ids, _ = _layout(plan)
    positions = tuple(reversed(_source_positions(state)[source]))
    ordered_sources = ordered_sources or _delivery_sources(state)
    ranked_chains = _chain_order(state)

    if family == "intra":
        for chain, start in positions:
            for target in range(start):
                yield (
                    NumericSearchAction.DELIVERY_INTRA_MOVE,
                    chain_ids[chain],
                    chain_ids[chain],
                    start,
                    start + 1,
                    target,
                    -1,
                )
        return

    if family == "node":
        ranks = {
            position: index
            for index, owner in enumerate(ordered_sources)
            for position in reversed(_source_positions(state)[owner])
        }

        def moves():
            for chain, start in positions:
                if len(chains[chain]) < 2:
                    continue
                row = chains[chain][start]
                for target in (*range(chain), *range(chain + 1, len(chains))):
                    slots = _direct_slots(
                        state, row, chains[target], range(len(chains[target]) + 1)
                    )
                    for slot in slots:
                        yield (
                            NumericSearchAction.NODE_MOVE,
                            chain_ids[chain],
                            chain_ids[target],
                            start,
                            start + 1,
                            slot,
                            -1,
                        )

        def exchanges():
            for chain, start in positions:
                for target, slot in (
                    position
                    for owner in ordered_sources
                    for position in reversed(_source_positions(state)[owner])
                    if position[0] != chain
                    and ranks.get((chain, start), -1) < ranks.get(position, -1)
                ):
                    yield (
                        NumericSearchAction.NODE_EXCHANGE,
                        chain_ids[chain],
                        chain_ids[target],
                        start,
                        start + 1,
                        slot,
                        slot + 1,
                    )

        yield from _alternate(moves(), exchanges())
        return

    if family == "block":
        ranks = {
            position: index
            for index, owner in enumerate(ordered_sources)
            for position in reversed(_source_positions(state)[owner])
        }

        def owner(chain, start, stop):
            return min(
                (ranks[(chain, slot)] for slot in range(start, stop) if (chain, slot) in ranks),
                default=len(ranks),
            )

        for chain, anchor in positions:
            maximum = min(4, len(chains[chain])) if critical_lane else len(chains[chain])
            for length in range(2, maximum):
                starts = range(
                    max(0, anchor - length + 1),
                    min(anchor + 1, len(chains[chain]) - length + 1),
                )
                for start in starts:
                    if owner(chain, start, start + length) != ranks[(chain, anchor)]:
                        continue
                    for target in ranked_chains:
                        if target == chain:
                            continue

                        def moves():
                            for slot in range(len(chains[target]) + 1):
                                yield (
                                    NumericSearchAction.BLOCK_MOVE,
                                    chain_ids[chain],
                                    chain_ids[target],
                                    start,
                                    start + length,
                                    slot,
                                    -1,
                                )

                        def exchanges():
                            maximum = (
                                min(4, len(chains[target]))
                                if critical_lane
                                else len(chains[target])
                            )
                            for size in range(1, maximum):
                                for slot in range(len(chains[target]) - size + 1):
                                    if size > 1 and (
                                        owner(chain, start, start + length),
                                        chain,
                                        start,
                                        start + length,
                                    ) > (
                                        owner(target, slot, slot + size),
                                        target,
                                        slot,
                                        slot + size,
                                    ):
                                        continue
                                    yield (
                                        NumericSearchAction.BLOCK_EXCHANGE,
                                        chain_ids[chain],
                                        chain_ids[target],
                                        start,
                                        start + length,
                                        slot,
                                        slot + size,
                                    )

                        yield from _alternate(moves(), exchanges())
        return

    owned_chains = tuple(dict.fromkeys(chain for chain, _ in positions))
    if family == "cut":
        new_id = max(chain_ids, default=-1) + 1
        for chain in owned_chains:
            for cut in range(1, len(chains[chain])):
                for prefix, suffix in permutations(range(len(chains) + 1), 2):
                    yield (
                        NumericSearchAction.CHAIN_CUT,
                        chain_ids[chain],
                        new_id,
                        cut,
                        -1,
                        prefix,
                        suffix,
                    )
        return
    if family == "order":
        for chain in owned_chains:
            for position in range(len(chains)):
                if position != chain and int(plan.chain_periods[position]) == int(
                    plan.chain_periods[chain]
                ):
                    yield (
                        NumericSearchAction.CHAIN_ORDER_RELOCATION,
                        chain_ids[chain],
                        chain_ids[position],
                        -1,
                        -1,
                        position,
                        -1,
                    )


def _family_stream(state, family, cursor, ordered_sources, critical, critical_lane, budget):
    sources = _rotate_after(ordered_sources, cursor[family])

    def source_stream(source):
        for recipe in _source_recipe_stream(
            state,
            source,
            family,
            ordered_sources=ordered_sources,
            critical_lane=critical_lane is True,
        ):
            rows = _recipe_real_sources(state, recipe)
            is_critical = bool(rows & critical)
            if critical_lane is not None and is_critical is not critical_lane:
                continue
            cursor[family] = source
            yield recipe

    return _round_robin(
        (source_stream(source) for source in sources), budget, _PROPOSALS_PER_SOURCE
    )


def _reclamation_stream(state):
    chains, chain_ids, _ = _layout(state.plan)
    for chain, rows in enumerate(chains):
        position = 0
        while position < len(rows):
            if not _ordinary_bridge(state.task, rows[position]):
                position += 1
                continue
            start = position
            while position < len(rows) and _ordinary_bridge(state.task, rows[position]):
                position += 1
            stop = position
            yield (
                NumericSearchAction.BRIDGE_RECLAMATION,
                chain_ids[chain],
                chain_ids[chain],
                start,
                stop,
                -1,
                -1,
            )
            if stop - start > 1:
                for item in range(start, stop):
                    yield (
                        NumericSearchAction.BRIDGE_RECLAMATION,
                        chain_ids[chain],
                        chain_ids[chain],
                        item,
                        item + 1,
                        -1,
                        -1,
                    )


def _recipe_real_sources(state, recipe):
    action, source_id, target_id, start, stop, other_start, other_stop = recipe
    chains, chain_ids, _ = _layout(state.plan)
    source = chains[chain_ids.index(source_id)]
    rows = source[start:stop] if start >= 0 and stop > start else source
    if action in {NumericSearchAction.NODE_EXCHANGE, NumericSearchAction.BLOCK_EXCHANGE}:
        target = chains[chain_ids.index(target_id)]
        rows += target[other_start:other_stop]
    return {
        int(state.task.nodes.source[row]) for row in rows if int(state.task.nodes.source[row]) >= 0
    }


def _trim_inner_bridges(task, pieces):
    trimmed, removed = [], []
    for index, piece in enumerate(pieces):
        left, right = 0, len(piece)
        while index and left < right and _ordinary_bridge(task, piece[left]):
            removed.append(piece[left])
            left += 1
        while index < len(pieces) - 1 and right > left and _ordinary_bridge(task, piece[right - 1]):
            right -= 1
            removed.append(piece[right])
        trimmed.append(piece[left:right])
    return tuple(trimmed), tuple(removed)


def _maximum_chain_weight(state):
    rules = state.program.for_kind(NumericRuleKind.CHAIN_WEIGHT)
    return None if not rules else rules[0].values[1]


def _edit_for(state, recipe, sequence):
    action, source_id, target_id, start, stop, other_start, other_stop = recipe
    values = dict(
        task_fingerprint=state.task.fingerprint,
        plan_fingerprint=state.plan.fingerprint,
        generation=state.plan.generation,
        sequence=sequence,
        action=action,
        source_chain_id=source_id,
        target_chain_id=target_id,
    )
    if action in {
        NumericSearchAction.DELIVERY_INTRA_MOVE,
        NumericSearchAction.NODE_MOVE,
        NumericSearchAction.BLOCK_MOVE,
    }:
        values.update(target_position=other_start, source_start=start, source_stop=stop)
    elif action in {NumericSearchAction.NODE_EXCHANGE, NumericSearchAction.BLOCK_EXCHANGE}:
        values.update(
            source_start=start, source_stop=stop, target_start=other_start, target_stop=other_stop
        )
    elif action is NumericSearchAction.CHAIN_CUT:
        values.update(source_start=start, target_start=other_start, target_stop=other_stop)
    elif action is NumericSearchAction.BRIDGE_RECLAMATION:
        values.update(source_start=start, source_stop=stop)
    else:
        values.update(target_position=other_start)
    return NumericCandidateEdit(**values)


def _try_repaired_parts(
    state, budget, indices, parts, removed_rows, edit, maximum_virtual_bridge_nodes
):
    chains, chain_ids, periods = _layout(state.plan)
    workspace = NumericResourceExtension(state.task, state.program, state.quality, ())
    sequence = state.virtual_sequence
    changed = []
    for pieces in parts:
        rows = ()
        for piece in pieces:
            joined = _join_with_bridge(
                workspace, rows, piece, maximum_virtual_bridge_nodes, sequence
            )
            if joined is None:
                return False
            workspace, rows, sequence = joined
        if not _has_real(workspace.task, rows):
            return False
        changed.append(rows)
    maximum = _maximum_chain_weight(state)
    if maximum is not None and any(
        _chain_weight(workspace.task, rows) > maximum for rows in changed
    ):
        return False
    for index, rows in zip(indices, changed):
        chains[index] = rows
        periods[index] = _assigned_period(workspace.task, rows)
    candidate = _build_plan(workspace.task, state.plan, chains, chain_ids, periods)
    affected_rows = tuple(
        dict.fromkeys(row for pieces in parts for piece in pieces for row in piece)
    ) + tuple(
        dict.fromkeys(
            (*removed_rows, *range(state.task.nodes.weight.size, workspace.task.nodes.weight.size))
        )
    )
    return _try_prepared_candidate(
        state,
        budget,
        edit,
        workspace.task,
        workspace.program,
        workspace.quality,
        candidate,
        affected_rows,
        virtual_sequence=sequence,
        reject_prohibited_kinds=tuple(NumericRuleKind),
    )


def _try_segment_recipe(state, budget, recipe, maximum_virtual_bridge_nodes):
    action, source_id, target_id, start, stop, other_start, other_stop = recipe
    chains, chain_ids, _ = _layout(state.plan)
    source_index, target_index = chain_ids.index(source_id), chain_ids.index(target_id)
    source, target = chains[source_index], chains[target_index]
    if action is NumericSearchAction.DELIVERY_INTRA_MOVE:
        moved = source[start:stop]
        parts = ((source[:other_start], moved, source[other_start:start], source[stop:]),)
        indices = (source_index,)
    else:
        moved = source[start:stop]
        exchange = action in {
            NumericSearchAction.NODE_EXCHANGE,
            NumericSearchAction.BLOCK_EXCHANGE,
        }
        exchanged = target[other_start:other_stop] if exchange else ()
        if not _has_real(state.task, moved) or (exchanged and not _has_real(state.task, exchanged)):
            return False
        parts = (
            (source[:start], exchanged, source[stop:]),
            (target[:other_start], moved, target[other_stop if exchange else other_start :]),
        )
        indices = (source_index, target_index)
    if any(
        not _has_real(state.task, tuple(row for piece in group for row in piece)) for group in parts
    ):
        return False
    cleaned, removed = zip(*(_trim_inner_bridges(state.task, group) for group in parts))
    cleaned_rows = tuple(row for group in removed for row in group)
    variants = [(tuple(cleaned), cleaned_rows)] if cleaned_rows else []
    variants.append((parts, ()))
    for position, (variant, removed_rows) in enumerate(variants):
        if position and not budget.consume_candidate_check():
            return False
        edit = _edit_for(state, recipe, budget.candidate_check_count)
        if _try_repaired_parts(
            state,
            budget,
            indices,
            variant,
            removed_rows,
            edit,
            maximum_virtual_bridge_nodes,
        ):
            return True
    return False


def _try_chain_cut(state, budget, recipe):
    _, source_id, new_id, cut, _, prefix_position, suffix_position = recipe
    chains, chain_ids, periods = _layout(state.plan)
    source_index = chain_ids.index(source_id)
    prefix, suffix = chains[source_index][:cut], chains[source_index][cut:]
    if not _has_real(state.task, prefix) or not _has_real(state.task, suffix):
        return False
    remaining = iter(
        (chain, identity, period)
        for index, (chain, identity, period) in enumerate(zip(chains, chain_ids, periods))
        if index != source_index
    )
    candidate_chains, candidate_ids, candidate_periods = [], [], []
    for position in range(len(chains) + 1):
        if position == prefix_position:
            values = (prefix, source_id, _assigned_period(state.task, prefix))
        elif position == suffix_position:
            values = (suffix, new_id, _assigned_period(state.task, suffix))
        else:
            values = next(remaining)
        candidate_chains.append(values[0])
        candidate_ids.append(values[1])
        candidate_periods.append(values[2])
    if any(left > right for left, right in zip(candidate_periods, candidate_periods[1:])):
        return False
    candidate = _build_plan(
        state.task,
        state.plan,
        candidate_chains,
        candidate_ids,
        candidate_periods,
        group_periods=False,
    )
    edit = _edit_for(state, recipe, budget.candidate_check_count)
    return _try_prepared_candidate(
        state,
        budget,
        edit,
        state.task,
        state.program,
        state.quality,
        candidate,
        (*prefix, *suffix),
        reject_prohibited_kinds=tuple(NumericRuleKind),
    )


def _try_order(state, budget, recipe):
    _, source_id, target_id, _, _, position, _ = recipe
    chains, chain_ids, periods = _layout(state.plan)
    source = chain_ids.index(source_id)
    if position >= len(chains) or chain_ids[position] != target_id:
        return False
    moved = chains.pop(source)
    moved_id = chain_ids.pop(source)
    moved_period = periods.pop(source)
    chains.insert(position, moved)
    chain_ids.insert(position, moved_id)
    periods.insert(position, moved_period)
    candidate = _build_plan(state.task, state.plan, chains, chain_ids, periods, group_periods=False)
    edit = _edit_for(state, recipe, budget.candidate_check_count)
    return _try_prepared_candidate(
        state,
        budget,
        edit,
        state.task,
        state.program,
        state.quality,
        candidate,
        moved,
        reject_prohibited_kinds=tuple(NumericRuleKind),
    )


def _try_reclaim(state, budget, recipe):
    _, chain_id, _, start, stop, _, _ = recipe
    chains, chain_ids, periods = _layout(state.plan)
    index = chain_ids.index(chain_id)
    removed = chains[index][start:stop]
    if not removed or not all(_ordinary_bridge(state.task, row) for row in removed):
        return False
    kept = chains[index][:start] + chains[index][stop:]
    if not _has_real(state.task, kept):
        return False
    if (
        start
        and stop < len(chains[index])
        and not _edge_allowed(
            state.task, state.program, chains[index][start - 1], chains[index][stop]
        )
    ):
        return False
    chains[index] = kept
    candidate = _build_plan(state.task, state.plan, chains, chain_ids, periods)
    edit = _edit_for(state, recipe, budget.candidate_check_count)
    return _try_prepared_candidate(
        state,
        budget,
        edit,
        state.task,
        state.program,
        state.quality,
        candidate,
        removed,
        reject_prohibited_kinds=tuple(NumericRuleKind),
    )


def _try_recipe(state, budget, recipe, maximum_virtual_bridge_nodes):
    action = recipe[0]
    if action in {
        NumericSearchAction.DELIVERY_INTRA_MOVE,
        NumericSearchAction.NODE_MOVE,
        NumericSearchAction.NODE_EXCHANGE,
        NumericSearchAction.BLOCK_MOVE,
        NumericSearchAction.BLOCK_EXCHANGE,
    }:
        return _try_segment_recipe(state, budget, recipe, maximum_virtual_bridge_nodes)
    if action is NumericSearchAction.CHAIN_CUT:
        return _try_chain_cut(state, budget, recipe)
    if action is NumericSearchAction.CHAIN_ORDER_RELOCATION:
        return _try_order(state, budget, recipe)
    if action is NumericSearchAction.BRIDGE_RECLAMATION:
        return _try_reclaim(state, budget, recipe)
    raise NumericValueError("refinement", "known numeric refinement action required")


def _scan_family(state, budget, recipes, maximum_virtual_bridge_nodes=2):
    for _ in range(_PROPOSALS_PER_FAMILY):
        if not budget.allows_search():
            return False, False
        try:
            recipe = next(recipes)
        except StopIteration:
            return False, True
        if not budget.consume_candidate_check():
            return False, False
        if _try_recipe(state, budget, recipe, maximum_virtual_bridge_nodes):
            return True, False
    return False, False


def improve_numeric_refinement(state, budget, *, maximum_virtual_bridge_nodes=2):
    """Alternate critical and regular action families until no improvement remains."""
    _validate_search_inputs(state.task, state.program, state.quality, state, budget)
    if type(maximum_virtual_bridge_nodes) is not int or not 0 <= maximum_virtual_bridge_nodes <= 2:
        raise NumericValueError(
            "maximum_virtual_bridge_nodes", "zero, one or two bridge nodes required"
        )
    if any(value.prohibited for value in state.evaluation.violations):
        return state
    if budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE:
        budget.stop_reason = None
    cursors = [{family: None for family in _FAMILIES} for _ in range(2)]
    next_family, next_lane = [0, 0], 0
    first_cleanup = True
    while budget.allows_search():
        critical_order = _critical_sources(state)
        critical = frozenset(critical_order)
        ordered_sources = (
            *critical_order,
            *(source for source in _delivery_sources(state) if source not in critical),
        )
        streams = [
            [
                _family_stream(
                    state,
                    family,
                    cursors[lane],
                    ordered_sources,
                    critical,
                    lane == 0,
                    budget,
                )
                for family in _FAMILIES[: _CRITICAL_FAMILY_COUNT if lane == 0 else -1]
            ]
            for lane in range(2)
        ]
        cleanup = iter(_reclamation_stream(state))
        streams[1].append(cleanup)
        sizes = (_CRITICAL_FAMILY_COUNT, _REGULAR_FAMILY_COUNT)
        pending = [
            deque((next_family[lane] + offset) % size for offset in range(size))
            for lane, size in enumerate(sizes)
        ]
        accepted = False
        if first_cleanup:
            first_cleanup = False
            accepted, _ = _scan_family(state, budget, cleanup, maximum_virtual_bridge_nodes)
        while not accepted and any(pending) and budget.allows_search():
            lane = next_lane if pending[next_lane] else 1 - next_lane
            family = pending[lane].popleft()
            accepted, exhausted = _scan_family(
                state,
                budget,
                streams[lane][family],
                maximum_virtual_bridge_nodes,
            )
            next_family[lane] = (family + 1) % sizes[lane]
            next_lane = 1 - lane
            if not accepted and not exhausted:
                pending[lane].append(family)
        if not accepted:
            if budget.allows_search():
                budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
            return state
    return state


def run_numeric_serial_search(
    task,
    program,
    quality,
    initial,
    budget,
    *,
    pair_scan_slack_weight,
    maximum_virtual_bridge_nodes=2,
):
    """Run construction output through local search, split replay and refinement."""
    if not isinstance(budget, SolveRuntimeBudget):
        raise NumericValueError("budget", "shared runtime budget required")
    started = perf_counter()
    state = NumericSearchState.start(task, program, quality, initial)
    _run_numeric_local_search(
        state,
        budget,
        pair_scan_slack_weight=pair_scan_slack_weight,
        maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes,
    )
    if budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE:
        improve_numeric_controlled_split(
            state,
            budget,
            pair_scan_slack_weight=pair_scan_slack_weight,
            maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes,
        )
    if budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE:
        improve_numeric_refinement(
            state,
            budget,
            maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes,
        )
    return state, NumericSearchCheckpoint.capture(state.task, state, budget, started)
