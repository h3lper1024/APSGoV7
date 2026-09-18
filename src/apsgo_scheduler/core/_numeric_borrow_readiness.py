"""Bounded future-borrow admission on already evaluated numeric candidates.

No scheduling, clock accumulation, scoring, quota consumption or publication is
performed here. The caller owns enabled-rule checks, authorized resources and
immutable task/plan/evaluation/view identities, including between resumptions.
"""

from collections import namedtuple

from numba import njit

from ._numeric_kernel import (
    _GENERATED_VIRTUAL, _add, _error, _location, _sub, column_size, column_value,
)
from ._numeric_state import INVALID, MORE_WORK, OK

# Thin array views, not alternate plans or domain resources. Old positions are
# chain-local (NumericPlan.build); old flat position = offsets[chain] + position.
BorrowReadinessIndex = namedtuple("BorrowReadinessIndex", (
    "rows offsets periods row_chain row_position ends period_count"
))
BorrowReadinessPieces = namedtuple("BorrowReadinessPieces", (
    "old_group_count old_node_groups node_groups piece_indices piece_counts "
    "group_parents group_periods"
))
BorrowReadinessWitness = namedtuple("BorrowReadinessWitness", (
    "reason row source chain position flat_position source_period old_period "
    "new_period has_old lower_ms old_start_ms new_start_ms old_early_ms new_early_ms"
))

BORROW_ALLOWED, BORROW_BEFORE_EARLIEST, BORROW_EXISTING_WORSENED = range(3)
BORROW_CURSOR_SIZE = 8
_PHASE, _CHAIN, _POSITION, _FLAT, _EXPECTED, _VISITED, _PROTECTED, _REAL = range(8)
_HEADERS, _NODES, _COMPLETE, _REJECTED = range(4)


@njit
def _empty_witness():
    return BorrowReadinessWitness(BORROW_ALLOWED, -1, -1, -1, -1, -1,
                                  -1, -1, -1, False, 0, 0, 0, 0, 0)


@njit
def _old_location(index, row, status):
    """Validate before indexing; missing mappings never address the last item."""
    if row < 0 or row >= index.row_chain.size:
        _error(status, INVALID)
        return -1, -1
    chain, position = index.row_chain[row], index.row_position[row]
    if chain < 0 or chain >= index.periods.size or position < 0:
        _error(status, INVALID)
        return -1, -1
    start, stop = index.offsets[chain], index.offsets[chain + 1]
    if (start < 0 or stop <= start or stop > index.rows.size
            or position >= stop - start
            or not 0 <= index.periods[chain] < index.period_count):
        _error(status, INVALID)
        return -1, -1
    flat = start + position  # bounded above by stop, which is a valid array size
    if index.rows[flat] != row:
        _error(status, INVALID)
        return -1, -1
    return chain, flat


@njit
def _new_piece_parent(pieces, row, old_count, new_period, status):
    """Check actual split metadata, never treat arbitrary missing rows as new.

    Node/group columns may be flat or (base, active-private-tail). The resource
    owner must have authorized these groups; this is provenance/period binding,
    not a second ControlledOrderSplitRule or a replacement conservation audit.
    """
    group = column_value(pieces.node_groups, row)
    if (row < old_count or group < pieces.old_group_count
            or group >= column_size(pieces.group_parents)):
        _error(status, INVALID)
        return -1
    parent = column_value(pieces.group_parents, group)
    count = column_value(pieces.piece_counts, row)
    number = column_value(pieces.piece_indices, row)
    if (parent < 0 or parent >= old_count or pieces.old_node_groups[parent] >= 0
            or count < 2 or not 1 <= number <= count
            or column_value(pieces.group_periods, group) != new_period):
        _error(status, INVALID)
        return -1
    return parent


@njit
def _start(ends, flat, duration, status):
    """Read a complete evaluation's time; do not invent or accumulate a clock."""
    if flat < 0 or flat >= ends.size or duration < 0:
        _error(status, INVALID)
        return 0
    start = _sub(ends[flat], duration, status)
    if status[0] == OK and (start < 0 or start != (0 if flat == 0 else ends[flat - 1])):
        _error(status, INVALID)
    return start


