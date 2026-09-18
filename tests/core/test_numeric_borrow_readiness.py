"""FB1 function tests only: evaluated times/lineage are explicit numeric fixtures."""

from dataclasses import dataclass, replace
from random import Random

import numpy as np
import pytest

from apsgo_scheduler.core._numeric_borrow_readiness import (
    BORROW_ALLOWED, BORROW_BEFORE_EARLIEST, BORROW_CURSOR_SIZE,
    BORROW_EXISTING_WORSENED, BorrowReadinessIndex, BorrowReadinessPieces,
    borrow_readiness_step,
)
from apsgo_scheduler.core._numeric_kernel import TaskColumns, _GENERATED_VIRTUAL
from apsgo_scheduler.core._numeric_state import (
    CANCELLED, INVALID, MORE_WORK, NUMERIC_ERROR, OK, STALE, NumericChainView,
)

MAX, MIN = (1 << 63) - 1, -(1 << 63)


def frozen(values, dtype=np.int64):
    value = np.array(values, dtype=dtype)
    value.setflags(write=False)
    return value


def columns(source, periods, durations, lower, roles=None):
    result = TaskColumns(*(frozen([]) for _ in TaskColumns._fields))
    roles = roles if roles is not None else [
        _GENERATED_VIRTUAL if s == -1 else 0 for s in source]
    return result._replace(source=frozen(source), period=frozen(periods),
        duration=frozen(durations), role=frozen(roles), earliest_start=frozen(lower),
        has_earliest_start=frozen([True] * len(lower), np.bool_))


@dataclass
class Case:
    old_columns: object
    columns: object
    index: object
    view: object
    ends: np.ndarray
    pieces: object

    def step(self, cursor, status, limit=256):
        return borrow_readiness_step(self.old_columns, self.columns, self.index,
            self.view, self.ends, self.pieces, cursor, status, limit)


def make_case(sources, periods, durations, lower, old_chains, new_chains,
              old_periods, new_periods, *, old_count=None, roles=None):
    n = len(sources)
    old_count = n if old_count is None else old_count
    new = columns(sources, periods, durations, lower, roles)
    old = new._replace(**{name: getattr(new, name)[:old_count]
                          for name in ("source", "duration", "period", "role")})
    def layout(chains):
        rows, offsets, ends, clock = [], [0], [], 0
        for chain in chains:
            for row in chain:
                rows.append(row)
                clock += durations[row]
                ends.append(clock)
            offsets.append(len(rows))
        return frozen(rows), frozen(offsets), frozen(ends)
    rows, offsets, old_ends = layout(old_chains)
    row_chain, row_position = [-1] * old_count, [-1] * old_count
    for chain, entries in enumerate(old_chains):
        for position, row in enumerate(entries):
            row_chain[row], row_position[row] = chain, position
    index = BorrowReadinessIndex(rows, offsets, frozen(old_periods),
        frozen(row_chain), frozen(row_position), old_ends, 4)
    new_rows, new_offsets, ends = layout(new_chains)
    view = NumericChainView(rows, new_rows, new_offsets[:-1], new_offsets[1:],
        frozen([True] * len(new_chains), np.bool_), frozen(range(len(new_chains))),
        frozen(new_periods), len(new_chains), 7)
    pieces = BorrowReadinessPieces(0, frozen([-1] * old_count), frozen([-1] * n),
        frozen([-1] * n), frozen([-1] * n), frozen([]), frozen([]))
    return Case(old, new, index, view, ends, pieces)


def finish(case, limit=256):
    cursor, status = np.zeros(BORROW_CURSOR_SIZE, np.int64), np.zeros(5, np.int64)
    for _ in range(10000):
        result = case.step(cursor, status, limit)
        if result[0] != MORE_WORK:
            return result, cursor, status
        assert not result[1] and not result[2] and result[3].row == -1
        assert status[0] == OK
    pytest.fail("bounded scan did not terminate")


