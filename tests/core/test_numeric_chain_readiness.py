"""Function tests for the bounded integer chain-readiness scan; no solver runs."""

from collections import namedtuple
from random import Random

import numpy as np
import pytest

from apsgo_scheduler.core._numeric_readiness import (
    READINESS_CURSOR_SIZE,
    chain_readiness_step,
)
from apsgo_scheduler.core._numeric_state import (
    CANCELLED,
    INVALID,
    MORE_WORK,
    NUMERIC_ERROR,
    OK,
)

HOUR = 3_600_000
MIN = -(1 << 63)
MAX = (1 << 63) - 1
# Only the columns used by this function; these names match task_columns().
Columns = namedtuple("ReadinessTestColumns", "duration source earliest_start has_earliest_start")


def array(values, dtype=np.int64):
    result = np.array(values, dtype=dtype)
    result.setflags(write=False)
    return result


def columns(durations, sources, lower, present=None):
    return Columns(array(durations), array(sources), array(lower),
                   array([True] * len(lower) if present is None else present, np.bool_))


def scan(data, rows, work_limit=256):
    rows = array(rows)
    cursor, status = np.zeros(READINESS_CURSOR_SIZE, np.int64), np.zeros(5, np.int64)
    # Progress must depend on consumed nodes, not on positive elapsed time.
    for _ in range(rows.size + 1):
        before = int(cursor[0])
        result = chain_readiness_step(data, rows, cursor, status, work_limit)
        assert 0 <= int(cursor[0]) - before <= max(0, work_limit)
        if result[0] != MORE_WORK:
            return result, cursor, status
        assert result == (MORE_WORK, 0, 0, False)
        assert status[0] == OK
        assert cursor[0] > before
    pytest.fail("readiness scan did not terminate")


def test_design_two_chain_example_without_reordering_or_waiting():
    data = columns([HOUR] * 4, [0, 1, 2, 3], [0, 3 * HOUR, 0, HOUR])
    first, _, _ = scan(data, [0, 1])
    second, _, _ = scan(data, [2, 3])
    assert first == (OK, 2 * HOUR, 2 * HOUR, True)
    assert second == (OK, 0, 2 * HOUR, True)
    assert not (0 >= first[1])
    assert second[2] >= first[1]  # Production of L2, not a jump to L1's lower bound.


@pytest.mark.parametrize("lower,expected", [
    ([0, HOUR], 0),
    ([0, 3 * HOUR], 2 * HOUR),
    ([-2 * HOUR, -HOUR], -2 * HOUR),
    ([-2 * HOUR, 0], -HOUR),
])
def test_all_real_positions_contribute_to_threshold(lower, expected):
    result, _, _ = scan(columns([HOUR, HOUR], [0, 1], lower), [0, 1])
    assert result == (OK, expected, 2 * HOUR, True)


def test_lower_bounds_are_indexed_by_original_source_not_node_position():
    data = columns([100, 50], [1, 0], [100, 0])
    assert scan(data, [0, 1])[0] == (OK, 0, 150, True)


@pytest.mark.parametrize("role_description", ["normal_real", "actual_transition", "split_piece"])
def test_every_real_source_obeys_lower_bound(role_description):
    # The task boundary has already validated roles; all share source-index timing.
    data = columns([10], [0], [11])
    assert scan(data, [0])[0] == (OK, 11, 10, True), role_description


def test_generated_virtual_duration_delays_later_real_start():
    data = columns([HOUR, HOUR, HOUR], [-1, 0, 1], [HOUR, 2 * HOUR])
    assert scan(data, [0, 1, 2])[0] == (OK, 0, 3 * HOUR, True)


def test_valid_zero_millisecond_split_and_repeated_source():
    data = columns([0, 10], [0, 0], [100])
    result, cursor, _ = scan(data, [0, 1], work_limit=1)
    assert result == (OK, 100, 10, True)
    assert cursor[3] == 2


def test_same_source_in_later_piece_does_not_replace_first_start_check():
    data = columns([3, 7], [0, 0], [20])
    assert scan(data, [0, 1])[0] == (OK, 20, 10, True)


def test_private_columns_and_borrowed_rows_use_existing_column_access():
    data = Columns((array([2, 3]), array([5, 7])),
                   (array([1, 0]), array([-1, 0])),
                   array([10, 0]), array([True, True], np.bool_))
    rows = array([99, 0, 2, 3, 99])[1:4]
    cursor, status = np.zeros(4, np.int64), np.zeros(5, np.int64)
    assert chain_readiness_step(data, rows, cursor, status, 256) == (OK, 3, 14, True)


def test_equal_time_is_ready_but_one_millisecond_early_is_not():
    result, _, _ = scan(columns([1], [0], [1]), [0])
    assert result == (OK, 1, 1, True)
    assert 1 >= result[1]
    assert not (0 >= result[1])


def test_int64_min_is_a_valid_release_not_an_absence_sentinel():
    assert scan(columns([1], [0], [MIN]), [0])[0] == (OK, MIN, 1, True)