@njit
def borrow_readiness_step(old_columns, columns, index, view, ends, pieces,
                          cursor, status, work_limit=256):
    """Return (code, passed, complete, witness), stopping at the first rejection.

    Pass existing TaskColumns/private_task_columns, a BorrowReadinessIndex made
    from the CURRENT NumericPlan and its evaluation, and the FINAL candidate
    NumericChainView with summary.ends. All times are relative integer ms.
    BorrowReadinessPieces borrows actual old/candidate split metadata; truncate
    private arrays to their active counts. Flat post-materialization columns use
    exactly the same function. Do not infer legal new pieces from source alone.

    Start with a zeroed int64 cursor[BORROW_CURSOR_SIZE] and status[5]. Cursor
    slots are phase, chain, local position, flat position, expected node count,
    visited node count, checked borrowed-node count and current-chain real count.
    Metadata validation and node visits share the per-call work_limit. MORE_WORK
    leaves status[0] == OK; only private cursor/status are mutated. The caller
    checks the SAME budget between calls or supplies its terminal status. Errors
    and interruptions return passed=False, complete=False and an empty witness.

    A business rejection is (OK, False, True, witness). It is terminal: discard
    its cursor; resuming it is INVALID, never a later pass. A full pass has an
    empty witness. No old node is represented by has_old=False, not time zero.
    Existing tasks, lineage authorization, full conservation, identity/epoch and
    rule enablement remain with the caller. This function is not wired into the
    search entry until FB2; it cannot on its own authorize a search-state commit.
    """
    empty = _empty_witness()
    if status.size != 5:
        return INVALID, False, False, empty
    if status[0] != OK:
        return status[0], False, False, empty
    _location(status, -1, -1, -1)
    old_count, count = column_size(old_columns.source), column_size(columns.source)
    original_count = columns.earliest_start.size
    if (cursor.size != BORROW_CURSOR_SIZE or work_limit <= 0 or old_count > count
            or index.period_count <= 0 or index.periods.size == 0
            or index.offsets.size != index.periods.size + 1
            or index.offsets[0] != 0 or index.offsets[-1] != index.rows.size
            or index.ends.size != index.rows.size
            or index.row_chain.size != old_count or index.row_position.size != old_count
            or column_size(old_columns.duration) != old_count
            or column_size(old_columns.period) != old_count
            or column_size(old_columns.role) != old_count
            or column_size(columns.duration) != count or column_size(columns.period) != count
            or column_size(columns.role) != count
            or columns.has_earliest_start.size != original_count
            or old_columns.earliest_start.size != original_count
            or old_columns.has_earliest_start.size != original_count
            or pieces.old_node_groups.size != old_count
            or column_size(pieces.node_groups) != count
            or column_size(pieces.piece_indices) != count
            or column_size(pieces.piece_counts) != count
            or not 0 <= pieces.old_group_count <= column_size(pieces.group_parents)
            or column_size(pieces.group_periods) != column_size(pieces.group_parents)
            or view.count <= 0 or view.count > min(view.starts.size, view.stops.size,
                view.private.size, view.ids.size, view.periods.size)):
        _error(status, INVALID)
        return status[0], False, False, empty
    if (cursor[_PHASE] < _HEADERS or cursor[_PHASE] > _COMPLETE
            or cursor[_CHAIN] < 0 or cursor[_CHAIN] > view.count
            or cursor[_POSITION] < 0 or cursor[_FLAT] < 0 or cursor[_FLAT] > ends.size
            or cursor[_EXPECTED] < 0 or cursor[_VISITED] != cursor[_FLAT]
            or not 0 <= cursor[_PROTECTED] <= cursor[_VISITED]
            or not 0 <= cursor[_REAL] <= cursor[_POSITION]):
        _error(status, INVALID)
        return status[0], False, False, empty
    work = 0
    while work < work_limit:
        chain = cursor[_CHAIN]
        if cursor[_PHASE] == _HEADERS:
            if chain == view.count:
                if cursor[_EXPECTED] != ends.size:
                    _error(status, INVALID)
                    return status[0], False, False, empty
                cursor[_PHASE], cursor[_CHAIN] = _NODES, 0
                continue
            start, stop = view.starts[chain], view.stops[chain]
            capacity = view.changed_rows.size if view.private[chain] else view.base_rows.size
            if (start < 0 or stop <= start or stop > capacity or view.ids[chain] < 0
                    or not 0 <= view.periods[chain] < index.period_count
                    or chain and view.periods[chain] < view.periods[chain - 1]):
                _location(status, -1, chain, -1)
                _error(status, INVALID)
                return status[0], False, False, empty
            size = _add(cursor[_EXPECTED], stop - start, status)
            if status[0] != OK:
                return status[0], False, False, empty
            cursor[_EXPECTED], cursor[_CHAIN] = size, chain + 1
            work += 1
            continue
        if chain == view.count:
            if cursor[_FLAT] != ends.size or cursor[_POSITION] != 0:
                _error(status, INVALID)
                return status[0], False, False, empty
            cursor[_PHASE] = _COMPLETE
            return OK, True, True, empty
        position, flat = cursor[_POSITION], cursor[_FLAT]
        _location(status, -1, chain, position)
        if (position >= view.stops[chain] - view.starts[chain] or flat >= ends.size
                or cursor[_PHASE] != _NODES):
            _error(status, INVALID)
            return status[0], False, False, empty
        rows = view.changed_rows if view.private[chain] else view.base_rows
        row = rows[view.starts[chain] + position]
        if row < 0 or row >= count:
            _error(status, INVALID)
            return status[0], False, False, empty
        source = column_value(columns.source, row)
        role = column_value(columns.role, row)
        duration = column_value(columns.duration, row)
        start = _start(ends, flat, duration, status)
        if (source < -1 or source >= original_count
                or (source == -1) != (role == _GENERATED_VIRTUAL)):
            _error(status, INVALID)
        if status[0] != OK:
            return status[0], False, False, empty
        if source >= 0:
            period, new_period = column_value(columns.period, row), view.periods[chain]
            if (not 0 <= period < index.period_count
                    or not columns.has_earliest_start[source]
                    or not old_columns.has_earliest_start[source]
                    or columns.earliest_start[source] != old_columns.earliest_start[source]):
                _error(status, INVALID)
                return status[0], False, False, empty
            has_old, old_period, old_flat = row < old_count, -1, -1
            if has_old:
                old_chain, old_flat = _old_location(index, row, status)
                if (status[0] == OK and (
                        source != column_value(old_columns.source, row)
                        or period != column_value(old_columns.period, row)
                        or duration != column_value(old_columns.duration, row)
                        or role != column_value(old_columns.role, row))):
                    _error(status, INVALID)
                if status[0] == OK:
                    old_period = index.periods[old_chain]
            else:
                parent = _new_piece_parent(pieces, row, old_count, new_period, status)
                if status[0] == OK:
                    _old_location(index, parent, status)
                    if (source != column_value(old_columns.source, parent)
                            or period != column_value(old_columns.period, parent)
                            or role != column_value(old_columns.role, parent)):
                        _error(status, INVALID)
            if status[0] != OK:
                return status[0], False, False, empty
            if new_period < period:
                lower = columns.earliest_start[source]
                old_start, old_early, reason = 0, 0, BORROW_ALLOWED
                early = _sub(lower, start, status) if start < lower else 0
                if has_old:
                    old_start = _start(index.ends, old_flat,
                                       column_value(old_columns.duration, row), status)
                    if status[0] == OK and old_start < lower:
                        old_early = _sub(lower, old_start, status)
                if status[0] != OK:
                    return status[0], False, False, empty
                if not has_old or new_period < old_period:
                    if early:
                        reason = BORROW_BEFORE_EARLIEST
                elif early > old_early:
                    reason = BORROW_EXISTING_WORSENED
                cursor[_PROTECTED] += 1
                if reason != BORROW_ALLOWED:
                    # The witness node was visited too; counters include it once.
                    cursor[_VISITED] += 1
                    cursor[_FLAT] += 1
                    cursor[_POSITION] += 1
                    cursor[_REAL] += 1
                    cursor[_PHASE] = _REJECTED
                    return OK, False, True, BorrowReadinessWitness(
                        reason, row, source, chain, position, flat, period,
                        old_period, new_period, has_old, lower, old_start, start,
                        old_early, early)
            cursor[_REAL] += 1
        cursor[_POSITION] += 1
        cursor[_FLAT] += 1
        cursor[_VISITED] += 1
        work += 1
        if cursor[_POSITION] == view.stops[chain] - view.starts[chain]:
            if cursor[_REAL] == 0:
                _error(status, INVALID)
                return status[0], False, False, empty
            cursor[_CHAIN], cursor[_POSITION], cursor[_REAL] = chain + 1, 0, 0
    # A final header or node may have consumed the last work unit. Completion is
    # emitted on the next call so the owner gets its normal cancellation boundary.
    return MORE_WORK, False, False, empty
