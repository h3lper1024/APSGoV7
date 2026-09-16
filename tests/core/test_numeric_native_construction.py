"""Array traversals retain ordered Hopcroft-Karp and stable random tie ordering."""

from collections import deque
from itertools import combinations
from random import Random

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_construction as construction
from apsgo_scheduler.core._numeric_kernel import task_columns, rule_tables
from apsgo_scheduler.core._numeric_state import readonly, OK, CAPACITY, MORE_WORK
from tests.core.test_numeric_construction import construction_case, budget


def reference_initial_layout(task, program, cover):
    from apsgo_scheduler.core._numeric_rules import NumericRuleKind, numeric_rows_prohibited_profile
    chains, ids, periods = [], [], []
    weights = program.for_kind(NumericRuleKind.CHAIN_WEIGHT)
    maximum = weights[0].values[1] if weights else None

    def finish(current):
        chains.append(tuple(current))
        ids.append(len(ids))
        periods.append(min(int(task.nodes.source_period[row]) for row in current))

    for p in range(cover.path_count):
        current, weight, profile = [], 0, None
        for value in cover.path_rows[cover.path_offsets[p]:cover.path_offsets[p + 1]]:
            row = int(value)
            appended = (*current, row)
            added_weight = weight + int(task.nodes.weight[row])
            if current and (maximum is None or added_weight <= maximum):
                candidate = numeric_rows_prohibited_profile(task, program, appended)
                if profile is None:
                    profile = numeric_rows_prohibited_profile(task, program, current)
                if candidate <= profile:
                    current, weight, profile = list(appended), added_weight, candidate
                    continue
            if current:
                finish(current)
            current, weight, profile = [row], int(task.nodes.weight[row]), None
        finish(current)
    if program.for_kind(NumericRuleKind.DELIVERY):
        order = sorted(range(len(chains)), key=periods.__getitem__)
        chains, ids, periods = ([values[i] for i in order] for values in (chains, ids, periods))
    return chains, ids, periods


@pytest.mark.parametrize("seed", range(8))
def test_native_initial_layout_matches_reference_cuts_period_grouping_and_identity(seed, monkeypatch):
    from apsgo_scheduler.core import _numeric_rules as old_rules
    from apsgo_scheduler.core._numeric_state import NumericPlan
    rng = Random(seed)
    task, program, quality = construction_case(
        weights=tuple(str(rng.choice((100, 400, 600))) for _ in range(12)),
        widths=tuple(str(rng.choice((1000, 980, 960))) for _ in range(12)),
        source_periods=tuple(rng.choice(("P0", "P1")) for _ in range(12)))
    graph = construction.build_numeric_construction_graph(task, program, budget(), seed=seed)
    cover = construction.numeric_minimum_path_cover(graph, budget())
    chains, ids, periods = reference_initial_layout(task, program, cover)
    expected = NumericPlan.build(task, [r for chain in chains for r in chain],
        np.concatenate(([0], np.cumsum([len(chain) for chain in chains]))), ids, periods)

    def forbidden(*args, **kwargs):
        raise AssertionError("object chain wrapper re-entered")

    monkeypatch.setattr(old_rules, "numeric_rows_prohibited_profile", forbidden)
    runtime = budget()
    actual = construction.construct_numeric_initial_plan(task, program, quality, graph, cover, runtime)
    assert actual.complete and actual.plan.fingerprint == expected.fingerprint
    assert runtime.candidate_check_count == 0
    assert construction._initial_layout_step.nopython_signatures