@pytest.mark.parametrize("data,rows", [
    (columns([1], [0], [0]), []),
    (columns([1, 0], [-1, -1], []), [0, 1]),
    (columns([1], [0], [0], [False]), [0]),
    (columns([1], [-2], [0]), [0]),
    (columns([1], [1], [0]), [0]),
    (columns([1], [0], [0]), [-1]),
    (columns([1], [0], [0]), [1]),
    (columns([-1], [0], [0]), [0]),
    (columns([1], [0, 0], [0]), [0]),
    (columns([1], [0], [0, 1], [True]), [0]),
])
def test_invalid_input_is_not_a_readiness_result(data, rows):
    result, _, status = scan(data, rows)
    assert result == (INVALID, 0, 0, False)
    assert status[0] == INVALID


@pytest.mark.parametrize("work_limit", [0, -1])
def test_invalid_step_size_is_rejected(work_limit):
    result, cursor, status = scan(columns([1], [0], [0]), [0], work_limit)
    assert result == (INVALID, 0, 0, False)
    assert cursor.tolist() == [0, 0, 0, 0]


@pytest.mark.parametrize("cursor", [[0] * 3, [-1, 0, 0, 0], [2, 0, 0, 0],
                                   [0, 1, 0, 0], [1, 0, 0, 2]])
def test_invalid_cursor_cannot_produce_a_complete_result(cursor):
    status = np.zeros(5, np.int64)
    result = chain_readiness_step(columns([1], [0], [0]), array([0]),
                                  np.array(cursor, dtype=np.int64), status, 1)
    assert result == (INVALID, 0, 0, False)


@pytest.mark.parametrize("data", [
    columns([MAX, 1], [0, 1], [0, MAX]),
    columns([1, 1], [-1, 0], [MIN]),
])
def test_overflow_is_reported_without_wrapping_or_partial_publication(data):
    result, cursor, status = scan(data, [0, 1], work_limit=1)
    assert result == (NUMERIC_ERROR, 0, 0, False)
    assert cursor[0] == 1  # The failing node was not committed to the cursor.
    assert status[3] == 1
    snapshot = cursor.copy()
    assert chain_readiness_step(data, array([0, 1]), cursor, status, 1) == result
    np.testing.assert_array_equal(cursor, snapshot)


@pytest.mark.parametrize("work_limit", [1, 2, 7, 256])
def test_resumption_matches_one_shot_and_does_not_mutate_input(work_limit):
    data = columns([1, 0, 3, 2, 5], [0, -1, 1, -1, 0], [4, 8])
    snapshots = [value.copy() for value in data]
    result, cursor, status = scan(data, [0, 1, 2, 3, 4], work_limit)
    assert result == (OK, 7, 11, True)
    assert cursor.tolist() == [5, 11, 7, 3]
    assert chain_readiness_step(data, array([0, 1, 2, 3, 4]), cursor, status, 1) == result
    for value, snapshot in zip(data, snapshots):
        np.testing.assert_array_equal(value, snapshot)


def test_cancel_at_step_boundary_does_not_expose_or_advance_partial_summary():
    data, rows = columns([10, 20], [0, 1], [100, 200]), array([0, 1])
    cursor, status = np.zeros(4, np.int64), np.zeros(5, np.int64)
    assert chain_readiness_step(data, rows, cursor, status, 1) == (MORE_WORK, 0, 0, False)
    snapshot = cursor.copy()
    status[0] = CANCELLED
    assert chain_readiness_step(data, rows, cursor, status, 1) == (CANCELLED, 0, 0, False)
    np.testing.assert_array_equal(cursor, snapshot)


def test_existing_error_status_is_not_cleared():
    cursor, status = np.zeros(4, np.int64), np.array([NUMERIC_ERROR, 8, 9, 10, 11], np.int64)
    expected = status.copy()
    result = chain_readiness_step(columns([1], [0], [0]), array([0]), cursor, status)
    assert result == (NUMERIC_ERROR, 0, 0, False)
    np.testing.assert_array_equal(status, expected)


@pytest.mark.parametrize("work_limit", [1, 7, 256])
def test_generated_cases_match_independent_sequential_clock(work_limit):
    rng = Random(590531)
    for _ in range(100):
        count = rng.randrange(1, 20)
        durations = [rng.randrange(0, 10000) for _ in range(count)]
        lower = [rng.randrange(-20000, 20000) for _ in range(5)]
        sources = [rng.randrange(-1, 5) for _ in range(count)]
        sources[0] = 0
        rows = list(range(count))
        rng.shuffle(rows)
        result, _, _ = scan(columns(durations, sources, lower), rows, work_limit)
        code, release, duration, complete = result
        assert code == OK and complete and duration == sum(durations)
        for start in (release - 1, release, release + 1, 0):
            clock, ready = start, True
            for row in rows:
                if sources[row] >= 0:
                    ready = ready and clock >= lower[sources[row]]
                clock += durations[row]
            assert (start >= release) is ready
    assert chain_readiness_step.nopython_signatures  # Exercise actual compiled code.
