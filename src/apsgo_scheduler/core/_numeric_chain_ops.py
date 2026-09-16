"""Shared native chain operations; no rules, resources, quota or acceptance."""

from dataclasses import dataclass

import numpy as np
from numba import njit

from ._numeric_state import OK, INVALID, CAPACITY, _require_numeric_base, readonly
from ._numeric_units import NumericValueError

PART_CHAIN, PART_START, PART_STOP, PART_REVERSE = range(4)


@njit
def chain_rows(view, chain):
    """Borrow a base or changed slice, never convert its node rows to objects."""
    if chain < 0 or chain >= view.count:
        raise ValueError("chain index outside active view")
    start, stop = view.starts[chain], view.stops[chain]
    return view.changed_rows[start:stop] if view.private[chain] else view.base_rows[start:stop]


@njit
def bind_base_chain(view, chain, start, stop, identity, period):
    """Attach an existing slice; callers own the active metadata length."""
    if (chain < 0 or chain >= view.ids.size or start < 0 or stop <= start
            or stop > view.base_rows.size or identity < 0 or period < 0):
        return INVALID
    view.starts[chain], view.stops[chain] = start, stop
    view.private[chain] = False
    view.ids[chain], view.periods[chain] = identity, period
    return OK


@njit
def flatten_view(view):
    """Materialize once at a formal plan boundary, never for rejected attempts."""
    offsets = np.zeros(view.count + 1, np.int64)
    for chain in range(view.count):
        offsets[chain + 1] = offsets[chain] + view.stops[chain] - view.starts[chain]
    rows = np.empty(offsets[-1], np.int64)
    for chain in range(view.count):
        rows[offsets[chain]:offsets[chain + 1]] = chain_rows(view, chain)
    return rows, offsets


@njit
def write_parts(view, parts, extra, used):
    """Append ordered slices atomically; chain -1 denotes the explicit extra array.

    Parts contain chain index, start, stop, reversal flag. All input validation
    and capacity checks finish before the first private write. The caller alone
    attaches the result to chain metadata and advances its valid length.
    """
    if used < 0 or used > view.changed_rows.size or parts.shape[1] != 4:
        return INVALID, used
    length = 0
    for i in range(parts.shape[0]):
        chain, start, stop, reverse = parts[i]
        if chain < -1 or chain >= view.count or (reverse != 0 and reverse != 1):
            return INVALID, used
        size = extra.size if chain == -1 else view.stops[chain] - view.starts[chain]
        if start < 0 or stop < start or stop > size:
            return INVALID, used
        if chain >= 0 and view.private[chain] and view.stops[chain] > used:
            return INVALID, used
        amount = stop - start
        if amount > view.changed_rows.size - used - length:
            return CAPACITY, used
        length += amount
    cursor = used
    for i in range(parts.shape[0]):
        chain, start, stop, reverse = parts[i]
        rows = extra if chain == -1 else chain_rows(view, chain)
        for offset in range(stop - start):
            position = stop - 1 - offset if reverse else start + offset
            view.changed_rows[cursor] = rows[position]
            cursor += 1
    return OK, cursor


@njit
def _attach(view, chain, start, stop):
    view.starts[chain], view.stops[chain] = start, stop
    view.private[chain] = True


@njit
def replace_span(view, chain, start, stop, extra, used, reverse=False):
    if chain < 0 or chain >= view.count:
        return INVALID, used
    size = view.stops[chain] - view.starts[chain]
    if start < 0 or stop < start or stop > size:
        return INVALID, used
    parts = np.array(((chain, 0, start, 0), (-1, 0, extra.size, int(reverse)),
                      (chain, stop, size, 0)), dtype=np.int64)
    status, end = write_parts(view, parts, extra, used)
    if status == OK:
        _attach(view, chain, used, end)
    return status, end


@njit
def reverse_chain(view, chain, used):
    if chain < 0 or chain >= view.count:
        return INVALID, used
    size = view.stops[chain] - view.starts[chain]
    parts = np.array(((chain, 0, size, 1),), dtype=np.int64)
    status, end = write_parts(view, parts, view.base_rows[:0], used)
    if status == OK:
        _attach(view, chain, used, end)
    return status, end


@njit
def move_span(view, source, target, start, stop, position, used):
    """Position is in the original target, including for an intra-chain move."""
    if source < 0 or target < 0 or source >= view.count or target >= view.count:
        return INVALID, used
    n = view.stops[source] - view.starts[source]
    m = view.stops[target] - view.starts[target]
    if start < 0 or stop <= start or stop > n or position < 0 or position > m:
        return INVALID, used
    if source == target:
        if start < position < stop:
            return INVALID, used
        if position <= start:
            parts = np.array(((source, 0, position, 0), (source, start, stop, 0),
                              (source, position, start, 0), (source, stop, n, 0)), dtype=np.int64)
        else:
            parts = np.array(((source, 0, start, 0), (source, stop, position, 0),
                              (source, start, stop, 0), (source, position, n, 0)), dtype=np.int64)
    else:
        parts = np.array(((source, 0, start, 0), (source, stop, n, 0),
                          (target, 0, position, 0), (source, start, stop, 0),
                          (target, position, m, 0)), dtype=np.int64)
    status, end = write_parts(view, parts, view.base_rows[:0], used)
    if status == OK:
        if source == target:
            _attach(view, source, used, end)
        else:
            middle = used + n - (stop - start)
            _attach(view, source, used, middle)
            _attach(view, target, middle, end)
    return status, end


