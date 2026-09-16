"""Native chain primitives match plain sequence algebra, without full plan builds."""

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_chain_ops as ops
from apsgo_scheduler.core._numeric_state import OK, INVALID, CAPACITY, NumericPlan, NumericCandidateWorkspace
from apsgo_scheduler.core._numeric_units import NumericValueError
from tests.core.test_numeric_construction import construction_case


def workspace(*, capacity=64, chains=8):
    task, _, _ = construction_case(weights=("100",) * 7, widths=("1000",) * 7)
    plan = NumericPlan.build(task, range(7), (0, 3, 5, 7), (30, 10, 20), (0, 0, 1))
    return NumericCandidateWorkspace.allocate(task, plan, changed_capacity=capacity,
        chain_capacity=chains, node_capacity=0, group_capacity=0, event_capacity=0)


def chains(value):
    return [ops.chain_rows(value.view(), i).tolist() for i in range(value.chain_count)]


def test_slice_reverse_insert_delete_and_append_use_only_changed_storage():
    value = workspace()
    base = value.plan.node_rows.copy()
    assert np.shares_memory(ops.chain_rows(value.view(), 2), value.plan.node_rows)
    status, value.changed_count = ops.reverse_chain(value.view(), 0, value.changed_count)
    assert status == OK and chains(value)[0] == [2, 1, 0]
    for start, stop, extra, expected in (
        (1, 2, [6, 5], [2, 6, 5, 0]), (0, 2, [], [5, 0]),
        (2, 2, [4], [5, 0, 4]), (0, 0, [3], [3, 5, 0, 4]),
    ):
        status, value.changed_count = ops.replace_span(value.view(), 0, start, stop,
            np.array(extra, dtype=np.int64), value.changed_count)
        assert status == OK and chains(value)[0] == expected
    np.testing.assert_array_equal(value.plan.node_rows, base)
    assert np.shares_memory(ops.chain_rows(value.view(), 2), value.plan.node_rows)
    assert ops.reverse_chain.nopython_signatures and ops.replace_span.nopython_signatures


def test_all_inter_and_intra_moves_match_original_position_semantics():
    value = workspace()
    original = [[0, 1, 2], [3, 4], [5, 6]]
    for source in range(3):
        for target in range(3):
            for start in range(len(original[source])):
                for stop in range(start + 1, len(original[source]) + 1):
                    for position in range(len(original[target]) + 1):
                        value.reset()
                        expected = [row[:] for row in original]
                        fragment = expected[source][start:stop]
                        if source == target and start < position < stop:
                            code, end = ops.move_span(value.view(), source, target, start, stop, position, 0)
                            assert code == INVALID and end == 0 and chains(value) == original
                            continue
                        del expected[source][start:stop]
                        adjusted = position - len(fragment) if source == target and position >= stop else position
                        expected[target][adjusted:adjusted] = fragment
                        code, value.changed_count = ops.move_span(
                            value.view(), source, target, start, stop, position, 0)
                        assert code == OK
                        assert chains(value) == expected
                        value.chain_count = ops.remove_empty_chains(value.view())
                        assert chains(value) == [row for row in expected if row]
    assert ops.move_span.nopython_signatures


def test_all_cross_chain_exchange_intervals_match_sequence_algebra():
    value = workspace()
    original = chains(value)
    for source in range(3):
        for target in range(3):
            if source == target:
                continue
            for start in range(len(original[source])):
                for stop in range(start + 1, len(original[source]) + 1):
                    for other_start in range(len(original[target])):
                        for other_stop in range(other_start + 1, len(original[target]) + 1):
                            value.reset()
                            expected = [row[:] for row in original]
                            a, b = expected[source][start:stop], expected[target][other_start:other_stop]
                            expected[source][start:stop], expected[target][other_start:other_stop] = b, a
                            code, value.changed_count = ops.exchange_spans(
                                value.view(), source, target, start, stop, other_start, other_stop, 0)
                            assert code == OK and chains(value) == expected
    assert ops.exchange_spans.nopython_signatures