def test_initial_chunk_continuation_and_arithmetic_failure_have_no_partial_publication():
    from apsgo_scheduler.core._numeric_state import NumericChainView, INVALID, NUMERIC_ERROR
    task, program, quality = construction_case(weights=("100",) * 6, widths=("1000",) * 6)
    graph = construction.build_numeric_construction_graph(task, program, budget(), seed=7)
    cover = construction.numeric_minimum_path_cover(graph, budget())
    columns, rules = task_columns(task), rule_tables(program.rules)
    single_weight = int(columns.weight[0])

    def build_view():
        return NumericChainView(cover.path_rows, np.empty(0, np.int64),
            np.zeros(6, np.int64), np.zeros(6, np.int64), np.zeros(6, np.bool_),
            np.zeros(6, np.int64), np.zeros(6, np.int64), 6, 0)

    snapshots = []
    for limit in (1, 2, 256):
        view, cursor, status = build_view(), np.zeros(8, np.int64), np.zeros(5, np.int64)
        while True:
            code = construction._initial_layout_step(columns, rules, cover.path_offsets, single_weight * 2,
                view, cursor, status, limit)
            if code != MORE_WORK:
                break
        assert code == OK
        snapshots.append((view.starts[:cursor[7]].tolist(), view.stops[:cursor[7]].tolist()))
    assert snapshots[0] == snapshots[1] == snapshots[2]
    view, cursor, status = build_view(), np.zeros(8, np.int64), np.zeros(5, np.int64)
    assert construction._initial_layout_step(columns, rules, cover.path_offsets, single_weight - 1,
        view, cursor, status, 256) == INVALID
    assert cursor[7] == 0
    huge = columns._replace(weight=np.full(6, (1 << 63) - 1, np.int64))
    cursor, status = np.zeros(8, np.int64), np.zeros(5, np.int64)
    assert construction._initial_layout_step(huge, rules, cover.path_offsets, -1,
        view, cursor, status, 256) == NUMERIC_ERROR


def test_initial_mid_chunk_cancel_does_not_publish_any_plan():
    task, program, quality = construction_case(weights=("100",) * 300, widths=("1000",) * 300)
    graph = construction.build_numeric_construction_graph(task, program, budget(), seed=7)
    cover = construction.numeric_minimum_path_cover(graph, budget())

    class CancelAfter:
        calls = 0

        def is_cancelled(self):
            self.calls += 1
            return self.calls > 1

    runtime = budget(cancellation=CancelAfter())
    initial = construction.construct_numeric_initial_plan(task, program, quality, graph, cover, runtime)
    assert not initial.complete and initial.plan is None and initial.fingerprint is None
    assert runtime.candidate_check_count == 0


def reference_matching(ordered, adjacency):
    """Frozen traversal order from the pre-unification implementation."""
    count = len(ordered)
    left, right = [-1] * count, [-1] * count
    distance = [0] * count
    infinity = count + 1
    while True:
        queue = deque()
        found = False
        for node in ordered:
            distance[node] = 0 if left[node] < 0 else infinity
            if left[node] < 0:
                queue.append(node)
        while queue:
            node = queue.popleft()
            for target in adjacency[node]:
                next_left = right[target]
                if next_left < 0:
                    found = True
                elif distance[next_left] == infinity:
                    distance[next_left] = distance[node] + 1
                    queue.append(next_left)
        if not found:
            return left
        for start in ordered:
            if left[start] >= 0:
                continue
            stack = [(start, iter(adjacency[start]), None)]
            while stack:
                node, iterator, _ = stack[-1]
                target = next(iterator, None)
                if target is None:
                    distance[node] = infinity
                    stack.pop()
                    continue
                next_left = right[target]
                if next_left < 0:
                    for parent, _, incoming in reversed(stack):
                        left[parent], right[target] = target, parent
                        target = incoming
                    break
                if distance[next_left] == distance[node] + 1:
                    stack.append((next_left, iter(adjacency[next_left]), target))


def test_all_small_ordered_graphs_match_the_original_queue_and_iterator_stack():
    for count in range(1, 5):
        pairs = tuple(combinations(range(count), 2))
        for mask in range(1 << len(pairs)):
            rng = Random(mask)
            ordered = list(range(count))
            rng.shuffle(ordered)
            adjacency = [[] for _ in ordered]
            for bit, (i, j) in enumerate(pairs):
                if mask & (1 << bit):
                    adjacency[ordered[i]].append(ordered[j])
            offsets, rows = [0], []
            for source in ordered:
                rng.shuffle(adjacency[source])
                rows.extend(adjacency[source])
                offsets.append(len(rows))
            graph = construction.NumericConstructionGraph("task", "rules", readonly(ordered, np.int64),
                readonly(offsets, np.int64), readonly(rows, np.int64), len(pairs), len(rows), 0, None, 0.0, "graph")
            actual = construction.numeric_minimum_path_cover(graph, budget())
            assert actual.matching_successor.tolist() == reference_matching(ordered, adjacency)
            assert actual.path_rows.size == count
    assert construction._bfs_step.nopython_signatures
    assert construction._dfs_step.nopython_signatures


