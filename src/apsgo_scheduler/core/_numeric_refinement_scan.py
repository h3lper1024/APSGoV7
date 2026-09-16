"""Native refinement enumeration; stage control alone owns quota and acceptance.

Compiled generators retain native loop cursors. A negative action is a bounded
continuation, not a proposal: the caller checks cancellation and resumes it.
"""
from collections import namedtuple, deque
from functools import wraps

import numpy as np
from numba import njit

from ._numeric_candidate_kernel import (
    INTRA, NODE_MOVE, NODE_SWAP, BLOCK_MOVE, BLOCK_SWAP, CUT, ORDER, RECLAIM,
    ACTION, SOURCE, TARGET, POSITION, START, STOP, OTHER_START, OTHER_STOP, OWNER,
    REVERSE_SOURCE, REVERSE_TARGET, VARIANT,
)
from ._numeric_kernel import _add, _mul, edge_node, all_edges_allowed_values
from ._numeric_state import OK, DESCRIPTOR_FIELDS, readonly
from ._numeric_units import NumericValueError
from .model import MaterialRole, VirtualPurpose

ScanColumns = namedtuple("ScanColumns", (
    "rows offsets ids periods piece_rows piece_offsets row_chain row_position "
    "critical ranks interval_offsets interval_ranks interval_slots ranked_chains sources"
))
_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)
_BRIDGE = tuple(VirtualPurpose).index(VirtualPurpose.EDGE_BRIDGE)
CONTINUE, ERROR = -1, -2
_DESCRIPTOR_SIZE = len(DESCRIPTOR_FIELDS)


def _managed_scan(function):
    """Finish native frames on early close without enumerating another item.

    The native generator has no close method. Each suspension point therefore
    checks a private stop flag before doing any more work when resumed here.
    """
    native = njit(function)

    @wraps(function)
    def managed(*args, **kwargs):
        stop = np.zeros(1, np.bool_)
        stream = native(*args, **kwargs, _closing=stop)
        try:
            yield from stream
        finally:
            stop[0] = True
            if next(stream, None) is not None:
                raise RuntimeError("native scan cleanup must return before further work")

    managed.native = native
    return managed


@njit
def description(action, source=-1, target=-1, start=-1, stop=-1, other=-1, end=-1):
    result = np.full(_DESCRIPTOR_SIZE, -1, np.int64)
    result[ACTION], result[SOURCE], result[TARGET] = action, source, target
    result[START], result[STOP], result[OTHER_START], result[OTHER_STOP] = start, stop, other, end
    result[REVERSE_SOURCE], result[REVERSE_TARGET], result[VARIANT] = 0, 0, 0
    if action in (ORDER, INTRA, NODE_MOVE, BLOCK_MOVE):
        result[POSITION] = other
    return result


@njit(inline="always")
def has_critical(x, chain, start, stop):
    base = x.offsets[chain]
    return x.critical[base + stop] > x.critical[base + start]


@njit(inline="always")
def interval(x, chain, start, stop):
    return x.interval_offsets[chain] + start * (x.offsets[chain + 1] - x.offsets[chain] + 1) + stop


@njit
def ownership(rows, offsets, owners, piece_rows, piece_offsets, row_chain, row_position, sources):
    ranks = np.full(rows.size, -1, np.int64)
    orders = np.full(rows.size, -1, np.int64)
    for rank in range(sources.size):
        source = sources[rank]
        first, last = piece_offsets[source], piece_offsets[source + 1]
        for i in range(last - 1, first - 1, -1):
            row = piece_rows[i]
            slot = offsets[row_chain[row]] + row_position[row]
            ranks[slot], orders[slot] = rank, last - 1 - i
    starts = np.zeros(offsets.size, np.int64)
    for chain in range(offsets.size - 1):
        length = offsets[chain + 1] - offsets[chain]
        starts[chain + 1] = starts[chain] + length * (length + 1)
    default = np.count_nonzero(ranks >= 0)
    interval_ranks = np.full(starts[-1], default, np.int64)
    slots = np.full(starts[-1], -1, np.int64)
    for chain in range(offsets.size - 1):
        base, length = offsets[chain], offsets[chain + 1] - offsets[chain]
        for start in range(length):
            best_rank, best_order, best_slot = default, default, -1
            for stop in range(start + 1, length + 1):
                slot = stop - 1
                rank, order = ranks[base + slot], orders[base + slot]
                if rank >= 0 and (rank, order) < (best_rank, best_order):
                    best_rank, best_order, best_slot = rank, order, slot
                offset = starts[chain] + start * (length + 1) + stop
                interval_ranks[offset], slots[offset] = best_rank, best_slot
    return ranks, starts, interval_ranks, slots