def time_case(old_start=20, new_start=40, lower=100, old_period=1, new_period=1, role=0):
    # The two generated prefixes switch places; all durations stay immutable.
    # Row 2 / source 0 / flat position 1 deliberately have different meanings.
    return make_case([-1, -1, 0, 1], [0, 0, 3, 0],
        [old_start, new_start, 1, 1], [lower, 0],
        [[0, 2, 1, 3]], [[1, 2, 0, 3]], [old_period], [new_period],
        roles=[_GENERATED_VIRTUAL, _GENERATED_VIRTUAL, role, 0])


@pytest.mark.parametrize("old_p,new_p,old_start,new_start,lower,reason", [
    (3, 0, 20, 40, 100, BORROW_BEFORE_EARLIEST),
    (2, 1, 20, 90, 100, BORROW_BEFORE_EARLIEST),  # deeper, although early amount fell
    (3, 0, 20, 100, 100, BORROW_ALLOWED),
    (3, 0, 20, 99, 100, BORROW_BEFORE_EARLIEST),
    (3, 0, 20, 101, 100, BORROW_ALLOWED),
    (1, 1, 20, 40, 100, BORROW_ALLOWED),
    (1, 1, 20, 20, 100, BORROW_ALLOWED),
    (1, 1, 100, 99, 100, BORROW_EXISTING_WORSENED),
    (1, 1, 40, 20, 100, BORROW_EXISTING_WORSENED),
    (1, 1, 120, 110, 100, BORROW_ALLOWED),
    (0, 1, 20, 40, 100, BORROW_ALLOWED),  # partial return but still borrowed
    (0, 1, 40, 20, 100, BORROW_EXISTING_WORSENED),
    (0, 3, 40, 20, 100, BORROW_ALLOWED),  # fully returned; original rules still apply
    (3, 3, 40, 20, 100, BORROW_ALLOWED),
    (3, 0, 20, 0, -1, BORROW_ALLOWED),
    (3, 0, 20, 0, MIN, BORROW_ALLOWED),
    (3, 0, 20, 0, MAX, BORROW_BEFORE_EARLIEST),
])
def test_new_deeper_existing_and_returned_decisions(old_p, new_p, old_start, new_start, lower, reason):
    result, _, _ = finish(time_case(old_start, new_start, lower, old_p, new_p))
    assert result[:3] == (OK, reason == BORROW_ALLOWED, True)
    assert result[3].reason == reason
    if reason:
        witness = result[3]
        assert (witness.row, witness.source, witness.flat_position) == (2, 0, 1)
        assert witness.has_old and witness.old_period == old_p and witness.new_period == new_p
        assert witness.old_start_ms == old_start and witness.new_start_ms == new_start
        assert witness.old_early_ms == max(0, lower - old_start)
        assert witness.new_early_ms == max(0, lower - new_start)


@pytest.mark.parametrize("role", [0, 1])
def test_real_and_transition_material_are_not_exempt(role):
    result, _, _ = finish(time_case(role=role, old_period=3))
    assert result[3].reason == BORROW_BEFORE_EARLIEST


def test_final_legal_bridge_time_not_the_old_insertion_estimate():
    # Milliseconds here represent the already evaluated real bridge durations.
    result, _, _ = finish(time_case(old_start=10, new_start=30, lower=30, old_period=3))
    assert result[:3] == (OK, True, True)


def test_unchanged_node_affected_by_removed_prefix_is_protected():
    case = time_case(old_start=100, new_start=20, lower=50)
    case = replace(case, view=case.view._replace(
        changed_rows=frozen([1, 2, 3]), starts=frozen([0]), stops=frozen([3])),
        ends=frozen([20, 21, 22]))
    result, _, _ = finish(case)
    assert result[3].reason == BORROW_EXISTING_WORSENED
    assert result[3].row == 2


