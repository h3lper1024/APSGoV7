"""Maximum matching verified independently of the production augmenting algorithm."""

from itertools import combinations, product
from sys import getrecursionlimit

import pytest

from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.compatibility import ConstructionDAG
from apsgo_scheduler.core.contracts import SearchStopReason
from apsgo_scheduler.core.path_cover import minimum_path_cover


def make_dag(ordered_node_ids, adjacency, **changes):
    """Build a graph value directly, without running rules or construction."""
    ids = tuple(ordered_node_ids)
    neighbors = {key: tuple(targets) for key, targets in adjacency.items()}
    for node in ids:
        neighbors.setdefault(node, ())
    values = dict(
        ordered_node_ids=ids,
        adjacency=neighbors,
        checked_edge_count=len(ids) * (len(ids) - 1) // 2,
        allowed_edge_count=sum(map(len, neighbors.values())),
        cache_hit_count=0,
        direction_rejected_edge_count=0,
        elapsed_seconds=0.0,
        stop_reason=None,
    )
    return ConstructionDAG(**(values | changes))


def budget(**changes):
    values = dict(
        started_at_monotonic=0.0,
        search_deadline_monotonic=100.0,
        final_deadline_monotonic=110.0,
        candidate_check_limit=0,
        candidate_check_count=0,
        cancellation=None,
        clock=lambda: 1.0,
    )
    return SolveRuntimeBudget(**(values | changes))


def brute_force_matching_size(dag):
    """Tiny independent oracle: choose zero or one successor per left node."""
    best = 0
    choices = ((None,) + dag.adjacency[node] for node in dag.ordered_node_ids)
    for assignment in product(*choices):
        targets = tuple(right for right in assignment if right is not None)
        if len(set(targets)) == len(targets):
            best = max(best, len(targets))
    return best


def assert_valid_matching(dag, result):
    assert result.complete and result.stop_reason is None
    edges = result.matching_edges
    assert len({left for left, _ in edges}) == len(edges)
    assert len({right for _, right in edges}) == len(edges)
    assert all(right in dag.adjacency[left] for left, right in edges)
    selected = {left for left, _ in edges}
    assert tuple(left for left, _ in edges) == tuple(
        node for node in dag.ordered_node_ids if node in selected
    )
    assert result.matched_edge_count == len(edges)
    assert result.path_count == len(dag.ordered_node_ids) - len(edges)


@pytest.mark.parametrize(
    "ids,adjacency,expected",
    (
        ((), {}, ()),
        (("only",), {}, ()),
        (("z", "a", "m"), {}, ()),
        (("z", "a", "m"), {"z": ("a",), "a": ("m",)}, (("z", "a"), ("a", "m"))),
        (
            ("z", "a", "u", "b", "r"),
            {"z": ("a",), "u": ("b",)},
            (("z", "a"), ("u", "b")),
        ),
        (
            ("z", "a", "m", "b"),
            {"z": ("m", "b"), "a": ("m", "b")},
            (("z", "m"), ("a", "b")),
        ),
        (
            ("z", "a", "m", "b"),
            {"z": ("b", "m"), "a": ("m", "b")},
            (("z", "b"), ("a", "m")),
        ),
    ),
)
def test_fixed_matching_goldens_follow_node_and_neighbor_order(ids, adjacency, expected):
    dag = make_dag(ids, adjacency)
    runtime = budget()
    result = minimum_path_cover(dag, runtime)
    assert_valid_matching(dag, result)
    assert result.matching_edges == expected
    assert runtime.candidate_check_count == 0 and runtime.stop_reason is None


def test_augmenting_path_rewires_earlier_choices_instead_of_stopping_at_a_maximal_matching():
    dag = make_dag(
        ("start", "middle-a", "middle-b", "end"),
        {
            "start": ("middle-b", "middle-a"),
            "middle-a": ("end", "middle-b"),
            "middle-b": ("end",),
        },
    )
    result = minimum_path_cover(dag, budget())
    assert result.matching_edges == (
        ("start", "middle-a"),
        ("middle-a", "middle-b"),
        ("middle-b", "end"),
    )
    assert_valid_matching(dag, result)
    assert result.matched_edge_count == brute_force_matching_size(dag) == 3


def test_all_64_four_node_forward_graphs_match_the_independent_exhaustive_optimum():
    nodes = ("z", "a", "m", "b")
    possible = tuple(combinations(nodes, 2))
    cases = 0
    for enabled in product((False, True), repeat=len(possible)):
        adjacency = {node: [] for node in nodes}
        for (left, right), present in zip(possible, enabled):
            if present:
                adjacency[left].append(right)
        dag = make_dag(nodes, adjacency)
        runtime = budget()
        result = minimum_path_cover(dag, runtime)
        assert_valid_matching(dag, result)
        assert result.matched_edge_count == brute_force_matching_size(dag), adjacency
        assert runtime.candidate_check_count == 0
        cases += 1
    assert cases == 64


def test_complete_forward_graph_matches_every_node_except_the_final_left_copy():
    nodes = tuple(f"node-{index}" for index in range(8))
    dag = make_dag(
        nodes, {node: tuple(reversed(nodes[index + 1 :])) for index, node in enumerate(nodes)}
    )
    result = minimum_path_cover(dag, budget())
    assert_valid_matching(dag, result)
    assert result.matching_edges == tuple(zip(nodes, nodes[1:]))


def test_repeated_matching_is_deterministic_without_mutating_the_frozen_graph():
    adjacency = {"end": [], "second": ["end"], "first": ["end", "second"]}
    dag = make_dag(("first", "second", "end"), adjacency)
    before = tuple(dag.adjacency.items()), dag.fingerprint
    first = minimum_path_cover(dag, budget())
    adjacency["first"].clear()
    second = minimum_path_cover(dag, budget())
    assert tuple(dag.adjacency.items()) == before[0] and dag.fingerprint == before[1]
    assert first.matching_edges == second.matching_edges
    assert first.matching_fingerprint == second.matching_fingerprint
    assert first.paths == second.paths and first.path_fingerprint == second.path_fingerprint
    assert first.graph_fingerprint == dag.fingerprint


def test_1100_node_deep_augmentation_does_not_use_or_raise_the_recursion_limit():
    nodes = tuple(f"node-{index}" for index in range(1100))
    adjacency = {
        nodes[index]: (nodes[index + 2], nodes[index + 1]) for index in range(len(nodes) - 2)
    }
    adjacency[nodes[-2]] = (nodes[-1],)
    dag = make_dag(nodes, adjacency)
    recursion_limit = getrecursionlimit()
    runtime = budget()
    result = minimum_path_cover(dag, runtime)
    assert getrecursionlimit() == recursion_limit
    assert_valid_matching(dag, result)
    assert result.matching_edges == tuple(zip(nodes, nodes[1:]))
    assert result.matched_edge_count == 1099
    assert runtime.candidate_check_count == 0 and runtime.stop_reason is None


@pytest.mark.parametrize("invalid", ("graph_type", "partial_graph", "budget_type"))
def test_matching_requires_a_complete_graph_and_the_shared_runtime_budget(invalid):
    dag = make_dag(("a", "b"), {"a": ("b",)})
    runtime = budget()
    if invalid == "graph_type":
        dag = None
    elif invalid == "partial_graph":
        dag = make_dag(("a", "b"), {"a": ("b",)}, stop_reason=SearchStopReason.USER_CANCELLED)
    else:
        runtime = None
    with pytest.raises(ValueError):
        minimum_path_cover(dag, runtime)