@njit
def source_priority_values(t, rows, offsets, group, target_period, completion):
    """Same integer priority keys; no per-chain owner dictionaries or row tuples."""
    status = np.zeros(1, np.int64)
    n = completion.size
    potential = np.full(n, -1, np.int64)
    present = np.zeros(n, np.bool_)
    due_key = np.empty(offsets.size - 1, np.int64)
    for chain in range(offsets.size - 1):
        first, last = offsets[chain], offsets[chain + 1]
        earliest, owner, multiple = np.iinfo(np.int64).max, -1, False
        due_key[chain] = np.iinfo(np.int64).max
        for slot in range(first, last):
            row = rows[slot]
            source = t.source[row]
            if source < 0:
                continue
            present[source] = True
            due_key[chain] = min(due_key[chain], max(0, t.due[source]))
            period = t.period[row]
            if period < earliest:
                earliest, owner, multiple = period, source, False
            elif period == earliest and source != owner:
                multiple = True
        if owner < 0 or multiple:
            continue
        later, duration = np.iinfo(np.int64).max, 0
        for slot in range(first, last):
            row = rows[slot]
            if t.source[row] != owner:
                duration = _add(duration, t.duration[row], status)
                if t.source[row] >= 0:
                    later = min(later, t.period[row])
        if later == np.iinfo(np.int64).max or later <= earliest:
            continue
        valid = True
        for slot in range(first, last):
            row = rows[slot]
            if t.source[row] >= 0 and t.source[row] != owner and group[row] >= 0:
                if target_period[group[row]] != later:
                    valid = False
        if valid:
            potential[owner] = max(potential[owner], duration)
    slack = np.empty(n, np.int64)
    backlog_weight, late_weight = np.zeros(n, np.int64), np.zeros(n, np.int64)
    for source in range(n):
        # Nonpositive dates participate only through the backlog keys; avoid an
        # unused subtraction overflowing where the old wide integer was ignored.
        slack[source] = _add(t.due[source], -completion[source], status) if t.due[source] > 0 else 0
        if t.backlog[source]:
            backlog_weight[source] = _mul(t.original_weight[source], completion[source], status)
        if present[source] and t.due[source] > 0 and slack[source] < 0:
            late_weight[source] = _mul(t.original_weight[source], slack[source], status)
    return status[0], potential, present, due_key, slack, backlog_weight, late_weight


@njit
def interleave(first, second, third, unique, count):
    result = np.empty(first.size + second.size + third.size, np.int64)
    seen = np.zeros(count, np.bool_)
    size = 0
    for i in range(max(first.size, second.size, third.size)):
        for values in (first, second, third):
            if i < values.size:
                value = values[i]
                if not unique or not seen[value]:
                    result[size], seen[value] = value, True
                    size += 1
    return result[:size]


