"""Retired reference helpers from 7108523; test-only, never a production fallback."""


from collections import deque
from dataclasses import dataclass, replace
from itertools import permutations, zip_longest
from time import perf_counter
from types import MappingProxyType
import numpy as np


from apsgo_scheduler.core._numeric_evaluation import summarize_numeric_candidate, materialize_numeric_evaluation, _check_kernel_status
from apsgo_scheduler.core._numeric_batch import MAX_BATCH_CANDIDATES
from tests.core.numeric_reference_batch import pack_numeric_candidates, evaluate_numeric_batch, numeric_batch_result
from apsgo_scheduler.core._numeric_resources import NumericResourceExtension
from apsgo_scheduler.core._numeric_rules import NumericRuleKind
from apsgo_scheduler.core._numeric_search import NumericCandidateEdit, NumericSearchAction, _build_plan
from tests.core.numeric_reference_search import _assigned_period, _chain_weight, _edge_allowed, _join_with_bridge, _ordinary_bridge
from apsgo_scheduler.core._numeric_state import NumericPlanOverlay, readonly, split_target_periods_match
from apsgo_scheduler.core._numeric_units import NumericValueError, checked_product


from apsgo_scheduler.core.model import MaterialRole
from apsgo_scheduler.core._numeric_refinement import _maximum_chain_weight

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)
_PROPOSALS_PER_SOURCE = 4

@dataclass(frozen=True, slots=True)
class NumericRefinementIndex:
    """One read-only search index for one accepted numeric plan generation."""

    task: object
    plan: object
    chains: tuple[np.ndarray, ...]
    chain_lengths: np.ndarray
    chain_id_to_index: object
    source_positions: tuple[tuple[tuple[int, int], ...], ...]
    critical_prefix: np.ndarray

    @staticmethod
    def _critical_prefix(task, plan, critical_sources):
        critical_mask = np.zeros(task.originals.weight.size, dtype=np.bool_)
        if critical_sources:
            critical_mask[np.asarray(tuple(critical_sources), dtype=np.int64)] = True
        owners = task.nodes.source[plan.node_rows]
        marked = np.zeros(plan.node_rows.size, dtype=np.int64)
        real = owners >= 0
        marked[real] = critical_mask[owners[real]]
        return readonly(
            np.concatenate((np.zeros(1, dtype=np.int64), np.cumsum(marked))),
            np.int64,
        )

    @classmethod
    def build(cls, state, critical_sources=()):
        task, plan = state.task, state.plan
        chains = tuple(
            plan.node_rows[int(plan.chain_offsets[index]) : int(plan.chain_offsets[index + 1])]
            for index in range(plan.chain_ids.size)
        )
        positions = []
        for source in range(task.originals.weight.size):
            start = int(plan.source_piece_offsets[source])
            stop = int(plan.source_piece_offsets[source + 1])
            positions.append(
                tuple(
                    (
                        int(plan.row_to_chain[int(row)]),
                        int(plan.row_to_position[int(row)]),
                    )
                    for row in plan.source_piece_rows[start:stop]
                )
            )
        return cls(
            task,
            plan,
            chains,
            readonly(np.diff(plan.chain_offsets), np.int64),
            MappingProxyType(
                {int(identity): index for index, identity in enumerate(plan.chain_ids)}
            ),
            tuple(positions),
            cls._critical_prefix(task, plan, critical_sources),
        )

    def with_critical_sources(self, state, sources):
        if state.task is not self.task or state.plan is not self.plan:
            raise NumericValueError("refinement_index", "current plan generation required")
        return replace(
            self,
            critical_prefix=self._critical_prefix(self.task, self.plan, sources),
        )

    def chain_index(self, chain_id):
        try:
            return self.chain_id_to_index[int(chain_id)]
        except KeyError as error:
            raise NumericValueError(
                "refinement_recipe", "stable chain identity is absent from current plan"
            ) from error

    def _range_has_critical(self, chain, start, stop):
        offset = int(self.plan.chain_offsets[chain])
        return int(self.critical_prefix[offset + stop]) > int(
            self.critical_prefix[offset + start]
        )

    def recipe_is_critical(self, recipe):
        action, source_id, target_id, start, stop, other_start, other_stop = recipe
        source = self.chain_index(source_id)
        source_start = start if start >= 0 and stop > start else 0
        source_stop = stop if start >= 0 and stop > start else int(self.chain_lengths[source])
        if self._range_has_critical(source, source_start, source_stop):
            return True
        if action in {NumericSearchAction.NODE_EXCHANGE, NumericSearchAction.BLOCK_EXCHANGE}:
            target = self.chain_index(target_id)
            return self._range_has_critical(target, other_start, other_stop)
        return False

    def recipe_sources(self, recipe):
        action, source_id, target_id, start, stop, other_start, other_stop = recipe
        source = self.chain_index(source_id)
        rows = self.chains[source][start:stop] if start >= 0 and stop > start else self.chains[source]
        selections = [rows]
        if action in {NumericSearchAction.NODE_EXCHANGE, NumericSearchAction.BLOCK_EXCHANGE}:
            target = self.chain_index(target_id)
            selections.append(self.chains[target][other_start:other_stop])
        return {
            int(owner)
            for selection in selections
            for owner in self.task.nodes.source[selection]
            if int(owner) >= 0
        }