def test_stable_numeric_sort_preserves_exact_python_key_and_shuffle_order():
    task, _, _ = construction_case(weights=("100", "100", "150", "150", "200", "100"),
        widths=("1000", "1000", "1000", "990", "1000", "1000"),
        temperatures=(("700", "800"), ("710", "800"), ("700", "800"),
                      ("710", "800"), ("710", "800"), ("700", "800")))
    columns = task_columns(task)
    for seed in range(10):
        original, actual = list(range(6)), np.arange(6, dtype=np.int64)
        Random(seed).shuffle(original)
        Random(seed).shuffle(actual)
        assert actual.tolist() == original
        priority = lambda r: tuple(int(v) for v in task.priority[:, r])
        expected = sorted(original, key=lambda r: (-int(task.nodes.width[r]), priority(r),
            int(task.nodes.thickness[r]), int(task.nodes.min_temperature[r])))
        construction._stable_sort_rows(columns, actual)
        assert actual.tolist() == expected
        for anchor in range(6):
            current = np.array(original, dtype=np.int64)
            expected = sorted(original, key=lambda r: (abs(int(task.nodes.thickness[anchor]) - int(task.nodes.thickness[r])),
                                                       -int(task.nodes.weight[r]), priority(r)))
            construction._stable_sort_rows(columns, current, anchor)
            assert current.tolist() == expected
    assert int(construction._distance_unsigned(-(1 << 63), (1 << 63) - 1)) == (1 << 64) - 1


def test_graph_capacity_retry_does_not_duplicate_checks_or_edges():
    task, program, _ = construction_case(weights=("100",) * 3, widths=("1000",) * 3)
    columns, rules = task_columns(task), rule_tables(program.rules)
    order = np.arange(3, dtype=np.int64)
    counts = np.zeros(3, dtype=np.int64)
    status, cursor = construction._scan_graph_row(columns, rules, order, 0, 1, np.empty(0, np.int64), counts, 1)
    assert status == CAPACITY and cursor == 1 and counts.tolist() == [0, 0, 0]
    output = np.empty(2, dtype=np.int64)
    status, cursor = construction._scan_graph_row(columns, rules, order, 0, cursor, output, counts, 1)
    assert status == MORE_WORK and cursor == 2 and counts.tolist() == [1, 1, 0]
    status, cursor = construction._scan_graph_row(columns, rules, order, 0, cursor, output, counts, 1)
    assert status == OK and counts.tolist() == [2, 2, 0] and output.tolist() == [1, 2]


def test_mid_scan_cancellation_returns_only_unsigned_partial_construction():
    class CancelAfter:
        def __init__(self, limit):
            self.calls, self.limit = 0, limit

        def is_cancelled(self):
            self.calls += 1
            return self.calls >= self.limit

    task, program, _ = construction_case(weights=("100",) * 300, widths=("1000",) * 300)
    interrupted = construction.build_numeric_construction_graph(task, program,
        budget(cancellation=CancelAfter(4)), seed=7)
    assert not interrupted.complete and interrupted.fingerprint is None
    assert 0 < interrupted.checked_edge_count <= 256
    assert interrupted.adjacency_offsets[-1] == interrupted.adjacency_rows.size
    full = construction.build_numeric_construction_graph(task, program, budget(), seed=7)
    runtime = budget(cancellation=CancelAfter(4))
    cover = construction.numeric_minimum_path_cover(full, runtime)
    assert not cover.complete and cover.fingerprint is None
    assert cover.path_rows.size == cover.path_offsets.size == 0
    assert runtime.candidate_check_count == 0