def build_scan(state, columns):
    task, plan = state.task, state.plan
    completion = state.evaluation.delivery.original_completion_ms
    status, potential, present, due_key, slack, backlog_weight, late_weight = source_priority_values(
        columns, plan.node_rows, plan.chain_offsets, task.nodes.split_group,
        task.split_groups.target_period, completion)
    if status != OK:
        raise NumericValueError("refinement.priority", "integer priority overflow")
    numbers = np.arange(completion.size, dtype=np.int64)
    def ordered(mask, *keys):
        selected = numbers[mask]
        return selected[np.lexsort(tuple(key[selected] for key in reversed((*keys, numbers))))]
    backlog = ordered(present & task.originals.old_backlog, -completion, -backlog_weight)
    releasable = ordered(potential >= 0, -potential, -completion)
    late = ordered(present & (task.originals.due_ms > 0) & (slack < 0), late_weight, slack)
    critical_sources = interleave(backlog, releasable, late, True, completion.size)
    delivery_late = ordered((task.originals.due_ms > 0) & (slack < 0), slack, task.originals.due_ms)
    delivery_backlog = ordered(task.originals.old_backlog, -completion, -backlog_weight)
    on_time = ordered((task.originals.due_ms > 0) & (slack >= 0), slack)
    delivery = (interleave(delivery_late, delivery_backlog, on_time, False, completion.size)
                if delivery_backlog.size else np.concatenate((delivery_late, on_time)))
    critical_mask = np.zeros(completion.size, np.bool_)
    critical_mask[critical_sources] = True
    sources = np.concatenate((critical_sources, delivery[~critical_mask[delivery]]))
    row_owners = task.nodes.source[plan.node_rows]
    marked = np.zeros(row_owners.size, np.int64)
    real = row_owners >= 0
    marked[real] = critical_mask[row_owners[real]]
    prefix = np.concatenate((np.zeros(1, np.int64), np.cumsum(marked)))
    ranks, starts, interval_ranks, slots = ownership(plan.node_rows, plan.chain_offsets,
        task.nodes.source, plan.source_piece_rows, plan.source_piece_offsets,
        plan.row_to_chain, plan.row_to_position, sources)
    return ScanColumns(plan.node_rows, plan.chain_offsets, plan.chain_ids, plan.chain_periods,
        plan.source_piece_rows, plan.source_piece_offsets, plan.row_to_chain, plan.row_to_position,
        readonly(prefix, np.int64), readonly(ranks, np.int64), readonly(starts, np.int64),
        readonly(interval_ranks, np.int64), readonly(slots, np.int64),
        readonly(np.argsort(due_key, kind="stable"), np.int64), readonly(sources, np.int64))


@_managed_scan
def intra(x, source, lane, _closing):
    steps = 0
    for i in range(x.piece_offsets[source + 1] - 1, x.piece_offsets[source] - 1, -1):
        row = x.piece_rows[i]
        chain, start = x.row_chain[row], x.row_position[row]
        steps += 1
        if steps % 256 == 0:
            yield description(CONTINUE)
            if _closing[0]:
                return
        if has_critical(x, chain, start, start + 1) != lane:
            continue
        for target in range(start):
            yield description(INTRA, x.ids[chain], x.ids[chain], start, start + 1, target)
            if _closing[0]:
                return


@_managed_scan
def node_moves(x, t, rules, source, lane, _closing):
    steps, status = 0, np.array((OK, -1, -1, -1), np.int64)
    for i in range(x.piece_offsets[source + 1] - 1, x.piece_offsets[source] - 1, -1):
        row = x.piece_rows[i]
        chain, start = x.row_chain[row], x.row_position[row]
        if has_critical(x, chain, start, start + 1) != lane or x.offsets[chain + 1] - x.offsets[chain] < 2:
            continue
        for target in range(x.ids.size):
            if target == chain:
                continue
            first, last = x.offsets[target], x.offsets[target + 1]
            direct = np.empty(last - first + 1, np.bool_)
            for slot in range(direct.size):
                left = slot == 0 or all_edges_allowed_values(edge_node(t, x.rows[first + slot - 1]), edge_node(t, row), rules, status)
                right = slot == direct.size - 1 or all_edges_allowed_values(edge_node(t, row), edge_node(t, x.rows[first + slot]), rules, status)
                direct[slot] = left and right
                steps += 1
                if status[0] != OK:
                    yield description(ERROR)
                    return
                if steps % 256 == 0:
                    yield description(CONTINUE)
                    if _closing[0]:
                        return
            for accepted in (True, False):
                for slot in range(direct.size):
                    if direct[slot] == accepted:
                        yield description(NODE_MOVE, x.ids[chain], x.ids[target], start, start + 1, slot)
                        if _closing[0]:
                            return


