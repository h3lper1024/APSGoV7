"""Native descriptors keep the frozen structural ownership and traversal order."""
from itertools import islice

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_refinement as old
from apsgo_scheduler.core import _numeric_refinement_scan as scan
from apsgo_scheduler.core._numeric_kernel import task_columns, rule_tables
from apsgo_scheduler.core._numeric_search import NumericSearchAction
from tests.core.test_numeric_construction import budget, construction_case
from tests.core.test_numeric_refinement import _state


def recipe(value):
    return (tuple(NumericSearchAction)[value[0]], *(int(value[i]) for i in (1, 2, 5, 6, 7, 8)))


def make_state(length=5):
    task, program, quality = construction_case(weights=("100",) * (length * 3),
        widths=tuple(str(1000 - (i % length) * 10) for i in range(length * 3)))
    return _state(task, program, quality, range(length * 3),
        (0, length, length * 2, length * 3), (10, 20, 30), (0, 0, 0))


@pytest.mark.parametrize("family", ("intra", "node", "block", "cut", "order"))
@pytest.mark.parametrize("lane", (False, True))
@pytest.mark.parametrize("previous", (None, 3))
def test_native_family_descriptors_match_order_and_shared_chain_ownership(family, lane, previous):
    state = make_state()
    columns = task_columns(state.task)
    x = scan.build_scan(state, columns)
    index = old.NumericRefinementIndex.build(state)
    critical = frozenset(old._critical_sources(state, index))
    index = index.with_critical_sources(state, critical)
    ordered = (*old._critical_sources(state, index),
               *(i for i in old._delivery_sources(state) if i not in critical))
    np.testing.assert_array_equal(x.sources, ordered)
    np.testing.assert_array_equal(x.critical, index.critical_prefix)
    expected = list(old._family_stream(state, family, {family: previous}, ordered,
        critical, lane, budget(), index=index, defer_cursor=True))
    actual = list(scan.family_stream(x, columns, rule_tables(state.program.rules), family, previous, lane, budget()))
    assert [(int(value[11]), recipe(value)) for value in actual] == expected
    for value in actual:
        assert value.dtype == np.int64 and value.shape == (13,)


def test_native_generator_has_bounded_continuations_and_cancel_is_not_a_candidate():
    state = make_state(100)
    x = scan.build_scan(state, task_columns(state.task))
    # An empty critical lane still has to yield control during deep rejected scans.
    raw = scan.node_exchanges(x, int(x.sources[0]), True)
    values = list(raw)
    assert any(value[0] == scan.CONTINUE for value in values)
    assert all(value[0] == scan.CONTINUE for value in values)
    class Cancelled:
        def allows_search(self):
            return False
    assert list(scan.bounded(scan.node_exchanges(x, int(x.sources[0]), True), Cancelled())) == []


def test_native_descriptor_stream_stops_without_materializing_all_candidates():
    state = make_state(20)
    columns = task_columns(state.task)
    x = scan.build_scan(state, columns)
    runtime = budget()
    values = list(islice(scan.family_stream(x, columns, rule_tables(state.program.rules),
        "block", None, False, runtime), 8))
    assert len(values) == 8
    assert runtime.candidate_check_count == 0


@pytest.mark.parametrize("family", ("intra", "node", "block", "cut", "order"))
@pytest.mark.parametrize("lane", (False, True))
def test_mixed_critical_positions_match_native_structural_ownership(family, lane):
    state = make_state()
    columns = task_columns(state.task)
    x = scan.build_scan(state, columns)
    critical = frozenset((0, 3, 7))
    index = old.NumericRefinementIndex.build(state, critical)
    x = x._replace(critical=index.critical_prefix)
    sources = tuple(map(int, x.sources))
    expected = list(old._family_stream(state, family, {family: None}, sources, critical,
        lane, budget(), index=index, defer_cursor=True))
    actual = list(scan.family_stream(x, columns, rule_tables(state.program.rules),
        family, None, lane, budget()))
    assert [(int(value[11]), recipe(value)) for value in actual] == expected


def test_production_refinement_does_not_call_old_descriptor_generators(monkeypatch):
    state = make_state()
    def forbidden(*args, **kwargs):
        raise AssertionError("old Python descriptor enumeration reached")
    for name in ("_critical_sources", "_delivery_sources", "_family_stream",
                 "_source_recipe_stream", "_structural_ownership", "_reclamation_stream"):
        monkeypatch.setattr(old, name, forbidden)
    old.improve_numeric_refinement(state, budget(candidate_limit=20))


def test_empty_cut_at_maximum_identity_has_no_speculative_error():
    state = make_state(1)
    x = scan.build_scan(state, task_columns(state.task))
    x = x._replace(ids=np.array((10, 20, np.iinfo(np.int64).max), np.int64))
    assert list(scan.chain_candidates(x, scan.CUT, 2)) == []


def test_ignored_negative_due_does_not_overflow_a_priority_subtraction():
    state = make_state(1)
    t = task_columns(state.task)
    due = np.full(t.due.size, np.iinfo(np.int64).min, np.int64)
    t = t._replace(due=due)
    plan = state.plan
    result = scan.source_priority_values(t, plan.node_rows, plan.chain_offsets,
        state.task.nodes.split_group, state.task.split_groups.target_period,
        state.evaluation.delivery.original_completion_ms)
    assert result[0] == scan.OK