def test_implicit_borrow_from_target_period_change_checks_all_real_rows():
    case = make_case([0, 1, 2], [0, 2, 2], [1, 1, 1], [0, 0, 10],
        [[0], [1, 2]], [[0, 1, 2]], [0, 2], [0])
    result, _, _ = finish(case)
    assert result[3].reason == BORROW_BEFORE_EARLIEST
    assert result[3].row == 2  # row 1 is ready; the chain head alone is insufficient


def test_old_chain_local_position_is_converted_to_global_position():
    case = make_case([2, 3, 0, 1], [3, 0, 1, 0], [10, 20, 30, 40],
        [0, 0, 80, 0], [[3, 1], [2, 0]], [[3, 1], [0, 2]], [0, 1], [0, 1])
    result, _, _ = finish(case)
    witness = result[3]
    assert (witness.row, witness.source, witness.flat_position) == (0, 2, 2)
    assert (witness.old_start_ms, witness.new_start_ms) == (90, 60)
    assert witness.new_early_ms == 20


def test_same_source_old_pieces_are_compared_individually():
    case = make_case([0, 0, 1], [2, 2, 0], [5, 5, 20], [20, 0],
        [[2, 0, 1]], [[0, 2, 1]], [0], [0])
    case = replace(case, pieces=case.pieces._replace(old_group_count=1,
        old_node_groups=frozen([0, 0, -1]), node_groups=frozen([0, 0, -1]),
        piece_indices=frozen([1, 2, -1]), piece_counts=frozen([2, 2, -1]),
        group_parents=frozen([0]), group_periods=frozen([0])))
    result, _, _ = finish(case)
    assert result[3].row == 0 and result[3].old_start_ms == 20
    assert result[3].new_start_ms == 0


def split_case(*, lower=5, first_duration=5, target=0):
    case = make_case([0, 1, -1, 0, 0], [2, 0, 0, 2, 2],
        [10, 5, 1, first_duration, 10-first_duration], [lower, 0],
        [[1], [0]], [[1, 3, 4]], [0, 2], [target], old_count=3)
    return replace(case, pieces=BorrowReadinessPieces(0, frozen([-1, -1, -1]),
        frozen([-1, -1, -1, 0, 0]), frozen([-1, -1, -1, 1, 2]),
        frozen([-1, -1, -1, 2, 2]), frozen([0]), frozen([target])))


def private_columns(case):
    old_count = case.index.row_chain.size
    candidate = case.columns._replace(**{name: (
        getattr(case.columns, name)[:old_count], getattr(case.columns, name)[old_count:])
        for name in ("duration", "source", "role", "period")})
    p = case.pieces
    pieces = p._replace(**{name: (getattr(p, name)[:old_count], getattr(p, name)[old_count:])
                          for name in ("node_groups", "piece_indices", "piece_counts")},
        group_parents=(p.group_parents[:p.old_group_count], p.group_parents[p.old_group_count:]),
        group_periods=(p.group_periods[:p.old_group_count], p.group_periods[p.old_group_count:]))
    return replace(case, columns=candidate, pieces=pieces)


@pytest.mark.parametrize("lower,first_duration,allowed", [(5, 5, True), (6, 5, False), (5, 0, True), (6, 0, False)])
def test_authorized_new_pieces_private_and_materialized_match(lower, first_duration, allowed):
    case = split_case(lower=lower, first_duration=first_duration)
    flat, _, _ = finish(case)
    private, _, _ = finish(private_columns(case), limit=1)
    assert private == flat
    assert flat[:3] == (OK, allowed, True)
    if not allowed:
        w = flat[3]
        assert w.reason == BORROW_BEFORE_EARLIEST and w.row == 3
        assert not w.has_old and w.old_period == -1
        assert (w.old_start_ms, w.old_early_ms) == (0, 0)  # unused, has_old is the presence flag


def test_new_pieces_returned_to_source_period_are_not_borrowed():
    assert finish(split_case(lower=100, target=2))[0][:3] == (OK, True, True)


