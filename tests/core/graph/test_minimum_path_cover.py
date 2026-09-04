"""Path restoration, interruption boundaries and the frozen reference graph golden."""

import json
import sys
from dataclasses import FrozenInstanceError, replace
from itertools import pairwise
from pathlib import Path

import pytest

from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.path_cover import PathCoverResult, minimum_path_cover
from tests.core.graph.test_bipartite_matching import budget, make_dag


def assert_complete_cover(dag, result):
    flattened = tuple(node for path in result.paths for node in path)
    assert result.complete and result.stop_reason is None
    assert result.graph_fingerprint == dag.fingerprint
    assert len(flattened) == len(dag.ordered_node_ids)
    assert set(flattened) == set(dag.ordered_node_ids)
    assert result.path_count == len(dag.ordered_node_ids) - result.matched_edge_count
    assert result.maximum_path_length == max(map(len, result.paths), default=0)
    assert set(result.matching_edges) == {edge for path in result.paths for edge in pairwise(path)}
    assert all(right in dag.adjacency[left] for left, right in result.matching_edges)
    roots = {path[0] for path in result.paths}
    assert tuple(path[0] for path in result.paths) == tuple(
        node for node in dag.ordered_node_ids if node in roots
    )
    assert result.matching_fingerprint == fingerprint(
        {"graph_fingerprint": dag.fingerprint, "matching_edges": result.matching_edges}
    )
    assert result.path_fingerprint == fingerprint(
        {"graph_fingerprint": dag.fingerprint, "paths": result.paths}
    )


@pytest.mark.parametrize(
    "ids,adjacency,paths",
    (
        ((), {}, ()),
        (("only",), {}, (("only",),)),
        (("z", "a", "m"), {}, (("z",), ("a",), ("m",))),
        (
            ("z", "a", "m"),
            {"z": ("a",), "a": ("m",)},
            (("z", "a", "m"),),
        ),
        (
            ("z", "a", "u", "b", "r"),
            {"z": ("a",), "u": ("b",)},
            (("z", "a"), ("u", "b"), ("r",)),
        ),
        (
            ("z", "a", "m", "b"),
            {"z": ("m", "b"), "a": ("m", "b")},
            (("z", "m"), ("a", "b")),
        ),
    ),
)
def test_frozen_order_restores_each_input_node_exactly_once(ids, adjacency, paths):
    dag = make_dag(ids, adjacency)
    runtime = budget()
    result = minimum_path_cover(dag, runtime)
    assert_complete_cover(dag, result)
    assert result.paths == paths
    # This stage only probes time/cancellation, even when no candidate checks remain.
    assert runtime.candidate_check_limit == runtime.candidate_check_count == 0
    assert runtime.stop_reason is None and not runtime.must_stop


def test_path_restoration_follows_final_augmenting_matching_not_first_greedy_choices():
    dag = make_dag(
        ("start", "middle-a", "middle-b", "end"),
        {
            "start": ("middle-b", "middle-a"),
            "middle-a": ("end", "middle-b"),
            "middle-b": ("end",),
        },
    )
    result = minimum_path_cover(dag, budget())
    assert_complete_cover(dag, result)
    assert result.paths == (("start", "middle-a", "middle-b", "end"),)


def assert_interrupted(dag, result, runtime, reason):
    assert not result.complete and result.stop_reason is reason
    assert runtime.stop_reason is reason and runtime.must_stop
    assert result.graph_fingerprint == dag.fingerprint
    assert result.paths == () and result.path_count == result.maximum_path_length == 0
    assert result.matching_fingerprint is result.path_fingerprint is None
    assert len({left for left, _ in result.matching_edges}) == result.matched_edge_count
    assert len({right for _, right in result.matching_edges}) == result.matched_edge_count
    assert all(right in dag.adjacency[left] for left, right in result.matching_edges)
    assert runtime.candidate_check_count == 0