@_managed_scan
def node_exchanges(x, source, lane, _closing):
    steps = 0
    for i in range(x.piece_offsets[source + 1] - 1, x.piece_offsets[source] - 1, -1):
        row = x.piece_rows[i]
        chain, start = x.row_chain[row], x.row_position[row]
        source_critical = has_critical(x, chain, start, start + 1)
        for owner in x.sources:
            for j in range(x.piece_offsets[owner + 1] - 1, x.piece_offsets[owner] - 1, -1):
                other = x.piece_rows[j]
                target, slot = x.row_chain[other], x.row_position[other]
                steps += 1
                if steps % 256 == 0:
                    yield description(CONTINUE)
                    if _closing[0]:
                        return
                if target == chain or x.ranks[x.offsets[chain] + start] >= x.ranks[x.offsets[target] + slot]:
                    continue
                if (source_critical or has_critical(x, target, slot, slot + 1)) == lane:
                    yield description(NODE_SWAP, x.ids[chain], x.ids[target], start, start + 1, slot, slot + 1)
                    if _closing[0]:
                        return


@_managed_scan
def blocks(x, source, lane, _closing):
    steps = 0
    for i in range(x.piece_offsets[source + 1] - 1, x.piece_offsets[source] - 1, -1):
        row = x.piece_rows[i]
        chain, anchor = x.row_chain[row], x.row_position[row]
        size = x.offsets[chain + 1] - x.offsets[chain]
        for length in range(2, min(4, size) if lane else size):
            for start in range(max(0, anchor - length + 1), min(anchor + 1, size - length + 1)):
                steps += 1
                if steps % 256 == 0:
                    yield description(CONTINUE)
                    if _closing[0]:
                        return
                stop = start + length
                source_interval = interval(x, chain, start, stop)
                if x.interval_slots[source_interval] != anchor:
                    continue
                source_critical = has_critical(x, chain, start, stop)
                if not lane and source_critical:
                    continue
                for target in x.ranked_chains:
                    if target == chain:
                        continue
                    target_size = x.offsets[target + 1] - x.offsets[target]
                    maximum = min(4, target_size) if lane else target_size
                    move_slot, exchange_size, exchange_slot = 0, 1, 0
                    if source_critical != lane:
                        move_slot = target_size + 1
                    while move_slot <= target_size or exchange_size < maximum:
                        if move_slot <= target_size:
                            yield description(BLOCK_MOVE, x.ids[chain], x.ids[target], start, stop, move_slot)
                            if _closing[0]:
                                return
                            move_slot += 1
                        found = False
                        while exchange_size < maximum and not found:
                            slot, end = exchange_slot, exchange_slot + exchange_size
                            exchange_slot += 1
                            current_size = exchange_size
                            if exchange_slot > target_size - exchange_size:
                                exchange_size += 1
                                exchange_slot = 0
                            steps += 1
                            if steps % 256 == 0:
                                yield description(CONTINUE)
                                if _closing[0]:
                                    return
                            if (source_critical or has_critical(x, target, slot, end)) != lane:
                                continue
                            if current_size > 1 and (x.interval_ranks[source_interval], chain, start, stop) > (
                                x.interval_ranks[interval(x, target, slot, end)], target, slot, end):
                                continue
                            found = True
                            yield description(BLOCK_SWAP, x.ids[chain], x.ids[target], start, stop, slot, end)
                            if _closing[0]:
                                return


