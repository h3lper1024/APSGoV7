"""Deterministic numeric construction graph, path cover and initial plan."""

from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from random import Random
from time import perf_counter

import numpy as np
from numba import njit

from ._numeric_kernel import task_columns, rule_tables, edge_node, all_edges_allowed_values
from ._numeric_state import OK, INVALID, CAPACITY, MORE_WORK

from ._numeric_evaluation import (
    NumericPlanEvaluation,
    NumericQualityProgram,
    evaluate_numeric_plan,
)
from ._numeric_rules import (
    NumericRuleKind,
    NumericRuleProgram,
    numeric_rows_prohibited_profile,
    numeric_edge_allowed,
)
from ._numeric_state import NumericPlan, NumericTask, readonly
from ._numeric_units import NumericValueError, checked_sum, int64
from .budget import SolveRuntimeBudget
from .contracts import SearchStopReason, fingerprint


def _array_fingerprint(kind, identities, arrays):
    digest = sha256()
    digest.update(fingerprint({"kind": kind, **identities}).encode("ascii"))
    for name, value in arrays:
        digest.update(fingerprint((name, value.shape, value.dtype.str)).encode("ascii"))
        digest.update(value.astype(value.dtype.newbyteorder("<"), copy=False).tobytes())
    return digest.hexdigest()


def _validate_i64(value, name):
    if (
        not isinstance(value, np.ndarray)
        or value.ndim != 1
        or value.dtype != np.int64
        or value.flags.writeable
        or not value.flags.c_contiguous
    ):
        raise NumericValueError(name, "read-only one-dimensional int64 array required")


def _validate_elapsed(value):
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        raise NumericValueError("elapsed_seconds", "finite nonnegative elapsed time required")