class PhaseStop:
    """Observe existing safe points without adding production phase hooks or sleeping."""

    def __init__(self, phase):
        self.phase = phase
        self.observed = 0

    def reached(self):
        frame = sys._getframe()
        try:
            while frame is not None:
                if frame.f_globals.get("__name__") == "apsgo_scheduler.core.path_cover":
                    name, values = frame.f_code.co_name, frame.f_locals
                    if self.phase in ("bfs", "dfs") and name == self.phase:
                        self.observed += 1
                        return self.observed >= 2
                    if self.phase == "path" and name == "minimum_path_cover":
                        if values.get("path") and values.get("cursor") is not None:
                            self.observed += 1
                            return True
                    if self.phase == "signed" and name == "minimum_path_cover":
                        if isinstance(values.get("value"), PathCoverResult):
                            self.observed += 1
                            return True
                frame = frame.f_back
            return False
        finally:
            del frame

    def is_cancelled(self):
        return self.reached()

    def clock(self):
        return 100.0 if self.reached() else 1.0


@pytest.mark.parametrize("phase", ("bfs", "dfs", "path", "signed"))
@pytest.mark.parametrize("stop_kind", ("cancel", "time"))
def test_interruption_during_each_phase_never_exposes_paths_or_completion_fingerprints(
    phase, stop_kind
):
    ids = tuple("abcde")
    dag = make_dag(ids, {left: (right,) for left, right in pairwise(ids)})
    observer = PhaseStop(phase)
    if stop_kind == "cancel":
        runtime = budget(cancellation=observer)
        reason = SearchStopReason.USER_CANCELLED
    else:
        runtime = budget(clock=observer.clock)
        reason = SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    result = minimum_path_cover(dag, runtime)
    assert observer.observed > 0
    assert_interrupted(dag, result, runtime, reason)
    if phase in ("path", "signed"):
        # Even a finished matching cannot be published as a cover after interruption.
        assert result.matching_edges == tuple(pairwise(ids))


@pytest.mark.parametrize("stop_kind", ("cancel", "time", "existing_stop"))
def test_interruption_before_start_keeps_empty_partial_matching(stop_kind):
    class Cancelled:
        def is_cancelled(self):
            return True

    dag = make_dag(("a", "b"), {"a": ("b",)})
    if stop_kind == "cancel":
        runtime = budget(cancellation=Cancelled())
        reason = SearchStopReason.USER_CANCELLED
    elif stop_kind == "time":
        runtime = budget(clock=lambda: 100.0)
        reason = SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    else:
        reason = SearchStopReason.CANDIDATE_LIMIT_REACHED
        runtime = budget(stop_reason=reason)
    result = minimum_path_cover(dag, runtime)
    assert_interrupted(dag, result, runtime, reason)
    assert result.matching_edges == ()


def test_cancellation_never_observes_half_rewritten_multi_level_augmentation():
    class CancelAfterRewire:
        observations = 0

        def is_cancelled(self):
            frame = sys._getframe()
            try:
                while frame is not None:
                    if frame.f_code is minimum_path_cover.__code__:
                        left = frame.f_locals["match_left"]
                        right = frame.f_locals["match_right"]
                        assert all(right[target] == node for node, target in left.items() if target)
                        assert all(left[node] == target for target, node in right.items() if node)
                        self.observations += 1
                        return left["start"] == "middle-a"
                    frame = frame.f_back
                return False
            finally:
                del frame

    dag = make_dag(
        ("start", "middle-a", "middle-b", "end"),
        {
            "start": ("middle-b", "middle-a"),
            "middle-a": ("end", "middle-b"),
            "middle-b": ("end",),
        },
    )
    token = CancelAfterRewire()
    runtime = budget(cancellation=token)
    result = minimum_path_cover(dag, runtime)
    assert token.observations > 0
    assert_interrupted(dag, result, runtime, SearchStopReason.USER_CANCELLED)
    assert result.matching_edges == tuple(pairwise(dag.ordered_node_ids))


def test_partial_graph_without_fingerprint_is_not_treated_as_complete_input():
    partial = make_dag(("a", "b"), {"a": ("b",)}, stop_reason=SearchStopReason.USER_CANCELLED)
    runtime = budget()
    assert partial.fingerprint is None
    with pytest.raises(ValueError, match="complete ConstructionDAG"):
        minimum_path_cover(partial, runtime)
    assert runtime.stop_reason is None and runtime.candidate_check_count == 0


