"""Deterministic numeric construction graph, path cover and initial plan."""

from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from random import Random
from time import perf_counter

import numpy as np

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
    ordered = list(range(count))
    adjacency = [[] for _ in ordered]
    checked = allowed = direction_rejected = 0

    def freeze_result():
        offsets = [0]
        flattened = []
        for targets in adjacency:
            flattened.extend(targets)
            offsets.append(len(flattened))
        arrays = (
            ("ordered_rows", readonly(ordered, np.int64)),
            ("adjacency_offsets", readonly(offsets, np.int64)),
            ("adjacency_rows", readonly(flattened, np.int64)),
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
            checked,
            allowed,
            direction_rejected,
            budget.stop_reason,
            perf_counter() - started,
            identity,
        )

    if not budget.allows_search():
        return freeze_result()

    def priority(row):
        return tuple(int(value) for value in task.priority[:, row])

    def main_key(row):
        width_present = bool(task.nodes.present[row, 0])
        thickness_present = bool(task.nodes.present[row, 1])
        temperature_present = bool(task.nodes.present[row, 2])
        return (
            not width_present,
            -int(task.nodes.width[row]) if width_present else 0,
            priority(row),
            not thickness_present,
            int(task.nodes.thickness[row]) if thickness_present else 0,
            not temperature_present,
            int(task.nodes.min_temperature[row]) if temperature_present else 0,
        )

    rng = Random(seed)
    rng.shuffle(ordered)
    ordered.sort(key=main_key)
    for left_position, left in enumerate(ordered):
        if not budget.allows_search():
            return freeze_result()
        targets = adjacency[left_position]
        for right in ordered[left_position + 1 :]:
            if not budget.allows_search():
                return freeze_result()
            checked += 1
            if not numeric_edge_allowed(task, program, left, right):
                continue
            if (
                task.nodes.present[left, 0]
                and task.nodes.present[right, 0]
                and int(task.nodes.width[right]) > int(task.nodes.width[left])
            ):
                direction_rejected += 1
                continue
            targets.append(right)
            allowed += 1
        rng.shuffle(targets)
        left_thickness = (
            int(task.nodes.thickness[left]) if task.nodes.present[left, 1] else 0
        )

        def successor_key(row):
            thickness = int(task.nodes.thickness[row]) if task.nodes.present[row, 1] else 0
            return abs(left_thickness - thickness), -int(task.nodes.weight[row]), priority(row)

        targets.sort(key=successor_key)
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
    infinity = count + 1
    positions = np.empty(count, dtype=np.int64)
    positions[graph.ordered_rows] = np.arange(count, dtype=np.int64)

    def neighbors(left):
        position = int(positions[left])
        return graph.adjacency_rows[
            int(graph.adjacency_offsets[position]) : int(graph.adjacency_offsets[position + 1])
        ]

    def freeze_result(paths=()):
        if budget.stop_reason is None:
            flattened = [row for path in paths for row in path]
            offsets = [0]
            for path in paths:
                offsets.append(offsets[-1] + len(path))
        else:
            flattened, offsets = [], []
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

    def bfs():
        queue = deque()
        found = False
        for left_value in graph.ordered_rows:
            if not budget.allows_search():
                return False
            left = int(left_value)
            if match_left[left] < 0:
                distance[left] = 0
                queue.append(left)
            else:
                distance[left] = infinity
        while queue:
            if not budget.allows_search():
                return False
            left = queue.popleft()
            for right_value in neighbors(left):
                if not budget.allows_search():
                    return False
                next_left = int(match_right[int(right_value)])
                if next_left < 0:
                    found = True
                elif distance[next_left] == infinity:
                    distance[next_left] = distance[left] + 1
                    queue.append(next_left)
        return found

    def dfs(start):
        stack = [(start, iter(neighbors(start)), None)]
        while stack:
            if not budget.allows_search():
                return False
            left, iterator, _ = stack[-1]
            right_value = next(iterator, None)
            if right_value is None:
                distance[left] = infinity
                stack.pop()
                continue
            right = int(right_value)
            next_left = int(match_right[right])
            if next_left < 0:
                for parent, _, incoming_right in reversed(stack):
                    match_left[parent] = right
                    match_right[right] = parent
                    right = incoming_right
                return True
            if distance[next_left] == distance[left] + 1:
                stack.append((next_left, iter(neighbors(next_left)), right))
        return False

    if not budget.allows_search():
        return freeze_result()
    while bfs():
        for left_value in graph.ordered_rows:
            if not budget.allows_search():
                return freeze_result()
            left = int(left_value)
            if match_left[left] < 0:
                dfs(left)
                if budget.must_stop:
                    return freeze_result()
    if budget.must_stop:
        return freeze_result()

    paths = []
    visited = set()
    for start_value in graph.ordered_rows:
        if not budget.allows_search():
            return freeze_result()
        start = int(start_value)
        if match_right[start] >= 0:
            continue
        path = []
        cursor = start
        while cursor >= 0:
            if not budget.allows_search():
                return freeze_result()
            if cursor in visited:
                raise NumericValueError("path_cover", "matching repeats a graph row")
            visited.add(cursor)
            path.append(cursor)
            cursor = int(match_left[cursor])
        paths.append(tuple(path))
    if len(visited) != count or len(paths) != count - int(np.count_nonzero(match_left >= 0)):
        raise NumericValueError("path_cover", "matching does not cover every graph row")
    if not budget.allows_search():
        return freeze_result()
    result = freeze_result(paths)
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