@pytest.mark.parametrize("damage", ["no_group", "old_group", "unknown_group", "unknown_parent",
    "parent_inactive", "parent_already_split", "target_period", "piece_zero", "piece_too_large", "count_one",
    "wrong_source", "wrong_period", "missing_piece_column", "group_size"])
def test_new_piece_cannot_bypass_provenance_checks(damage):
    case = split_case()
    p = case.pieces
    if damage == "no_group": p = p._replace(node_groups=frozen([-1]*5))
    if damage == "old_group": p = p._replace(old_group_count=1)
    if damage == "unknown_group": p = p._replace(node_groups=frozen([-1,-1,-1,7,7]))
    if damage == "unknown_parent": p = p._replace(group_parents=frozen([99]))
    if damage == "parent_already_split": p = p._replace(old_node_groups=frozen([0,-1,-1]))
    if damage == "target_period": p = p._replace(group_periods=frozen([1]))
    if damage == "piece_zero": p = p._replace(piece_indices=frozen([-1,-1,-1,0,2]))
    if damage == "piece_too_large": p = p._replace(piece_indices=frozen([-1,-1,-1,3,2]))
    if damage == "count_one": p = p._replace(piece_counts=frozen([-1,-1,-1,1,1]))
    if damage == "missing_piece_column": p = p._replace(piece_indices=frozen([]))
    if damage == "group_size": p = p._replace(group_periods=frozen([]))
    if damage == "parent_inactive":
        case = replace(case, index=case.index._replace(row_chain=frozen([-1,0,-1])))
    if damage == "wrong_source": case = replace(case, columns=case.columns._replace(source=frozen([0,1,-1,1,0])))
    if damage == "wrong_period": case = replace(case, columns=case.columns._replace(period=frozen([2,0,0,3,2])))
    result, _, _ = finish(replace(case, pieces=p))
    assert result[:3] == (INVALID, False, False)
    assert result[3].row == -1


@pytest.mark.parametrize("field,value", [("row_chain",[-1]*4), ("row_position",[-1]*4),
    ("row_chain",[0,0,99,0]), ("row_position",[0,2,99,3]),
    ("row_position",[0,2,0,3]), ("ends",[]), ("row_chain",[]), ("offsets",[0,99])])
def test_invalid_old_mapping_or_summary_is_not_a_new_piece(field, value):
    case = time_case()
    result, _, _ = finish(replace(case, index=case.index._replace(**{field:frozen(value)})))
    assert result[:3] == (INVALID, False, False)


@pytest.mark.parametrize("ends", [[1], [1,2,3,4,5], [0,1,2,3], [40,100,101,102]])
def test_candidate_summary_length_and_time_mismatch(ends):
    case = time_case()
    assert finish(replace(case, ends=frozen(ends)))[0][:3] == (INVALID, False, False)


@pytest.mark.parametrize("damage", ["missing_lower", "changed_lower", "missing_old_lower", "bad_source",
    "bad_real_role", "bad_virtual_source", "negative_period", "negative_duration", "changed_duration",
    "column_length", "bad_row", "backwards_periods", "unknown_period", "empty_chain", "slice_range"])