def test_cut_and_reorder_copy_only_references_and_preserve_ids_periods():
    value = workspace()
    code, value.chain_count = ops.cut_chain(value.view(), 0, 1, 99, 1)
    assert code == OK and value.changed_count == 0
    assert chains(value) == [[0], [1, 2], [3, 4], [5, 6]]
    assert value.ids[:4].tolist() == [30, 99, 10, 20]
    assert value.periods[:4].tolist() == [0, 1, 0, 1]
    assert ops.reorder_chains(value.view(), np.array([3, 1, 2, 0], dtype=np.int64)) == OK
    assert chains(value) == [[5, 6], [1, 2], [3, 4], [0]]
    assert all(np.shares_memory(ops.chain_rows(value.view(), i), value.plan.node_rows)
               for i in range(value.chain_count))
    assert value.ids[:4].tolist() == [20, 99, 10, 30]


def test_merge_all_positions_and_directions_keeps_target_identity():
    value = workspace()
    original = chains(value)
    for source in range(3):
        for target in range(3):
            if source == target:
                continue
            for position in range(len(original[target]) + 1):
                for reverse_source in (False, True):
                    for reverse_target in (False, True):
                        value.reset()
                        expected = [row[:] for row in original]
                        a = expected[source][::-1] if reverse_source else expected[source]
                        b = expected[target][::-1] if reverse_target else expected[target]
                        expected[target] = b[:position] + a + b[position:]
                        del expected[source]
                        code, value.changed_count, value.chain_count = ops.merge_chains(
                            value.view(), source, target, position, 0, reverse_source, reverse_target)
                        assert code == OK and chains(value) == expected
                        assert value.ids[:value.chain_count].tolist() == [n for i, n in enumerate([30, 10, 20]) if i != source]


def test_invalid_and_capacity_failures_leave_all_valid_state_unchanged():
    value = workspace(capacity=1, chains=3)
    value.changed_rows[:] = -777
    before = chains(value)
    code, end = ops.move_span(value.view(), 0, 1, 0, 1, 0, 0)
    assert code == CAPACITY and end == 0 and chains(value) == before
    assert value.changed_rows.tolist() == [-777]
    assert ops.cut_chain(value.view(), 0, 1, 99, 0) == (CAPACITY, 3)
    assert ops.cut_chain(value.view(), 0, 0, 99, 0)[0] == INVALID
    assert ops.reorder_chains(value.view(), np.array([0, 0, 2], dtype=np.int64)) == INVALID
    assert chains(value) == before and value.changed_count == 0
    value.grow_for_retry(changed_capacity=16, chain_capacity=4,
                         node_capacity=0, group_capacity=0, event_capacity=0)
    code, value.changed_count = ops.move_span(value.view(), 0, 1, 0, 1, 0, 0)
    assert code == OK and chains(value) == [[1, 2], [0, 3, 4], [5, 6]]


def test_generation_index_reuses_existing_node_and_original_position_authority():
    value = workspace()
    index = ops.NumericChainIndex.build(value.task, value.plan)
    index.require_current(value.task, value.plan)
    assert index.plan.row_to_chain is value.plan.row_to_chain
    assert index.plan.source_piece_rows is value.plan.source_piece_rows
    assert index.heads.tolist() == [0, 3, 5]
    assert index.tails.tolist() == [2, 4, 6]
    assert index.lengths.tolist() == [3, 2, 2]
    for identity, expected in ((30, 0), (10, 1), (20, 2), (99, -1)):
        assert ops.find_chain(index.sorted_ids, index.sorted_positions, identity) == expected
    with pytest.raises(NumericValueError, match="stale"):
        index.require_current(value.task, workspace().plan)


def test_operations_do_not_build_formal_plans_or_recreate_unchanged_chains(monkeypatch):
    value = workspace()

    def forbidden(*a, **kw):
        raise AssertionError("full candidate materialization forbidden")

    monkeypatch.setattr(NumericPlan, "build", forbidden)
    status, value.changed_count = ops.move_span(value.view(), 0, 1, 0, 1, 0, 0)
    assert status == OK
    assert np.shares_memory(ops.chain_rows(value.view(), 2), value.plan.node_rows)
    assert not value.private[2] and value.private[:2].all()