@_managed_scan
def chain_candidates(x, family, chain, _closing):
    if family == CUT:
        # Leave an unrepresentable identity for candidate validation at the
        # original charge boundary; empty scans must not raise speculatively.
        new_id = -1 if x.ids.max() == np.iinfo(np.int64).max else x.ids.max() + 1
        for cut in range(1, x.offsets[chain + 1] - x.offsets[chain]):
            for prefix in range(x.ids.size + 1):
                for suffix in range(x.ids.size + 1):
                    if prefix != suffix:
                        yield description(CUT, x.ids[chain], new_id, cut, -1, prefix, suffix)
                        if _closing[0]:
                            return
    else:
        for position in range(x.ids.size):
            if position != chain and x.periods[position] == x.periods[chain]:
                yield description(ORDER, x.ids[chain], x.ids[position], -1, -1, position)
                if _closing[0]:
                    return


@njit
def source_chains(x, source, lane):
    result = np.empty(x.ids.size, np.int64)
    seen = np.zeros(x.ids.size, np.bool_)
    size = 0
    for i in range(x.piece_offsets[source + 1] - 1, x.piece_offsets[source] - 1, -1):
        chain = x.row_chain[x.piece_rows[i]]
        if not seen[chain] and has_critical(x, chain, 0, x.offsets[chain + 1] - x.offsets[chain]) == lane:
            result[size], seen[chain] = chain, True
            size += 1
    return result[:size]


@_managed_scan
def reclaim(x, role, purpose, group, _closing):
    steps = 0
    for chain in range(x.ids.size):
        base, last = x.offsets[chain], x.offsets[chain + 1]
        position = base
        while position < last:
            row = x.rows[position]
            steps += 1
            if steps % 256 == 0:
                yield description(CONTINUE)
                if _closing[0]:
                    return
            if role[row] != _VIRTUAL or purpose[row] != _BRIDGE or group[row] >= 0:
                position += 1
                continue
            start = position
            while position < last:
                row = x.rows[position]
                if role[row] != _VIRTUAL or purpose[row] != _BRIDGE or group[row] >= 0:
                    break
                position += 1
                steps += 1
                if steps % 256 == 0:
                    yield description(CONTINUE)
                    if _closing[0]:
                        return
            yield description(RECLAIM, x.ids[chain], x.ids[chain], start - base, position - base)
            if _closing[0]:
                return
            if position - start > 1:
                for item in range(start, position):
                    yield description(RECLAIM, x.ids[chain], x.ids[chain], item - base, item + 1 - base)
                    if _closing[0]:
                        return


def bounded(stream, budget):
    for value in stream:
        if not budget.allows_search():
            return
        if value[ACTION] == ERROR:
            raise NumericValueError("refinement.scan", "integer scan overflow")
        if value[ACTION] != CONTINUE:
            yield value


def alternate(first, second):
    pending = [iter(first), iter(second)]
    while pending:
        for stream in pending[:]:
            try:
                yield next(stream)
            except StopIteration:
                pending.remove(stream)


def family_stream(x, columns, rules, family, previous, lane, budget):
    """Only scheduling remains in Python: four proposals per source, in order."""
    sources = x.sources
    matches = np.flatnonzero(sources == previous) if previous is not None else np.empty(0, np.int64)
    if matches.size:
        offset = int(matches[0]) + 1
        sources = np.concatenate((sources[offset:], sources[:offset]))
    shared = [None] * x.ids.size
    def source_stream(source):
        if family == "intra":
            yield from bounded(intra(x, source, lane), budget)
        elif family == "node":
            yield from alternate(bounded(node_moves(x, columns, rules, source, lane), budget),
                                 bounded(node_exchanges(x, source, lane), budget))
        elif family == "block":
            yield from bounded(blocks(x, source, lane), budget)
        else:
            for chain in source_chains(x, source, lane):
                if shared[chain] is None:
                    shared[chain] = bounded(chain_candidates(x, CUT if family == "cut" else ORDER, chain), budget)
                yield from shared[chain]
    pending = deque((int(source), iter(source_stream(int(source)))) for source in sources)
    while pending and budget.allows_search():
        source, stream = pending.popleft()
        for _ in range(4):
            if not budget.allows_search():
                return
            try:
                value = next(stream)
            except StopIteration:
                break
            value[OWNER] = source
            yield value
        else:
            pending.append((source, stream))