def test_result_detaches_mutable_inputs_and_elapsed_time_does_not_change_identity():
    edges, paths = [["a", "b"]], [["a", "b"], ["c"]]
    result = PathCoverResult("graph", edges, paths, None, 0.0)
    edges[0].append("changed")
    paths[0].clear()
    paths.append(["another"])
    assert result.matching_edges == (("a", "b"),)
    assert result.paths == (("a", "b"), ("c",))
    with pytest.raises(FrozenInstanceError):
        result.paths = ()
    with pytest.raises(TypeError):
        result.paths[0][0] = "changed"
    later = replace(result, elapsed_seconds=99.0)
    assert later.matching_fingerprint == result.matching_fingerprint
    assert later.path_fingerprint == result.path_fingerprint
    different_graph = replace(result, graph_fingerprint="another-graph")
    assert different_graph.matching_fingerprint != result.matching_fingerprint
    assert different_graph.path_fingerprint != result.path_fingerprint


@pytest.mark.parametrize(
    "changes",
    (
        {"graph_fingerprint": " "},
        {"matching_edges": (("a",),)},
        {"matching_edges": (("a", "a"),)},
        {"matching_edges": (("a", "b"), ("a", "c"))},
        {"matching_edges": (("a", "b"), ("c", "b"))},
        {"matching_edges": (("", "b"),)},
        {"matching_edges": "a-b"},
        {"paths": ((),)},
        {"paths": (("a", "b"), ("a",))},
        {"paths": (("a", "b"), (" ",))},
        {"paths": (("a", "c", "b"),)},
        {"paths": ("ab",)},
        {"stop_reason": "user_cancelled"},
        {"stop_reason": SearchStopReason.USER_CANCELLED},
        {"elapsed_seconds": -1.0},
        {"elapsed_seconds": float("nan")},
        {"elapsed_seconds": float("inf")},
        {"elapsed_seconds": True},
    ),
)
def test_result_rejects_incoherent_matching_paths_or_metadata(changes):
    result = PathCoverResult("graph", (("a", "b"),), (("a", "b"), ("c",)), None, 0.0)
    with pytest.raises(ValueError):
        replace(result, **changes)


def test_incomplete_result_may_keep_valid_matching_but_never_exposes_complete_fingerprints():
    result = PathCoverResult("graph", (("a", "b"),), (), SearchStopReason.USER_CANCELLED, 0.0)
    assert not result.complete
    assert result.matched_edge_count == 1 and result.path_count == 0
    assert result.matching_fingerprint is result.path_fingerprint is None


def test_gqga4_frozen_reference_graph_restores_identical_matching_and_all_path_sequences():
    # Function 9 separately verifies graph construction; here the graph is already fixed.
    # The frozen source identity is the original solver.py, not a target-solver result.
    source = Path(__file__).parents[2] / "baselines/gqga4/reference_stage_expectations.json"
    frozen = json.loads(source.read_text(encoding="utf-8"))
    assert frozen["script_sha256"] == (
        "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
    )
    expected = frozen["path_cover"]
    ids = tuple(expected["ordered_node_ids"])
    adjacency = {row["node_id"]: tuple(row["successor_node_ids"]) for row in expected["adjacency"]}
    dag = make_dag(ids, adjacency)
    runtime = budget()
    result = minimum_path_cover(dag, runtime)
    expected_edges = tuple(
        (left, expected["match_left"][left])
        for left in ids
        if expected["match_left"][left] is not None
    )
    expected_paths = tuple(tuple(path) for path in expected["paths"])
    assert_complete_cover(dag, result)
    assert len(ids) == 531 and dag.allowed_edge_count == 32536
    assert result.matched_edge_count == 514
    assert result.path_count == 17 and result.maximum_path_length == 61
    assert result.matching_edges == expected_edges
    assert {right: left for left, right in result.matching_edges} == {
        right: left for right, left in expected["match_right"].items() if left is not None
    }
    assert result.paths == expected_paths
    assert result.matching_fingerprint == fingerprint(
        {"graph_fingerprint": dag.fingerprint, "matching_edges": expected_edges}
    )
    assert result.path_fingerprint == fingerprint(
        {"graph_fingerprint": dag.fingerprint, "paths": expected_paths}
    )
    assert runtime.candidate_check_count == 0 and runtime.stop_reason is None