def _has_real(task, rows):
    return any(int(task.nodes.role[row]) != _GENERATED_VIRTUAL for row in rows)

def _source_positions(state, index=None):
    return (index or NumericRefinementIndex.build(state)).source_positions

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

def _critical_sources(state, index=None):
    task, plan, delivery = state.task, state.plan, state.evaluation.delivery
    completion = delivery.original_completion_ms
    due = task.originals.due_ms
    index = index or NumericRefinementIndex.build(state)
    positions = index.source_positions
    potential = {}
    for chain in range(plan.chain_ids.size):
        rows = index.chains[chain]
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
    present_sources = tuple(i for i, values in enumerate(positions) if values)
    backlog = sorted(
        (i for i in present_sources if bool(task.originals.old_backlog[i])),
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
        (i for i in present_sources if int(due[i]) > 0 and slack[i] < 0),
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

def _chain_order(state, index=None):
    task, plan = state.task, state.plan
    index = index or NumericRefinementIndex.build(state)

    def due(chain):
        owners = (
            int(task.nodes.source[row])
            for row in index.chains[chain]
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

def _direct_slots(state, row, target, positions, budget=None):
    row = int(row)
    direct, repaired = [], []
    for position in positions:
        if budget is not None and not budget.allows_search():
            break
        left = position == 0 or _edge_allowed(
            state.task, state.program, int(target[position - 1]), row
        )
        right = position == len(target) or _edge_allowed(
            state.task, state.program, row, int(target[position])
        )
        (direct if left and right else repaired).append(position)
    return tuple((*direct, *repaired))

def _alternate(first, second):
    missing = object()
    for values in zip_longest(first, second, fillvalue=missing):
        for value in values:
            if value is not missing:
                yield value

def _chain_recipe_stream(state, index, family, chain):
    """Generate each chain-owned candidate once in its established order."""
    plan = state.plan
    chains, chain_ids = index.chains, plan.chain_ids
    if family == "cut":
        new_id = max((int(value) for value in chain_ids), default=-1) + 1
        for cut in range(1, len(chains[chain])):
            for prefix, suffix in permutations(range(len(chains) + 1), 2):
                yield (
                    NumericSearchAction.CHAIN_CUT,
                    int(chain_ids[chain]),
                    new_id,
                    cut,
                    -1,
                    prefix,
                    suffix,
                )
        return
    if family == "order":
        for position in range(len(chains)):
            if position != chain and int(plan.chain_periods[position]) == int(
                plan.chain_periods[chain]
            ):
                yield (
                    NumericSearchAction.CHAIN_ORDER_RELOCATION,
                    int(chain_ids[chain]),
                    int(chain_ids[position]),
                    -1,
                    -1,
                    position,
                    -1,
                )

def _structural_ownership(index, ordered_sources, *, include_intervals):
    """Build position ranks once for every source stream in one family scan."""
    ranks = {
        position: rank
        for rank, owner in enumerate(ordered_sources)
        for position in reversed(index.source_positions[owner])
    }
    position_order = {
        position: order
        for owner in ordered_sources
        for order, position in enumerate(reversed(index.source_positions[owner]))
    }
    position_ranks = []
    position_orders = []
    for chain, rows in enumerate(index.chains):
        chain_ranks = np.full(len(rows), -1, dtype=np.int64)
        chain_orders = np.full(len(rows), -1, dtype=np.int64)
        for slot in range(len(rows)):
            position = (chain, slot)
            if position in ranks:
                chain_ranks[slot] = ranks[position]
                chain_orders[slot] = position_order[position]
        position_ranks.append(readonly(chain_ranks, np.int64))
        position_orders.append(readonly(chain_orders, np.int64))
    if not include_intervals:
        return tuple(position_ranks), (), ()

    default_rank = len(ranks)
    interval_ranks = []
    interval_owner_slots = []
    for chain_ranks, chain_orders in zip(position_ranks, position_orders):
        length = len(chain_ranks)
        owner_ranks = np.full((length, length + 1), default_rank, dtype=np.int64)
        owner_slots = np.full((length, length + 1), -1, dtype=np.int64)
        for start in range(length):
            best = (default_rank, default_rank)
            best_slot = -1
            for stop in range(start + 1, length + 1):
                slot = stop - 1
                rank = int(chain_ranks[slot])
                order = int(chain_orders[slot])
                if rank >= 0 and (rank, order) < best:
                    best = (rank, order)
                    best_slot = slot
                owner_ranks[start, stop] = best[0]
                owner_slots[start, stop] = best_slot
        interval_ranks.append(readonly(owner_ranks, np.int64))
        interval_owner_slots.append(readonly(owner_slots, np.int64))
    return tuple(position_ranks), tuple(interval_ranks), tuple(interval_owner_slots)

def _source_recipe_stream(
    state,
    source,
    family,
    *,
    ordered_sources=None,
    owner_sources=None,
    chain_recipe_streams=None,
    critical_lane=None,
    diagnostics=None,
    index=None,
    budget=None,
    ownership=None,
    ranked_chains=None,
):
    plan = state.plan
    index = index or NumericRefinementIndex.build(state)
    chains, chain_ids = index.chains, plan.chain_ids
    positions = tuple(reversed(index.source_positions[source]))
    ordered_sources = ordered_sources or _delivery_sources(state)
    owner_sources = owner_sources or ordered_sources
    def belongs_to_lane(is_critical):
        return critical_lane is None or is_critical is critical_lane

    if family == "intra":
        for chain, start in positions:
            if not belongs_to_lane(index._range_has_critical(chain, start, start + 1)):
                continue
            for target in range(start):
                yield (
                    NumericSearchAction.DELIVERY_INTRA_MOVE,
                    int(chain_ids[chain]),
                    int(chain_ids[chain]),
                    start,
                    start + 1,
                    target,
                    -1,
                )
        return

    if family == "node":
        position_ranks, _, _ = ownership or _structural_ownership(
            index, ordered_sources, include_intervals=False
        )

        def moves():
            for chain, start in positions:
                if not belongs_to_lane(index._range_has_critical(chain, start, start + 1)):
                    continue
                if len(chains[chain]) < 2:
                    continue
                row = chains[chain][start]
                for target in (*range(chain), *range(chain + 1, len(chains))):
                    slots = _direct_slots(
                        state,
                        row,
                        chains[target],
                        range(len(chains[target]) + 1),
                        budget,
                    )
                    for slot in slots:
                        yield (
                            NumericSearchAction.NODE_MOVE,
                            int(chain_ids[chain]),
                            int(chain_ids[target]),
                            start,
                            start + 1,
                            slot,
                            -1,
                        )

        def exchanges():
            for chain, start in positions:
                source_critical = index._range_has_critical(chain, start, start + 1)
                for target, slot in (
                    position
                    for owner in ordered_sources
                    for position in reversed(index.source_positions[owner])
                    if position[0] != chain
                    and int(position_ranks[chain][start])
                    < int(position_ranks[position[0]][position[1]])
                    and belongs_to_lane(
                        source_critical
                        or index._range_has_critical(position[0], position[1], position[1] + 1)
                    )
                ):
                    yield (
                        NumericSearchAction.NODE_EXCHANGE,
                        int(chain_ids[chain]),
                        int(chain_ids[target]),
                        start,
                        start + 1,
                        slot,
                        slot + 1,
                    )

        yield from _alternate(moves(), exchanges())
        return

    if family == "block":
        ranked_chains = ranked_chains or _chain_order(state, index)
        _, interval_ranks, interval_owner_slots = ownership or _structural_ownership(
            index, ordered_sources, include_intervals=True
        )

        def owner(chain, start, stop):
            return int(interval_ranks[chain][start, stop])

        def owner_position(chain, start, stop):
            slot = int(interval_owner_slots[chain][start, stop])
            return None if slot < 0 else (chain, slot)

        for chain, anchor in positions:
            maximum = min(4, len(chains[chain])) if critical_lane else len(chains[chain])
            for length in range(2, maximum):
                starts = range(
                    max(0, anchor - length + 1),
                    min(anchor + 1, len(chains[chain]) - length + 1),
                )
                for start in starts:
                    if owner_position(chain, start, start + length) != (chain, anchor):
                        continue
                    source_critical = index._range_has_critical(
                        chain, start, start + length
                    )
                    if critical_lane is False and source_critical:
                        continue
                    for target in ranked_chains:
                        if target == chain:
                            continue

                        def moves():
                            if not belongs_to_lane(source_critical):
                                return
                            for slot in range(len(chains[target]) + 1):
                                yield (
                                    NumericSearchAction.BLOCK_MOVE,
                                    int(chain_ids[chain]),
                                    int(chain_ids[target]),
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
                                    if not belongs_to_lane(
                                        source_critical
                                        or index._range_has_critical(
                                            target, slot, slot + size
                                        )
                                    ):
                                        continue
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
                                        int(chain_ids[chain]),
                                        int(chain_ids[target]),
                                        start,
                                        start + length,
                                        slot,
                                        slot + size,
                                    )

                        yield from _alternate(moves(), exchanges())
        return

    if family in {"cut", "order"}:
        if chain_recipe_streams is None:
            chain_owners = {}
            for candidate_owner in owner_sources:
                for chain, _ in reversed(index.source_positions[candidate_owner]):
                    chain_owners.setdefault(chain, candidate_owner)
            owned_chains = tuple(
                dict.fromkeys(
                    chain for chain, _ in positions if chain_owners.get(chain) == source
                )
            )
            chain_recipe_streams = {
                chain: iter(_chain_recipe_stream(state, index, family, chain))
                for chain in owned_chains
            }
        else:
            owned_chains = tuple(dict.fromkeys(chain for chain, _ in positions))
        owned_chains = tuple(
            chain
            for chain in owned_chains
            if belongs_to_lane(
                index._range_has_critical(chain, 0, int(index.chain_lengths[chain]))
            )
        )
        for chain in owned_chains:
            yield from chain_recipe_streams[chain]

def _family_stream(
    state,
    family,
    cursor,
    ordered_sources,
    critical,
    critical_lane,
    budget,
    diagnostics=None,
    index=None,
    defer_cursor=False,
):
    sources = _rotate_after(ordered_sources, cursor[family])
    ownership = (
        _structural_ownership(
            index,
            ordered_sources,
            include_intervals=family == "block",
        )
        if family in {"node", "block"}
        else None
    )
    ranked_chains = _chain_order(state, index) if family == "block" else None
    chain_recipe_streams = None
    if family in {"cut", "order"}:
        chain_recipe_streams = {}
        for source in sources:
            for chain, _ in reversed(index.source_positions[source]):
                if chain not in chain_recipe_streams:
                    chain_recipe_streams[chain] = iter(
                        _chain_recipe_stream(state, index, family, chain)
                    )
    lane = "critical" if critical_lane else "regular"
    key = f"{lane}:{family}"

    def source_stream(source):
        for recipe in _source_recipe_stream(
            state,
            source,
            family,
            ordered_sources=ordered_sources,
            owner_sources=sources,
            chain_recipe_streams=chain_recipe_streams,
            critical_lane=critical_lane is True,
            diagnostics=diagnostics,
            index=index,
            budget=budget,
            ownership=ownership,
            ranked_chains=ranked_chains,
        ):
            if not budget.allows_search():
                return
            if diagnostics is not None:
                diagnostics.record("raw_combinations", key)
            if diagnostics is not None:
                diagnostics.record("unique_combinations", key)
            if diagnostics is not None:
                diagnostics.record("routed_combinations", key)
            if defer_cursor:
                yield source, recipe
            else:
                cursor[family] = source
                yield recipe

    return _round_robin(
        (source_stream(source) for source in sources), budget, _PROPOSALS_PER_SOURCE
    )

def _reclamation_stream(state, diagnostics=None, index=None, budget=None):
    index = index or NumericRefinementIndex.build(state)
    chains, chain_ids = index.chains, state.plan.chain_ids
    for chain, rows in enumerate(chains):
        position = 0
        while position < len(rows):
            if budget is not None and not budget.allows_search():
                return
            if not _ordinary_bridge(state.task, rows[position]):
                position += 1
                continue
            start = position
            while position < len(rows) and _ordinary_bridge(state.task, rows[position]):
                position += 1
            stop = position
            yield (
                NumericSearchAction.BRIDGE_RECLAMATION,
                int(chain_ids[chain]),
                int(chain_ids[chain]),
                start,
                stop,
                -1,
                -1,
            )
            if stop - start > 1:
                for item in range(start, stop):
                    yield (
                        NumericSearchAction.BRIDGE_RECLAMATION,
                        int(chain_ids[chain]),
                        int(chain_ids[chain]),
                        item,
                        item + 1,
                        -1,
                        -1,
                    )

def _recipe_real_sources(state, recipe, diagnostics=None, index=None):
    return (index or NumericRefinementIndex.build(state)).recipe_sources(recipe)

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

def _candidate_overlay(task, current, chains, chain_ids, periods, *, group_periods=True):
    if group_periods:
        order = sorted(range(len(chains)), key=periods.__getitem__)
        chains = [chains[index] for index in order]
        chain_ids = [chain_ids[index] for index in order]
        periods = [periods[index] for index in order]
    return NumericPlanOverlay.build(task, current, chains, chain_ids, periods)

def _try_overlay_candidate(
    state,
    budget,
    edit,
    candidate_task,
    candidate_program,
    candidate_quality,
    overlay,
    affected_rows=(),
    *,
    virtual_sequence=None,
    split_sequence=None,
    reject_prohibited_kinds=(),
    diagnostics=None,
    diagnostic_key=None,
    _summary=None,
):
    if edit.sequence != budget.candidate_check_count:
        raise NumericValueError("candidate", "candidate sequence does not match consumed budget")
    if not split_target_periods_match(
        candidate_task, overlay.chains, overlay.chain_periods
    ):
        return False
    preview = _summary if _summary is not None else summarize_numeric_candidate(
        candidate_task,
        candidate_program,
        candidate_quality,
        overlay,
        state.task,
        state.program,
        state.quality,
        state.plan,
        state.evaluation,
    )
    _check_kernel_status(preview)
    if _summary is not None and diagnostics is not None:
        diagnostics.record("numeric_consumed", diagnostic_key)
    state.complete_candidate_evaluation_count += 1
    if any(
        preview.hits[:, rule.index].any()
        for rule in candidate_program.rules if rule.kind in reject_prohibited_kinds
    ):
        return False
    if not budget.allows_search() or not tuple(preview.quality) < tuple(
        state.evaluation.quality_key
    ):
        return False
    candidate = _build_plan(
        candidate_task,
        state.plan,
        overlay.chains,
        overlay.chain_ids,
        overlay.chain_periods,
        group_periods=False,
    )
    evaluation = materialize_numeric_evaluation(
        candidate_task,
        candidate_program,
        candidate_quality,
        candidate,
        preview,
    )
    if diagnostics is not None:
        diagnostics.record("plan_materializations", diagnostic_key)
    state.commit(
        candidate_task,
        candidate_program,
        candidate_quality,
        candidate,
        evaluation,
        edit,
        affected_rows,
        virtual_sequence=virtual_sequence,
        split_sequence=split_sequence,
    )
    return True

@dataclass(frozen=True, slots=True)
class PreparedRefinementCandidate:
    task: object
    program: object
    quality: object
    overlay: NumericPlanOverlay
    affected_rows: tuple
    virtual_sequence: int | None = None

def _prepare_repaired_parts(
    state,
    indices,
    parts,
    removed_rows,
    maximum_virtual_bridge_nodes,
    index=None,
):
    index = index or NumericRefinementIndex.build(state)
    chains = list(index.chains)
    chain_ids = [int(value) for value in state.plan.chain_ids]
    periods = [int(value) for value in state.plan.chain_periods]
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
                return None
            workspace, rows, sequence = joined
        if not _has_real(workspace.task, rows):
            return None
        changed.append(rows)
    maximum = _maximum_chain_weight(state)
    if maximum is not None and any(
        _chain_weight(workspace.task, rows) > maximum for rows in changed
    ):
        return None
    for chain_index, rows in zip(indices, changed):
        chains[chain_index] = rows
        periods[chain_index] = _assigned_period(workspace.task, rows)
    overlay = _candidate_overlay(workspace.task, state.plan, chains, chain_ids, periods)
    affected_rows = tuple(
        dict.fromkeys(row for pieces in parts for piece in pieces for row in piece)
    ) + tuple(
        dict.fromkeys(
            (*removed_rows, *range(state.task.nodes.weight.size, workspace.task.nodes.weight.size))
        )
    )
    return PreparedRefinementCandidate(
        workspace.task,
        workspace.program,
        workspace.quality,
        overlay,
        affected_rows,
        sequence,
    )

def _prepare_segment_recipe(
    state,
    recipe,
    maximum_virtual_bridge_nodes,
    index=None,
):
    action, source_id, target_id, start, stop, other_start, other_stop = recipe
    index = index or NumericRefinementIndex.build(state)
    source_index, target_index = index.chain_index(source_id), index.chain_index(target_id)
    source = tuple(int(value) for value in index.chains[source_index])
    target = tuple(int(value) for value in index.chains[target_index])
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
            yield None
            return
        parts = (
            (source[:start], exchanged, source[stop:]),
            (target[:other_start], moved, target[other_stop if exchange else other_start :]),
        )
        indices = (source_index, target_index)
    if any(
        not _has_real(state.task, tuple(row for piece in group for row in piece)) for group in parts
    ):
        yield None
        return
    cleaned, removed = zip(*(_trim_inner_bridges(state.task, group) for group in parts))
    cleaned_rows = tuple(row for group in removed for row in group)
    variants = [(tuple(cleaned), cleaned_rows)] if cleaned_rows else []
    variants.append((parts, ()))
    for variant, removed_rows in variants:
        yield _prepare_repaired_parts(
            state,
            indices,
            variant,
            removed_rows,
            maximum_virtual_bridge_nodes,
            index,
        )

def _prepare_chain_cut(state, recipe, index=None):
    _, source_id, new_id, cut, _, prefix_position, suffix_position = recipe
    index = index or NumericRefinementIndex.build(state)
    chains = list(index.chains)
    chain_ids = [int(value) for value in state.plan.chain_ids]
    periods = [int(value) for value in state.plan.chain_periods]
    source_index = index.chain_index(source_id)
    prefix = tuple(int(value) for value in chains[source_index][:cut])
    suffix = tuple(int(value) for value in chains[source_index][cut:])
    if not _has_real(state.task, prefix) or not _has_real(state.task, suffix):
        return None
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
        return None
    overlay = _candidate_overlay(
        state.task,
        state.plan,
        candidate_chains,
        candidate_ids,
        candidate_periods,
        group_periods=False,
    )
    return PreparedRefinementCandidate(
        state.task,
        state.program,
        state.quality,
        overlay,
        (*prefix, *suffix),
    )

def _prepare_order(state, recipe, index=None):
    _, source_id, target_id, _, _, position, _ = recipe
    index = index or NumericRefinementIndex.build(state)
    chains = list(index.chains)
    chain_ids = [int(value) for value in state.plan.chain_ids]
    periods = [int(value) for value in state.plan.chain_periods]
    source = index.chain_index(source_id)
    if position >= len(chains) or chain_ids[position] != target_id:
        return None
    moved = chains.pop(source)
    moved_id = chain_ids.pop(source)
    moved_period = periods.pop(source)
    chains.insert(position, moved)
    chain_ids.insert(position, moved_id)
    periods.insert(position, moved_period)
    overlay = _candidate_overlay(
        state.task, state.plan, chains, chain_ids, periods, group_periods=False
    )
    return PreparedRefinementCandidate(
        state.task,
        state.program,
        state.quality,
        overlay,
        tuple(map(int, moved)),
    )

def _prepare_reclaim(state, recipe, index=None):
    _, chain_id, _, start, stop, _, _ = recipe
    numeric_index = index or NumericRefinementIndex.build(state)
    chains = list(numeric_index.chains)
    chain_ids = [int(value) for value in state.plan.chain_ids]
    periods = [int(value) for value in state.plan.chain_periods]
    chain_index = numeric_index.chain_index(chain_id)
    original = chains[chain_index]
    removed = tuple(int(value) for value in original[start:stop])
    if not removed or not all(_ordinary_bridge(state.task, row) for row in removed):
        return None
    kept = tuple(int(value) for value in np.concatenate((original[:start], original[stop:])))
    if not _has_real(state.task, kept):
        return None
    if (
        start
        and stop < len(original)
        and not _edge_allowed(
            state.task, state.program, int(original[start - 1]), int(original[stop])
        )
    ):
        return None
    chains[chain_index] = kept
    overlay = _candidate_overlay(state.task, state.plan, chains, chain_ids, periods)
    return PreparedRefinementCandidate(
        state.task,
        state.program,
        state.quality,
        overlay,
        removed,
    )

def _prepare_recipe(state, recipe, maximum_virtual_bridge_nodes, index=None):
    action = recipe[0]
    if action in {
        NumericSearchAction.DELIVERY_INTRA_MOVE,
        NumericSearchAction.NODE_MOVE,
        NumericSearchAction.NODE_EXCHANGE,
        NumericSearchAction.BLOCK_MOVE,
        NumericSearchAction.BLOCK_EXCHANGE,
    }:
        yield from _prepare_segment_recipe(
            state,
            recipe,
            maximum_virtual_bridge_nodes,
            index,
        )
    elif action is NumericSearchAction.CHAIN_CUT:
        yield _prepare_chain_cut(state, recipe, index)
    elif action is NumericSearchAction.CHAIN_ORDER_RELOCATION:
        yield _prepare_order(state, recipe, index)
    elif action is NumericSearchAction.BRIDGE_RECLAMATION:
        yield _prepare_reclaim(state, recipe, index)
    else:
        raise NumericValueError("refinement", "known numeric refinement action required")

def _try_recipe(state, budget, recipe, maximum_virtual_bridge_nodes,
                diagnostics=None, diagnostic_key=None, index=None, prepared=None):
    attempts = iter(prepared) if prepared is not None else (
        (candidate, None) for candidate in _safe_prepare_recipe(
            state, recipe, maximum_virtual_bridge_nodes, index
        )
    )
    position = 0
    while True:
        # Pull the next attempt only after the previous variant was rejected.
        # The second repair still consumes its own logical check, not a new proposal.
        try:
            candidate, summary = next(attempts)
        except StopIteration:
            return False
        if position and not budget.consume_candidate_check():
            return False
        position += 1
        if isinstance(candidate, NumericValueError):
            raise candidate
        if candidate is None:
            continue
        edit = _edit_for(state, recipe, budget.candidate_check_count)
        if _try_overlay_candidate(
            state, budget, edit, candidate.task, candidate.program, candidate.quality,
            candidate.overlay, candidate.affected_rows, virtual_sequence=candidate.virtual_sequence,
            reject_prohibited_kinds=tuple(NumericRuleKind), diagnostics=diagnostics,
            diagnostic_key=diagnostic_key, _summary=summary,
        ):
            return True

def _safe_prepare_recipe(state, recipe, maximum_virtual_bridge_nodes, index):
    # A speculative error belongs to its logical candidate, not an earlier one.
    try:
        yield from _prepare_recipe(state, recipe, maximum_virtual_bridge_nodes, index)
    except NumericValueError as error:
        yield error

def _prepare_recipe_batch(state, budget, recipes, maximum_virtual_bridge_nodes,
                          index, diagnostics, key):
    prepared = [[] for _ in recipes]
    pending = []
    sequence = budget.candidate_check_count
    started = perf_counter()
    for position, recipe in enumerate(recipes):
        attempts = _safe_prepare_recipe(state, recipe, maximum_virtual_bridge_nodes, index)
        while budget.allows_search():
            try:
                candidate = next(attempts)
            except StopIteration:
                break
            sequence += 1
            slot = len(prepared[position])
            prepared[position].append((candidate, None))
            if sequence <= budget.candidate_check_limit and isinstance(candidate, PreparedRefinementCandidate) and split_target_periods_match(
                candidate.task, candidate.overlay.chains, candidate.overlay.chain_periods
            ):
                pending.append((position, slot, sequence, recipe[0], candidate))
    if diagnostics is not None:
        diagnostics.numeric_batch_prepare_seconds += perf_counter() - started
    offset = 0
    while offset < len(pending) and budget.allows_search():
        size = min(MAX_BATCH_CANDIDATES, len(pending) - offset)
        while True:
            started = perf_counter()
            chunk = pending[offset:offset + size]
            try:
                flat = pack_numeric_candidates(
                    state.task, state.program, state.quality, state.plan, state.evaluation,
                    [(c.task, c.program, c.quality, c.overlay) for *_, c in chunk],
                    [sequence for _, _, sequence, _, _ in chunk],
                    [tuple(NumericSearchAction).index(action) for _, _, _, action, _ in chunk],
                )
            except NumericValueError as error:
                if error.path != "candidate_batch.capacity":
                    raise
                if size == 1:
                    # An exceptional large candidate uses the same single kernel
                    # when consumed; it never allocates an unbounded batch buffer.
                    flat = None
                    break
                size = max(1, size // 2)
            else:
                break
            finally:
                if diagnostics is not None:
                    diagnostics.numeric_batch_prepare_seconds += perf_counter() - started
        if flat is not None:
            if not budget.allows_search():
                break
            started = perf_counter()
            output = evaluate_numeric_batch(flat, state.task, state.program, state.quality, state.plan)
            elapsed = perf_counter() - started
            for i, (position, slot, _, _, candidate) in enumerate(chunk):
                prepared[position][slot] = (candidate, numeric_batch_result(flat, output, i))
            if diagnostics is not None:
                diagnostics.record("numeric_precomputed", key, size)
                diagnostics.numeric_batch_calls += 1
                diagnostics.numeric_batch_evaluate_seconds += elapsed
                diagnostics.maximum_numeric_batch_bytes = max(
                    diagnostics.maximum_numeric_batch_bytes, flat.estimated_bytes
                )
                diagnostics.maximum_numeric_batch_seconds = max(
                    diagnostics.maximum_numeric_batch_seconds, elapsed
                )
        offset += size
    return prepared