def test_invalid_columns_and_views_report_error(damage):
    case = time_case()
    c, v = case.columns, case.view
    if damage == "missing_lower": c = c._replace(has_earliest_start=frozen([False,True],np.bool_))
    if damage == "changed_lower": c = c._replace(earliest_start=frozen([99,0]))
    if damage == "missing_old_lower":
        case = replace(case, old_columns=case.old_columns._replace(has_earliest_start=frozen([False,True],np.bool_)))
    if damage == "bad_source": c = c._replace(source=frozen([-1,-1,99,1]))
    if damage == "bad_real_role": c = c._replace(role=frozen([2,2,2,0]))
    if damage == "bad_virtual_source": c = c._replace(source=frozen([-1,-2,0,1]))
    if damage == "negative_period": c = c._replace(period=frozen([0,0,-1,0]))
    if damage == "negative_duration": c = c._replace(duration=frozen([20,40,-1,1]))
    if damage == "changed_duration":
        case = replace(case, old_columns=case.old_columns._replace(duration=frozen([20,40,2,1])))
    if damage == "column_length": c = c._replace(source=frozen([]))
    if damage == "bad_row": v = v._replace(changed_rows=frozen([1,99,0,3]))
    if damage == "backwards_periods":
        v = v._replace(starts=frozen([0,2]), stops=frozen([2,4]),
            private=frozen([True,True],np.bool_),ids=frozen([0,1]),periods=frozen([2,0]),count=2)
    if damage == "unknown_period": v = v._replace(periods=frozen([9]))
    if damage == "empty_chain": v = v._replace(stops=frozen([0]))
    if damage == "slice_range": v = v._replace(stops=frozen([99]))
    result, _, _ = finish(replace(case, columns=c, view=v))
    assert result[:3] == (INVALID, False, False)


@pytest.mark.parametrize("where", ["candidate", "current"])
def test_start_subtraction_overflow_is_numeric_error(where):
    case = time_case()
    if where == "candidate":
        case = replace(case, ends=frozen([MIN,0,0,0]))
    else:
        case = replace(case, index=case.index._replace(ends=frozen([20,MIN,0,0])))
    result, _, _ = finish(case)
    assert result[:3] == (NUMERIC_ERROR, False, False)


def test_negative_candidate_start_is_not_an_early_business_rejection():
    case = time_case()
    assert finish(replace(case, ends=frozen([39,40,60,61])))[0][:3] == (INVALID, False, False)


def test_virtual_only_chain_is_not_a_valid_pass():
    case = time_case(lower=0)
    view = case.view._replace(starts=frozen([0,1]), stops=frozen([1,4]), count=2,
        ids=frozen([0,1]), periods=frozen([1,1]), private=frozen([True,True],np.bool_))
    assert finish(replace(case, view=view))[0][:3] == (INVALID, False, False)


@pytest.mark.parametrize("limit", [1,2,3,256])
def test_bounded_resume_matches_one_shot_and_runs_nopython(limit):
    case = time_case(new_start=100)
    result, _, _ = finish(case, limit)
    assert result == finish(case, 1024)[0]
    assert borrow_readiness_step.nopython_signatures


def test_base_and_changed_candidate_slices_match_flat_candidate():
    case = make_case([0,1,2],[0,2,2],[5,1,1],[0,6,0],
        [[0],[1,2]], [[0],[2,1]], [0,2],[0,0])
    view = case.view._replace(private=frozen([False,True],np.bool_))
    assert finish(replace(case, view=view))[0] == finish(case)[0]


@pytest.mark.parametrize("terminal", [CANCELLED, STALE, NUMERIC_ERROR])
def test_caller_stop_after_partial_work_never_returns_pass(terminal):
    case = time_case()
    cursor,status=np.zeros(BORROW_CURSOR_SIZE,np.int64),np.zeros(5,np.int64)
    first = case.step(cursor,status,1)
    assert first[:3] == (MORE_WORK,False,False)
    before=cursor.copy()
    status[0]=terminal
    result=case.step(cursor,status,1)
    assert result[:3] == (terminal,False,False)
    np.testing.assert_array_equal(cursor,before)


def test_first_rejection_is_terminal_not_all_violations_or_a_later_pass():
    case = time_case(old_period=3)
    result,cursor,status=finish(case,1)
    assert result[3].row == 2
    assert case.step(cursor,status)[:3] == (INVALID,False,False)


@pytest.mark.parametrize("cursor_size,status_size,limit", [(7,5,256),(8,4,256),(8,5,0),(8,5,-1)])
def test_invalid_work_buffers(cursor_size,status_size,limit):
    result=time_case().step(np.zeros(cursor_size,np.int64),np.zeros(status_size,np.int64),limit)
    assert result[:3] == (INVALID,False,False)


