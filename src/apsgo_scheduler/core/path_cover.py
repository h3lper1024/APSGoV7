"""Deterministic maximum bipartite matching and minimum path cover of a DAG."""

from collections import deque
from dataclasses import dataclass, field, replace
from itertools import pairwise
from math import isfinite
from time import perf_counter

from .budget import SolveRuntimeBudget
from .compatibility import ConstructionDAG
from .contracts import SearchStopReason, fingerprint, freeze_tuple, require_enum, require_text


@dataclass(frozen=True, slots=True)
class PathCoverResult:
    graph_fingerprint: str
    matching_edges: tuple[tuple[str, str], ...]
    paths: tuple[tuple[str, ...], ...]
    stop_reason: SearchStopReason | None
    elapsed_seconds: float
    matching_fingerprint: str | None = field(init=False)
    path_fingerprint: str | None = field(init=False)

    def __post_init__(self):
        require_text(self.graph_fingerprint, "graph_fingerprint")
        edges = tuple(
            freeze_tuple(edge, str, "matching edge")
            for edge in freeze_tuple(self.matching_edges, (tuple, list), "matching_edges")
        )
        if any(len(edge) != 2 or edge[0] == edge[1] for edge in edges):
            raise ValueError("matching edges must have two distinct endpoints")
        for edge in edges:
            for node_id in edge:
                require_text(node_id, "matching node_id")
        if len({left for left, _ in edges}) != len(edges) or len(
            {right for _, right in edges}
        ) != len(edges):
            raise ValueError("matching endpoints must be unique on each side")
        paths = tuple(
            freeze_tuple(path, str, "path")
            for path in freeze_tuple(self.paths, (tuple, list), "paths")
        )
        if any(not path for path in paths):
            raise ValueError("paths must be nonempty")
        nodes = [node_id for path in paths for node_id in path]
        for node_id in nodes:
            require_text(node_id, "path node_id")
        if len(set(nodes)) != len(nodes):
            raise ValueError("each path node must occur exactly once")
        if self.stop_reason is not None:
            require_enum(self.stop_reason, SearchStopReason, "stop_reason")
            if paths:
                raise ValueError("unfinished path cover must not expose paths")
        elif set(edges) != {edge for path in paths for edge in pairwise(path)}:
            raise ValueError("completed paths must exactly restore the matching edges")
        if (
            type(self.elapsed_seconds) not in (int, float)
            or not isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("elapsed_seconds must be finite and nonnegative")
        object.__setattr__(self, "matching_edges", edges)
        object.__setattr__(self, "paths", paths)
        for name, key, value in (
            ("matching_fingerprint", "matching_edges", edges),
            ("path_fingerprint", "paths", paths),
        ):
            object.__setattr__(
                self,
                name,
                fingerprint({"graph_fingerprint": self.graph_fingerprint, key: value})
                if self.complete
                else None,
            )

    @property
    def complete(self) -> bool:
        return self.stop_reason is None

    @property
    def matched_edge_count(self) -> int:
        return len(self.matching_edges)

    @property
    def path_count(self) -> int:
        return len(self.paths)

    @property
    def maximum_path_length(self) -> int:
        return max(map(len, self.paths), default=0)


def minimum_path_cover(dag: ConstructionDAG, budget: SolveRuntimeBudget) -> PathCoverResult:
    """Preserve the graph's traversal order; partial matching is never a complete cover."""
    if not isinstance(dag, ConstructionDAG) or not dag.complete:
        raise ValueError("minimum path cover requires a complete ConstructionDAG")
    if not isinstance(budget, SolveRuntimeBudget):
        raise ValueError("minimum path cover requires the shared runtime budget")
    started = perf_counter()
    nodes = dag.ordered_node_ids
    match_left = dict.fromkeys(nodes)
    match_right = dict.fromkeys(nodes)
    distance = {}
    infinity = len(nodes) + 1

    def result(paths=()):
        value = PathCoverResult(
            dag.fingerprint,
            tuple((left, match_left[left]) for left in nodes if match_left[left] is not None),
            () if budget.must_stop else tuple(paths),
            budget.stop_reason,
            perf_counter() - started,
        )
        # Include result freezing and fingerprints in the measured stage time.
        object.__setattr__(value, "elapsed_seconds", perf_counter() - started)
        return value

    def bfs():
        queue = deque()
        found = False
        for left in nodes:
            if not budget.allows_search():
                return False
            if match_left[left] is None:
                distance[left] = 0
                queue.append(left)
            else:
                distance[left] = infinity
        while queue:
            if not budget.allows_search():
                return False
            left = queue.popleft()
            for right in dag.adjacency[left]:
                if not budget.allows_search():
                    return False
                next_left = match_right[right]
                if next_left is None:
                    # Keep scanning all layers, exactly as the reference does.
                    found = True
                elif distance[next_left] == infinity:
                    distance[next_left] = distance[left] + 1
                    queue.append(next_left)
        return found

    def dfs(start):
        # Each frame holds the suspended recursive call and its parent's chosen edge.
        stack = [(start, iter(dag.adjacency[start]), None)]
        while stack:
            if not budget.allows_search():
                return False
            left, neighbors, _ = stack[-1]
            right = next(neighbors, None)
            if right is None:
                distance[left] = infinity
                stack.pop()
                continue
            next_left = match_right[right]
            if next_left is None:
                # Back-write the whole augmenting path before the next cancellation point.
                for parent, _, incoming_right in reversed(stack):
                    match_left[parent] = right
                    match_right[right] = parent
                    right = incoming_right
                return True
            if distance.get(next_left, infinity) == distance[left] + 1:
                stack.append((next_left, iter(dag.adjacency[next_left]), right))
        return False

    if not budget.allows_search():
        return result()
    while bfs():
        for left in nodes:
            if not budget.allows_search():
                return result()
            if match_left[left] is None:
                dfs(left)
                if budget.must_stop:
                    return result()
    if budget.must_stop:
        return result()

    paths = []
    visited = set()
    for start in nodes:
        if not budget.allows_search():
            return result()
        if match_right[start] is not None:
            continue
        path = []
        cursor = start
        while cursor is not None:
            if not budget.allows_search():
                return result()
            if cursor in visited:
                raise ValueError("matching paths repeat a graph node")
            visited.add(cursor)
            path.append(cursor)
            successor = match_left[cursor]
            if successor is not None and successor not in dag.adjacency[cursor]:
                raise ValueError("matching edge does not exist in the input graph")
            cursor = successor
        paths.append(tuple(path))
    matched_count = sum(right is not None for right in match_left.values())
    if visited != set(nodes) or len(paths) != len(nodes) - matched_count:
        raise ValueError("path cover must contain every graph node exactly once")
    if not budget.allows_search():
        return result()
    value = result(paths)
    if not budget.allows_search():
        return replace(
            value,
            paths=(),
            stop_reason=budget.stop_reason,
            elapsed_seconds=perf_counter() - started,
        )
    return value
