"""Bounded chain readiness summaries on the existing integer time columns.

A summary is not a rule evaluation or a production schedule. The caller owns
rule enablement, immutable task/view identity, and budget/cancellation checks
between steps. This module does not change the production clock or add waiting.
"""

from numba import njit

from ._numeric_kernel import _add, _error, _location, _sub, column_size, column_value
from ._numeric_state import INVALID, MORE_WORK, OK

READINESS_CURSOR_SIZE = 4
_POSITION, _DURATION, _RELEASE, _REAL_COUNT = range(READINESS_CURSOR_SIZE)


@njit
def chain_readiness_step(columns, rows, cursor, status, work_limit=256):
    """Return (code, release_offset_ms, duration_ms, complete) for one chain.

    ``rows`` is a borrowed one-dimensional int64 node-row slice. ``columns``
    uses the existing task_columns/private_task_columns interface. Source -1
    denotes generated virtual material; real and transition rows index original
    lower bounds by source, NOT by node row. Input role/lineage validation remains
    with the existing task boundary.

    Start with a zeroed int64 cursor of length READINESS_CURSOR_SIZE and the
    existing five-slot int64 kernel status. The cursor stores next position,
    prefix duration, maximum (lower bound - prefix), and real-node count. It is
    private to this exact chain and these immutable columns until completion.

    Each call visits at most work_limit nodes. MORE_WORK leaves status[0] at OK
    so the caller can check the shared budget before resuming; no search candidate
    quota is consumed. On cancellation the caller may stop immediately or set
    the existing terminal status before calling again. Partial/error returns
    always have complete=False and zero output values; never use the cursor's
    partial summary for selection. After OK, a chain can start at T iff
    T >= release_offset_ms. Negative release values, including INT64_MIN, are
    valid; real-node count, not a sentinel time, records presence.
    """
    if status.size != 5:
        return INVALID, 0, 0, False
    if status[0] != OK:
        return status[0], 0, 0, False
    _location(status, -1, -1, -1)
    if cursor.size != READINESS_CURSOR_SIZE or rows.size == 0 or work_limit <= 0:
        _error(status, INVALID)
        return status[0], 0, 0, False
    if (column_size(columns.duration) != column_size(columns.source)
            or columns.earliest_start.size != columns.has_earliest_start.size):
        _error(status, INVALID)
        return status[0], 0, 0, False

    position, duration, release, real_count = cursor
    if (position < 0 or position > rows.size or duration < 0
            or real_count < 0 or real_count > position
            or position == 0 and (duration != 0 or release != 0 or real_count != 0)):
        _error(status, INVALID)
        return status[0], 0, 0, False

    work = 0
    while position < rows.size and work < work_limit:
        _location(status, -1, -1, position)
        row = rows[position]
        if row < 0 or row >= column_size(columns.source):
            _error(status, INVALID)
            return status[0], 0, 0, False
        source = column_value(columns.source, row)
        node_duration = column_value(columns.duration, row)
        # Existing proportional split allocation can legitimately yield 0 ms.
        if node_duration < 0 or source < -1 or source >= columns.earliest_start.size:
            _error(status, INVALID)
            return status[0], 0, 0, False

        next_release, next_real_count = release, real_count
        if source >= 0:
            if not columns.has_earliest_start[source]:
                _error(status, INVALID)
                return status[0], 0, 0, False
            required = _sub(columns.earliest_start[source], duration, status)
            if status[0] != OK:
                return status[0], 0, 0, False
            if real_count == 0 or required > release:
                next_release = required
            next_real_count += 1
        next_duration = _add(duration, node_duration, status)
        if status[0] != OK:
            return status[0], 0, 0, False

        position += 1
        work += 1
        duration, release, real_count = next_duration, next_release, next_real_count
        cursor[_POSITION], cursor[_DURATION] = position, duration
        cursor[_RELEASE], cursor[_REAL_COUNT] = release, real_count

    if position < rows.size:
        return MORE_WORK, 0, 0, False
    if real_count == 0:
        _error(status, INVALID)
        return status[0], 0, 0, False
    return OK, release, duration, True