def all_arrays(value):
    if isinstance(value,np.ndarray): yield value
    elif isinstance(value,tuple):
        for child in value: yield from all_arrays(child)


def test_input_arrays_are_read_only_and_unchanged():
    case=private_columns(split_case())
    arrays=[a for value in vars(case).values() for a in all_arrays(value)]
    before=[a.tobytes() for a in arrays]
    assert all(not a.flags.writeable for a in arrays)
    finish(case,1)
    assert [a.tobytes() for a in arrays] == before


def test_fixed_seed_cases_match_independent_sequential_oracle():
    rng=Random(590531)
    for _ in range(160):
        n=rng.randrange(1,13)
        sources=list(range(n)); rng.shuffle(sources)
        durations=[rng.randrange(0,21) for _ in range(n)]
        periods=[rng.randrange(4) for _ in range(n)]
        lower=[rng.randrange(-10,sum(durations)+20) for _ in range(n)]
        old_order=list(range(n)); new_order=list(range(n)); rng.shuffle(old_order); rng.shuffle(new_order)
        old_assigned=[rng.randrange(4) for _ in range(n)]
        new_assigned=[rng.randrange(4) for _ in range(n)]
        old_order.sort(key=lambda row:old_assigned[row]); new_order.sort(key=lambda row:new_assigned[row])
        case=make_case(sources,periods,durations,lower,[[r] for r in old_order],[[r] for r in new_order],
            [old_assigned[r] for r in old_order],[new_assigned[r] for r in new_order])
        old_starts={}; clock=0
        for row in old_order:
            old_starts[row]=clock; clock+=durations[row]
        clock,reason,failed_row=0,BORROW_ALLOWED,-1
        for row in new_order:
            r=lower[sources[row]]
            if new_assigned[row] < periods[row]:
                early=max(0,r-clock)
                if new_assigned[row] < old_assigned[row]:
                    if early: reason=BORROW_BEFORE_EARLIEST
                elif early>max(0,r-old_starts[row]): reason=BORROW_EXISTING_WORSENED
                if reason:
                    failed_row=row
                    break
            clock+=durations[row]
        result,_,_=finish(case,rng.choice([1,2,4,256]))
        assert result[:3] == (OK,reason==BORROW_ALLOWED,True)
        assert (result[3].reason,result[3].row)==(reason,failed_row)


def test_one_borrow_improvement_cannot_pay_for_another_borrow_worsening():
    case = make_case([0, 1], [2, 2], [30, 10], [100, 100],
        [[0, 1]], [[1, 0]], [0], [0])
    result, cursor, _ = finish(case, 1)
    assert result[3].reason == BORROW_EXISTING_WORSENED
    assert result[3].row == 1
    assert (result[3].old_early_ms, result[3].new_early_ms) == (70, 100)
    assert cursor[5] == 1 and cursor[6] == 1  # includes first rejecting visit


def test_every_step_is_bounded_including_metadata_and_complete_is_stable():
    case = time_case(lower=0)
    cursor, status = np.zeros(BORROW_CURSOR_SIZE, np.int64), np.zeros(5, np.int64)
    results = []
    for _ in range(10):
        before = int(cursor[5])
        result = case.step(cursor, status, 1)
        assert int(cursor[5]) - before <= 1
        results.append(result)
        if result[0] == OK:
            break
    assert results[0][:3] == (MORE_WORK, False, False)  # header alone consumes a step
    assert results[-1][:3] == (OK, True, True)
    assert cursor[5] == case.ends.size
    assert case.step(cursor, status) == results[-1]


@pytest.mark.parametrize("slot,value", [(0, 99), (1, -1), (2, -1), (3, 99)])
def test_invalid_resume_cursor_is_an_error(slot, value):
    cursor, status = np.zeros(BORROW_CURSOR_SIZE, np.int64), np.zeros(5, np.int64)
    cursor[slot] = value
    assert time_case().step(cursor, status)[:3] == (INVALID, False, False)