@dataclass(frozen=True, slots=True, eq=False)
class NumericConstructionGraph:
    task_fingerprint: str
    rule_program_fingerprint: str
    ordered_rows: np.ndarray
    adjacency_offsets: np.ndarray
    adjacency_rows: np.ndarray
    checked_edge_count: int
    allowed_edge_count: int
    direction_rejected_edge_count: int
    stop_reason: SearchStopReason | None
    elapsed_seconds: float
    fingerprint: str | None

    def __post_init__(self):
        for name in ("task_fingerprint", "rule_program_fingerprint"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise NumericValueError("graph", f"nonempty {name} required")
        for value, name in (
            (self.ordered_rows, "ordered_rows"),
            (self.adjacency_offsets, "adjacency_offsets"),
            (self.adjacency_rows, "adjacency_rows"),
        ):
            _validate_i64(value, name)
        count = self.ordered_rows.size
        if (
            self.adjacency_offsets.size != count + 1
            or self.adjacency_offsets[0] != 0
            or self.adjacency_offsets[-1] != self.adjacency_rows.size
            or np.any(self.adjacency_offsets[1:] < self.adjacency_offsets[:-1])
            or np.any(self.ordered_rows < 0)
            or np.any(self.ordered_rows >= count)
            or np.unique(self.ordered_rows).size != count
        ):
            raise NumericValueError("graph", "invalid numeric adjacency layout")
        position = np.empty(count, dtype=np.int64)
        position[self.ordered_rows] = np.arange(count, dtype=np.int64)
        for left_position, left in enumerate(self.ordered_rows):
            start = int(self.adjacency_offsets[left_position])
            stop = int(self.adjacency_offsets[left_position + 1])
            targets = self.adjacency_rows[start:stop]
            if (
                np.any(targets < 0)
                or np.any(targets >= count)
                or np.unique(targets).size != targets.size
                or np.any(position[targets] <= left_position)
            ):
                raise NumericValueError("graph", f"row {int(left)} has invalid forward edges")
        for name in (
            "checked_edge_count",
            "allowed_edge_count",
            "direction_rejected_edge_count",
        ):
            if int64(getattr(self, name), name) < 0:
                raise NumericValueError(name, "nonnegative graph counter required")
        possible = count * (count - 1) // 2
        if (
            self.allowed_edge_count != self.adjacency_rows.size
            or self.allowed_edge_count + self.direction_rejected_edge_count
            > self.checked_edge_count
            or self.checked_edge_count > possible
        ):
            raise NumericValueError("graph", "edge counters do not match adjacency")
        if self.stop_reason is None:
            if self.checked_edge_count != possible or not isinstance(self.fingerprint, str):
                raise NumericValueError("graph", "complete graph requires all edges and identity")
        elif not isinstance(self.stop_reason, SearchStopReason) or self.fingerprint is not None:
            raise NumericValueError("graph", "partial graph requires stop reason and no identity")
        _validate_elapsed(self.elapsed_seconds)

    @property
    def complete(self):
        return self.stop_reason is None

    def neighbors(self, row):
        if type(row) is not int or not 0 <= row < self.ordered_rows.size:
            raise NumericValueError("row", "row is outside construction graph")
        position = int(np.flatnonzero(self.ordered_rows == row)[0])
        return self.adjacency_rows[
            int(self.adjacency_offsets[position]) : int(self.adjacency_offsets[position + 1])
        ]


@njit
def _priority_before(priority, left, right):
    for i in range(priority.shape[0]):
        if priority[i, left] != priority[i, right]:
            return -1 if priority[i, left] < priority[i, right] else 1
    return 0


@njit
def _distance_unsigned(left, right):
    # Ranking accepts the full distance between two signed endpoints without
    # overflowing a signed subtraction or changing the authoritative columns.
    return np.uint64(left) - np.uint64(right) if left >= right else np.uint64(right) - np.uint64(left)


@njit
def _row_before(columns, left, right, anchor):
    if anchor >= 0:
        a = columns.thickness[anchor] if columns.present[anchor, 1] else 0
        x = columns.thickness[left] if columns.present[left, 1] else 0
        y = columns.thickness[right] if columns.present[right, 1] else 0
        dx, dy = _distance_unsigned(a, x), _distance_unsigned(a, y)
        if dx != dy:
            return dx < dy
        if columns.weight[left] != columns.weight[right]:
            return columns.weight[left] > columns.weight[right]
        return _priority_before(columns.priority, left, right) < 0
    if columns.present[left, 0] != columns.present[right, 0]:
        return columns.present[left, 0]
    if columns.present[left, 0] and columns.width[left] != columns.width[right]:
        return columns.width[left] > columns.width[right]
    priority = _priority_before(columns.priority, left, right)
    if priority:
        return priority < 0
    if columns.present[left, 1] != columns.present[right, 1]:
        return columns.present[left, 1]
    if columns.present[left, 1] and columns.thickness[left] != columns.thickness[right]:
        return columns.thickness[left] < columns.thickness[right]
    if columns.present[left, 2] != columns.present[right, 2]:
        return columns.present[left, 2]
    return columns.present[left, 2] and columns.minimum[left] < columns.minimum[right]


@njit
def _stable_sort_rows(columns, rows, anchor=-1):
    """Stable merge sorting preserves the original Random.shuffle tie order."""
    scratch = np.empty(rows.size, dtype=np.int64)
    width = 1
    while width < rows.size:
        for start in range(0, rows.size, 2 * width):
            middle, stop = min(start + width, rows.size), min(start + 2 * width, rows.size)
            left, right = start, middle
            for out in range(start, stop):
                if left == middle or (right < stop and _row_before(columns, rows[right], rows[left], anchor)):
                    scratch[out] = rows[right]
                    right += 1
                else:
                    scratch[out] = rows[left]
                    left += 1
        rows[:] = scratch
        width *= 2


_GRAPH_CHECKED, _GRAPH_ALLOWED, _GRAPH_DIRECTION = range(3)


@njit
def _scan_graph_row(columns, rules, ordered, left_position, right_position, adjacency, counts, work_limit):
    status = np.array((OK, -1, -1, -1), dtype=np.int64)
    left = ordered[left_position]
    stop = min(ordered.size, right_position + work_limit)
    while right_position < stop:
        right = ordered[right_position]
        allowed = all_edges_allowed_values(edge_node(columns, left), edge_node(columns, right), rules, status)
        if status[0] != OK:
            return status[0], right_position
        rejected = (allowed and columns.present[left, 0] and columns.present[right, 0]
                    and columns.width[right] > columns.width[left])
        if allowed and not rejected and counts[_GRAPH_ALLOWED] == adjacency.size:
            return CAPACITY, right_position
        counts[_GRAPH_CHECKED] += 1
        if rejected:
            counts[_GRAPH_DIRECTION] += 1
        elif allowed:
            adjacency[counts[_GRAPH_ALLOWED]] = right
            counts[_GRAPH_ALLOWED] += 1
        right_position += 1
    return (OK if right_position == ordered.size else MORE_WORK), right_position


_BFS_HEAD, _BFS_TAIL, _BFS_EDGE, _BFS_FOUND = range(4)


@njit
def _bfs_initialize(ordered, matching, distance, queue):
    tail = 0
    for left in ordered:
        if matching[left] < 0:
            distance[left], queue[tail], tail = 0, left, tail + 1
        else:
            distance[left] = ordered.size + 1
    return np.array((0, tail, -1, 0), dtype=np.int64)


@njit
def _bfs_step(offsets, neighbors, positions, right_matching, distance, queue, cursor, work_limit):
    work = 0
    while cursor[_BFS_HEAD] < cursor[_BFS_TAIL] and work < work_limit:
        left = queue[cursor[_BFS_HEAD]]
        position = positions[left]
        if cursor[_BFS_EDGE] < 0:
            cursor[_BFS_EDGE] = offsets[position]
        edge = cursor[_BFS_EDGE]
        work += 1
        if edge == offsets[position + 1]:
            cursor[_BFS_HEAD] += 1
            cursor[_BFS_EDGE] = -1
            continue
        cursor[_BFS_EDGE] += 1
        next_left = right_matching[neighbors[edge]]
        if next_left < 0:
            cursor[_BFS_FOUND] = 1
        elif distance[next_left] == positions.size + 1:
            distance[next_left] = distance[left] + 1
            queue[cursor[_BFS_TAIL]] = next_left
            cursor[_BFS_TAIL] += 1
    return OK if cursor[_BFS_HEAD] == cursor[_BFS_TAIL] else MORE_WORK


@njit
def _dfs_step(offsets, neighbors, positions, matching, right_matching, distance,
              stack, edge_cursor, incoming, depth, work_limit):
    work = 0
    while depth and work < work_limit:
        top, work = depth - 1, work + 1
        left = stack[top]
        if edge_cursor[top] == offsets[positions[left] + 1]:
            distance[left] = positions.size + 1
            depth -= 1
            continue
        right = neighbors[edge_cursor[top]]
        edge_cursor[top] += 1
        next_left = right_matching[right]
        if next_left < 0:
            for i in range(depth - 1, -1, -1):
                matching[stack[i]], right_matching[right] = right, stack[i]
                right = incoming[i]
            return OK, 0
        if distance[next_left] == distance[left] + 1:
            if depth == stack.size:
                return INVALID, depth
            stack[depth], edge_cursor[depth], incoming[depth] = next_left, offsets[positions[next_left]], right
            depth += 1
    return (MORE_WORK if depth else OK), depth


@njit
def _matching_paths(ordered, matching, right_matching):
    rows, offsets = np.empty(ordered.size, dtype=np.int64), np.empty(ordered.size + 1, dtype=np.int64)
    visited = np.zeros(ordered.size, dtype=np.bool_)
    count = paths = 0
    offsets[0] = 0
    for start in ordered:
        if right_matching[start] >= 0:
            continue
        cursor = start
        while cursor >= 0:
            if cursor >= ordered.size or visited[cursor]:
                return INVALID, offsets[:paths + 1], rows[:count]
            rows[count], visited[cursor], count = cursor, True, count + 1
            cursor = matching[cursor]
        paths += 1
        offsets[paths] = count
    status = OK if count == ordered.size and paths == ordered.size - np.count_nonzero(matching >= 0) else INVALID
    return status, offsets[:paths + 1], rows[:count]


def build_numeric_construction_graph(task, program, budget, *, seed):
    if (
        not isinstance(task, NumericTask)
        or not isinstance(program, NumericRuleProgram)
        or program.task_fingerprint != task.fingerprint
    ):
        raise NumericValueError("graph", "matching numeric task and rule program required")
    if not isinstance(budget, SolveRuntimeBudget):
        raise NumericValueError("graph", "shared runtime budget required")
    if type(seed) is not int:
        raise NumericValueError("seed", "integer seed required")
    started = perf_counter()
    count = task.originals.weight.size
    ordered = np.arange(count, dtype=np.int64)
    offsets = np.zeros(count + 1, dtype=np.int64)
    maximum_edges = count * (count - 1) // 2
    adjacency = np.empty(min(maximum_edges, 4096), dtype=np.int64)
    counts = np.zeros(_GRAPH_DIRECTION + 1, dtype=np.int64)
    left_position = 0
    columns, rules = task_columns(task), rule_tables(program.rules)

    def freeze_result():
        allowed = int(counts[_GRAPH_ALLOWED])
        offsets[min(left_position + 1, count):] = allowed
        arrays = (
            ("ordered_rows", readonly(ordered, np.int64)),
            ("adjacency_offsets", readonly(offsets, np.int64)),
            ("adjacency_rows", readonly(adjacency[:allowed], np.int64)),
        )
        identity = None
        if budget.stop_reason is None:
            identity = _array_fingerprint(
                "construction_graph",
                {"task": task.fingerprint, "rules": program.fingerprint},
                arrays,
            )
        return NumericConstructionGraph(
            task.fingerprint,
            program.fingerprint,
            *(value for _, value in arrays),
            int(counts[_GRAPH_CHECKED]),
            allowed,
            int(counts[_GRAPH_DIRECTION]),
            budget.stop_reason,
            perf_counter() - started,
            identity,
        )

    if not budget.allows_search():
        return freeze_result()

    rng = Random(seed)
    rng.shuffle(ordered)
    _stable_sort_rows(columns, ordered)
    for left_position, left in enumerate(ordered):
        if not budget.allows_search():
            return freeze_result()
        offsets[left_position] = counts[_GRAPH_ALLOWED]
        right_position = left_position + 1
        while right_position < count:
            if not budget.allows_search():
                return freeze_result()
            status, right_position = _scan_graph_row(columns, rules, ordered, left_position,
                right_position, adjacency, counts, 256)
            if status == CAPACITY:
                capacity = min(maximum_edges, max(1, adjacency.size * 2))
                if capacity <= adjacency.size:
                    raise NumericValueError("graph", "adjacency capacity cannot grow")
                larger = np.empty(capacity, dtype=np.int64)
                larger[:int(counts[_GRAPH_ALLOWED])] = adjacency[:int(counts[_GRAPH_ALLOWED])]
                adjacency = larger
                continue
            if status not in (OK, MORE_WORK):
                raise NumericValueError("graph", f"numeric edge scan failed: {status}")
        offsets[left_position + 1] = counts[_GRAPH_ALLOWED]
        targets = adjacency[int(offsets[left_position]):int(offsets[left_position + 1])]
        rng.shuffle(targets)
        _stable_sort_rows(columns, targets, int(left))
    if not budget.allows_search():
        return freeze_result()
    result = freeze_result()
    if budget.allows_search():
        return result
    return NumericConstructionGraph(
        result.task_fingerprint,
        result.rule_program_fingerprint,
        result.ordered_rows,
        result.adjacency_offsets,
        result.adjacency_rows,
        result.checked_edge_count,
        result.allowed_edge_count,
        result.direction_rejected_edge_count,
        budget.stop_reason,
        perf_counter() - started,
        None,
    )


@dataclass(frozen=True, slots=True, eq=False)
class NumericPathCover:
    graph_fingerprint: str
    matching_successor: np.ndarray
    path_offsets: np.ndarray
    path_rows: np.ndarray
    stop_reason: SearchStopReason | None
    elapsed_seconds: float
    fingerprint: str | None

    def __post_init__(self):
        if not isinstance(self.graph_fingerprint, str) or not self.graph_fingerprint:
            raise NumericValueError("path_cover", "nonempty graph identity required")
        for value, name in (
            (self.matching_successor, "matching_successor"),
            (self.path_offsets, "path_offsets"),
            (self.path_rows, "path_rows"),
        ):
            _validate_i64(value, name)
        count = self.matching_successor.size
        if np.any(self.matching_successor < -1) or np.any(self.matching_successor >= count):
            raise NumericValueError("matching", "matching successor is outside graph")
        targets = self.matching_successor[self.matching_successor >= 0]
        if np.unique(targets).size != targets.size:
            raise NumericValueError("matching", "right endpoints must be unique")
        if self.stop_reason is None:
            if (
                self.path_offsets.size < 2
                or self.path_offsets[0] != 0
                or self.path_offsets[-1] != self.path_rows.size
                or np.any(self.path_offsets[1:] <= self.path_offsets[:-1])
                or self.path_rows.size != count
                or np.any(self.path_rows < 0)
                or np.any(self.path_rows >= count)
                or np.unique(self.path_rows).size != count
                or not isinstance(self.fingerprint, str)
            ):
                raise NumericValueError("path_cover", "complete path cover is invalid")
            path_successors = np.full(count, -1, dtype=np.int64)
            for path_index in range(self.path_offsets.size - 1):
                start = int(self.path_offsets[path_index])
                stop = int(self.path_offsets[path_index + 1])
                path = self.path_rows[start:stop]
                path_successors[path[:-1]] = path[1:]
            if not np.array_equal(path_successors, self.matching_successor):
                raise NumericValueError("path_cover", "paths do not restore the matching")
        elif (
            not isinstance(self.stop_reason, SearchStopReason)
            or self.path_offsets.size
            or self.path_rows.size
            or self.fingerprint is not None
        ):
            raise NumericValueError("path_cover", "partial cover requires empty paths")
        _validate_elapsed(self.elapsed_seconds)

    @property
    def complete(self):
        return self.stop_reason is None

    @property
    def path_count(self):
        return max(0, self.path_offsets.size - 1)


def numeric_minimum_path_cover(graph, budget):
    if not isinstance(graph, NumericConstructionGraph) or not graph.complete:
        raise NumericValueError("path_cover", "complete numeric construction graph required")
    if not isinstance(budget, SolveRuntimeBudget):
        raise NumericValueError("path_cover", "shared runtime budget required")
    started = perf_counter()
    count = graph.ordered_rows.size
    match_left = np.full(count, -1, dtype=np.int64)
    match_right = np.full(count, -1, dtype=np.int64)
    distance = np.empty(count, dtype=np.int64)
    positions = np.empty(count, dtype=np.int64)
    positions[graph.ordered_rows] = np.arange(count, dtype=np.int64)
    queue = np.empty(count, dtype=np.int64)
    stack = np.empty(count, dtype=np.int64)
    edge_cursor = np.empty(count, dtype=np.int64)
    incoming = np.empty(count, dtype=np.int64)

    def freeze_result(offsets=(), flattened=()):
        if budget.stop_reason is not None:
            flattened, offsets = (), ()
        arrays = (
            ("matching_successor", readonly(match_left, np.int64)),
            ("path_offsets", readonly(offsets, np.int64)),
            ("path_rows", readonly(flattened, np.int64)),
        )
        identity = None
        if budget.stop_reason is None:
            identity = _array_fingerprint(
                "path_cover", {"graph": graph.fingerprint}, arrays
            )
        return NumericPathCover(
            graph.fingerprint,
            *(value for _, value in arrays),
            budget.stop_reason,
            perf_counter() - started,
            identity,
        )

    if not budget.allows_search():
        return freeze_result()
    while True:
        cursor = _bfs_initialize(graph.ordered_rows, match_left, distance, queue)
        while True:
            if not budget.allows_search():
                return freeze_result()
            status = _bfs_step(graph.adjacency_offsets, graph.adjacency_rows, positions,
                match_right, distance, queue, cursor, 256)
            if status == OK:
                break
        if not cursor[_BFS_FOUND]:
            break
        for left_value in graph.ordered_rows:
            if not budget.allows_search():
                return freeze_result()
            left = int(left_value)
            if match_left[left] < 0:
                stack[0], edge_cursor[0], incoming[0], depth = left, graph.adjacency_offsets[positions[left]], -1, 1
                while depth:
                    if not budget.allows_search():
                        return freeze_result()
                    status, depth = _dfs_step(graph.adjacency_offsets, graph.adjacency_rows,
                        positions, match_left, match_right, distance, stack, edge_cursor,
                        incoming, depth, 256)
                    if status not in (OK, MORE_WORK):
                        raise NumericValueError("path_cover", "numeric augmenting stack invalid")
    if budget.must_stop:
        return freeze_result()

    status, offsets, rows = _matching_paths(graph.ordered_rows, match_left, match_right)
    if status != OK:
        raise NumericValueError("path_cover", "matching does not cover every graph row")
    if not budget.allows_search():
        return freeze_result()
    result = freeze_result(offsets, rows)
    if budget.allows_search():
        return result
    return freeze_result()


@dataclass(frozen=True, slots=True, eq=False)
class NumericInitialSolution:
    graph_fingerprint: str
    path_cover_fingerprint: str
    plan: NumericPlan | None
    evaluation: NumericPlanEvaluation | None
    stop_reason: SearchStopReason | None
    elapsed_seconds: float
    fingerprint: str | None

    def __post_init__(self):
        if (
            not isinstance(self.graph_fingerprint, str)
            or not self.graph_fingerprint
            or not isinstance(self.path_cover_fingerprint, str)
            or not self.path_cover_fingerprint
        ):
            raise NumericValueError("initial", "graph and path-cover identities required")
        if self.stop_reason is None:
            if (
                not isinstance(self.plan, NumericPlan)
                or not isinstance(self.evaluation, NumericPlanEvaluation)
                or self.evaluation.plan_fingerprint != self.plan.fingerprint
                or not isinstance(self.fingerprint, str)
            ):
                raise NumericValueError("initial", "complete numeric candidate required")
        elif (
            not isinstance(self.stop_reason, SearchStopReason)
            or self.plan is not None
            or self.evaluation is not None
            or self.fingerprint is not None
        ):
            raise NumericValueError("initial", "interrupted construction cannot expose candidate")
        _validate_elapsed(self.elapsed_seconds)

    @property
    def complete(self):
        return self.stop_reason is None


def construct_numeric_initial_plan(task, program, quality, graph, cover, budget):
    if (
        not isinstance(task, NumericTask)
        or not isinstance(program, NumericRuleProgram)
        or not isinstance(quality, NumericQualityProgram)
        or not isinstance(graph, NumericConstructionGraph)
        or not graph.complete
        or not isinstance(cover, NumericPathCover)
        or not cover.complete
        or program.task_fingerprint != task.fingerprint
        or quality.task_fingerprint != task.fingerprint
        or quality.rule_program_fingerprint != program.fingerprint
        or graph.task_fingerprint != task.fingerprint
        or graph.rule_program_fingerprint != program.fingerprint
        or cover.graph_fingerprint != graph.fingerprint
    ):
        raise NumericValueError("initial", "matching numeric construction inputs required")
    if not isinstance(budget, SolveRuntimeBudget):
        raise NumericValueError("initial", "shared runtime budget required")
    started = perf_counter()
    if (
        cover.matching_successor.size != graph.ordered_rows.size
        or cover.path_rows.size != graph.ordered_rows.size
        or not np.array_equal(np.sort(cover.path_rows), np.sort(graph.ordered_rows))
    ):
        raise NumericValueError("initial", "path cover rows do not match construction graph")
    graph_positions = np.empty(graph.ordered_rows.size, dtype=np.int64)
    graph_positions[graph.ordered_rows] = np.arange(graph.ordered_rows.size, dtype=np.int64)
    for path_index in range(cover.path_count):
        start = int(cover.path_offsets[path_index])
        stop = int(cover.path_offsets[path_index + 1])
        path = cover.path_rows[start:stop]
        for left_value, right_value in zip(path[:-1], path[1:]):
            left, right = int(left_value), int(right_value)
            position = int(graph_positions[left])
            targets = graph.adjacency_rows[
                int(graph.adjacency_offsets[position]) : int(graph.adjacency_offsets[position + 1])
            ]
            if not np.any(targets == right):
                raise NumericValueError("initial", "path cover contains an absent graph edge")
    weight_rules = program.for_kind(NumericRuleKind.CHAIN_WEIGHT)
    maximum = weight_rules[0].values[1] if weight_rules else None

    def interrupted():
        return NumericInitialSolution(
            graph.fingerprint,
            cover.fingerprint,
            None,
            None,
            budget.stop_reason,
            perf_counter() - started,
            None,
        )

    chains = []
    chain_ids = []
    periods = []
    for path_index in range(cover.path_count):
        if not budget.allows_search():
            return interrupted()
        start = int(cover.path_offsets[path_index])
        stop = int(cover.path_offsets[path_index + 1])
        current = []
        current_weight = 0
        current_profile = None
        for row_value in cover.path_rows[start:stop]:
            if not budget.allows_search():
                return interrupted()
            row = int(row_value)
            weight = int(task.nodes.weight[row])
            if maximum is not None and weight > maximum:
                raise NumericValueError("initial", f"row {row} exceeds atomic chain maximum")
            append = (*current, row)
            appended_weight = checked_sum((current_weight, weight), "initial_chain_weight")
            within_weight = maximum is None or appended_weight <= maximum
            if current and within_weight:
                direct_profile = numeric_rows_prohibited_profile(task, program, append)
                if current_profile is None:
                    current_profile = numeric_rows_prohibited_profile(task, program, current)
                if direct_profile <= current_profile:
                    current = list(append)
                    current_weight = appended_weight
                    current_profile = direct_profile
                    continue
            if current:
                chains.append(tuple(current))
                chain_ids.append(len(chain_ids))
                periods.append(min(int(task.nodes.source_period[value]) for value in current))
            current = [row]
            current_weight = weight
            current_profile = None
        chains.append(tuple(current))
        chain_ids.append(len(chain_ids))
        periods.append(min(int(task.nodes.source_period[value]) for value in current))
    if not budget.allows_search():
        return interrupted()
    if program.for_kind(NumericRuleKind.DELIVERY):
        order = sorted(range(len(chains)), key=periods.__getitem__)
        chains = [chains[index] for index in order]
        chain_ids = [chain_ids[index] for index in order]
        periods = [periods[index] for index in order]
    rows = [row for chain in chains for row in chain]
    offsets = [0]
    for chain in chains:
        offsets.append(offsets[-1] + len(chain))
    plan = NumericPlan.build(task, rows, offsets, chain_ids, periods)
    evaluation = evaluate_numeric_plan(task, program, quality, plan)
    if not budget.allows_search():
        return interrupted()
    identity = fingerprint(
        {
            "graph": graph.fingerprint,
            "cover": cover.fingerprint,
            "plan": plan.fingerprint,
            "quality": tuple(int(value) for value in evaluation.quality_key),
        }
    )
    return NumericInitialSolution(
        graph.fingerprint,
        cover.fingerprint,
        plan,
        evaluation,
        None,
        perf_counter() - started,
        identity,
    )