@njit
def exchange_spans(view, source, target, start, stop, other_start, other_stop, used):
    if (source < 0 or target < 0 or source >= view.count or target >= view.count
            or source == target):
        return INVALID, used
    n, m = view.stops[source] - view.starts[source], view.stops[target] - view.starts[target]
    if (start < 0 or stop <= start or stop > n or other_start < 0
            or other_stop <= other_start or other_stop > m):
        return INVALID, used
    parts = np.array(((source, 0, start, 0), (target, other_start, other_stop, 0),
                      (source, stop, n, 0), (target, 0, other_start, 0),
                      (source, start, stop, 0), (target, other_stop, m, 0)), dtype=np.int64)
    status, end = write_parts(view, parts, view.base_rows[:0], used)
    if status == OK:
        middle = used + n - (stop - start) + (other_stop - other_start)
        _attach(view, source, used, middle)
        _attach(view, target, middle, end)
    return status, end


@njit
def _copy_chain_reference(view, target, source):
    view.starts[target], view.stops[target] = view.starts[source], view.stops[source]
    view.private[target] = view.private[source]
    view.ids[target], view.periods[target] = view.ids[source], view.periods[source]


@njit
def remove_empty_chains(view):
    count = 0
    for i in range(view.count):
        if view.stops[i] != view.starts[i]:
            _copy_chain_reference(view, count, i)
            count += 1
    return count


@njit
def cut_chain(view, chain, position, new_id, new_period):
    """Split references only; the caller provides authorized new identity/period."""
    if chain < 0 or chain >= view.count or new_id < 0 or new_period < 0:
        return INVALID, view.count
    size = view.stops[chain] - view.starts[chain]
    if position <= 0 or position >= size:
        return INVALID, view.count
    for i in range(view.count):
        if view.ids[i] == new_id:
            return INVALID, view.count
    if view.count == view.ids.size:
        return CAPACITY, view.count
    for i in range(view.count, chain, -1):
        _copy_chain_reference(view, i, i - 1)
    middle = view.starts[chain] + position
    view.stops[chain] = middle
    view.starts[chain + 1] = middle
    view.ids[chain + 1], view.periods[chain + 1] = new_id, new_period
    return OK, view.count + 1


@njit
def merge_chains(view, source, target, position, used, reverse_source=False, reverse_target=False):
    if (source < 0 or target < 0 or source >= view.count or target >= view.count
            or source == target):
        return INVALID, used, view.count
    n, m = view.stops[source] - view.starts[source], view.stops[target] - view.starts[target]
    if position < 0 or position > m:
        return INVALID, used, view.count
    if reverse_target:
        parts = np.array(((target, m - position, m, 1), (source, 0, n, int(reverse_source)),
                          (target, 0, m - position, 1)), dtype=np.int64)
    else:
        parts = np.array(((target, 0, position, 0), (source, 0, n, int(reverse_source)),
                          (target, position, m, 0)), dtype=np.int64)
    status, end = write_parts(view, parts, view.base_rows[:0], used)
    if status == OK:
        _attach(view, target, used, end)
        for i in range(source, view.count - 1):
            _copy_chain_reference(view, i, i + 1)
        return OK, end, view.count - 1
    return status, used, view.count


@njit
def reorder_chains(view, order):
    if order.size != view.count:
        return INVALID
    seen = np.zeros(view.count, dtype=np.bool_)
    for i in order:
        if i < 0 or i >= view.count or seen[i]:
            return INVALID
        seen[i] = True
    # Only small metadata arrays are copied; all node rows remain borrowed.
    starts, stops = view.starts[order], view.stops[order]
    private, ids, periods = view.private[order], view.ids[order], view.periods[order]
    for i in range(view.count):
        view.starts[i], view.stops[i], view.private[i] = starts[i], stops[i], private[i]
        view.ids[i], view.periods[i] = ids[i], periods[i]
    return OK


@njit
def find_chain(sorted_ids, positions, identity):
    left, right = 0, sorted_ids.size
    while left < right:
        middle = left + (right - left) // 2
        if sorted_ids[middle] < identity:
            left = middle + 1
        else:
            right = middle
    return positions[left] if left < sorted_ids.size and sorted_ids[left] == identity else -1


@dataclass(frozen=True, slots=True, eq=False)
class NumericChainIndex:
    """Once-per-generation metadata; existing row/source positions stay shared."""
    task: object
    plan: object
    lengths: np.ndarray
    heads: np.ndarray
    tails: np.ndarray
    sorted_ids: np.ndarray
    sorted_positions: np.ndarray

    @classmethod
    def build(cls, task, plan):
        _require_numeric_base(task, plan)
        order = np.argsort(plan.chain_ids, kind="stable")
        return cls(task, plan, readonly(np.diff(plan.chain_offsets), np.int64),
                   readonly(plan.node_rows[plan.chain_offsets[:-1]], np.int64),
                   readonly(plan.node_rows[plan.chain_offsets[1:] - 1], np.int64),
                   readonly(plan.chain_ids[order], np.int64), readonly(order, np.int64))

    def require_current(self, task, plan):
        if task is not self.task or plan is not self.plan:
            raise NumericValueError("chain_index", "stale task or plan generation")
