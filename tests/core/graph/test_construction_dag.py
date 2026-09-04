"""Complete forward graphs, direction policy and explicit interruption results."""

from dataclasses import FrozenInstanceError, replace

import pytest

from apsgo_scheduler.core.compatibility import ConstructionDAG, build_construction_dag
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.model import MaterialRole
from tests.core.graph.test_construction_order import build, node, rules, setup_graph


def test_full_graph_keeps_all_supported_roles_and_consumes_no_candidates():
    nodes = [node("a"), replace(node("b"), material_role=MaterialRole.ACTUAL_TRANSITION), node("c")]
    problem, cache, budget = setup_graph(nodes)
    graph = build_construction_dag(problem, cache, budget, seed=7)
    assert graph.complete and graph.stop_reason is None
    assert set(graph.ordered_node_ids) == {"a", "b", "c"}
    assert graph.checked_edge_count == graph.allowed_edge_count == 3
    assert graph.direction_rejected_edge_count == 0
    assert graph.cache_hit_count == cache.hit_count
    assert budget.candidate_check_count == 0 and budget.stop_reason is None
    assert graph.fingerprint == fingerprint(
        {"ordered_node_ids": graph.ordered_node_ids, "adjacency": graph.adjacency}
    )


def test_singleton_complete_graph_has_no_edges():
    graph = build([node("only")])
    assert graph.complete and graph.fingerprint
    assert graph.adjacency == {"only": ()}
    assert graph.checked_edge_count == graph.allowed_edge_count == 0


def test_no_rule_edges_and_all_forward_edges_are_distinct_cases():
    from apsgo_scheduler.core.contracts import RuleScope
    from apsgo_scheduler.core.rules.concrete import SoftHardConnectionRule

    deny = SoftHardConnectionRule(
        "soft",
        "soft",
        RuleScope.EDGE,
        True,
        "1",
        {
            "virtual_sphc_allows_bridge": False,
            "transition_material_breaks_soft_hard": False,
            "missing_grade_policy": "deny",
        },
    )
    ruleset = replace(rules(), rules=(deny,), fingerprint="deny")
    nodes = [node(name) for name in "abc"]
    graph = build(nodes, ruleset)
    assert graph.checked_edge_count == 3 and graph.allowed_edge_count == 0
    assert all(not targets for targets in graph.adjacency.values())
    assert graph.complete


def test_direction_policy_is_separate_from_allowed_rule_cache():
    # Same projected sort width cannot rise beyond epsilon; invoke the public policy
    # directly as well as checking graph counters on the attainable forward graph.
    from apsgo_scheduler.core.compatibility import construction_direction_allowed

    left, right = node("left", width="1000"), node("right", width="1010")
    problem, cache, budget = setup_graph([left, right], rules(maximum_increase="20"))
    assert cache.allows(left, right)
    assert not construction_direction_allowed(left, right)
    assert cache.allows(left, right)
    graph = build_construction_dag(problem, cache, budget, seed=7)
    assert graph.ordered_node_ids == ("right", "left")
    assert graph.adjacency == {"right": ("left",), "left": ()}
    assert graph.direction_rejected_edge_count == 0


@pytest.mark.parametrize(
    "width,allowed", (("1000.0000000005", True), ("1000.000000002", False), (None, True))
)
def test_direction_rule_uses_reference_addition_epsilon_and_nulls(width, allowed):
    from apsgo_scheduler.core.compatibility import construction_direction_allowed

    assert construction_direction_allowed(node("a"), node("b", width=width)) is allowed


def test_cancel_before_start_returns_no_completed_graph_or_fingerprint():
    class Cancelled:
        def is_cancelled(self):
            return True

    problem, cache, budget = setup_graph([node("a"), node("b")], cancellation=Cancelled())
    graph = build_construction_dag(problem, cache, budget, seed=7)
    assert not graph.complete and graph.fingerprint is None
    assert graph.stop_reason is SearchStopReason.USER_CANCELLED
    assert graph.checked_edge_count == cache.miss_count == 0
    assert budget.candidate_check_count == 0


def test_mid_graph_cancel_preserves_only_completed_work_and_never_natural_completion():
    class CancelAfterEdges:
        cache = None

        def is_cancelled(self):
            return self.cache is not None and self.cache.hit_count + self.cache.miss_count >= 3

    token = CancelAfterEdges()
    problem, cache, budget = setup_graph([node(name) for name in "abcdef"], cancellation=token)
    token.cache = cache
    graph = build_construction_dag(problem, cache, budget, seed=7)
    assert not graph.complete and graph.fingerprint is None
    assert graph.stop_reason is SearchStopReason.USER_CANCELLED
    assert graph.checked_edge_count == 3
    assert graph.allowed_edge_count == sum(map(len, graph.adjacency.values()))
    assert budget.candidate_check_count == 0


def test_search_deadline_before_graph_stops_without_consuming_candidate_budget():
    problem, cache, budget = setup_graph([node("a")], clock=lambda: 100)
    graph = build_construction_dag(problem, cache, budget, seed=7)
    assert not graph.complete and graph.fingerprint is None
    assert graph.stop_reason is SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    assert graph.checked_edge_count == 0


def test_cancellation_at_last_edge_is_observed_before_graph_can_be_signed():
    class LastEdge:
        cache = None

        def is_cancelled(self):
            return self.cache is not None and self.cache.hit_count + self.cache.miss_count >= 1

    token = LastEdge()
    problem, cache, budget = setup_graph([node("a"), node("b")], cancellation=token)
    token.cache = cache
    graph = build_construction_dag(problem, cache, budget, seed=7)
    assert graph.checked_edge_count == graph.allowed_edge_count == 1
    assert not graph.complete and graph.fingerprint is None


def test_graph_mappings_are_detached_and_immutable():
    graph = build([node("a"), node("b")])
    with pytest.raises(FrozenInstanceError):
        graph.checked_edge_count = 0
    with pytest.raises(TypeError):
        graph.adjacency["a"] = ()
    supplied = {key: list(value) for key, value in graph.adjacency.items()}
    copied = replace(graph, adjacency=supplied)
    supplied.clear()
    assert copied.adjacency == graph.adjacency


@pytest.mark.parametrize(
    "changes",
    (
        {"ordered_node_ids": ("a", "a")},
        {"adjacency": {"a": ("a",), "b": ()}},
        {"adjacency": {"a": (), "b": ("a",)}},
        {"adjacency": {"a": ("b", "b"), "b": ()}},
        {"adjacency": {"a": ("unknown",), "b": ()}},
        {"allowed_edge_count": 9},
        {"checked_edge_count": -1},
        {"elapsed_seconds": float("nan")},
        {"stop_reason": "cancelled"},
    ),
)
def test_invalid_graph_contract_fails(changes):
    values = dict(
        ordered_node_ids=("a", "b"),
        adjacency={"a": ("b",), "b": ()},
        checked_edge_count=1,
        allowed_edge_count=1,
        cache_hit_count=0,
        direction_rejected_edge_count=0,
        elapsed_seconds=0.0,
        stop_reason=None,
    )
    with pytest.raises(ValueError):
        ConstructionDAG(**(values | changes))


def test_cache_from_other_problem_is_rejected():
    problem, cache, budget = setup_graph([node("a")])
    with pytest.raises(ValueError):
        build_construction_dag(replace(problem, problem_id="another"), cache, budget, seed=7)


def test_gqga4_graph_matches_frozen_reference_order_and_every_successor():
    import json

    from apsgo_scheduler.app.rule_set_loader import load_rule_set
    from apsgo_scheduler.core.budget import SolveRuntimeBudget
    from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
    from apsgo_scheduler.core.rules.base import RuleEvaluationContext
    from tests.app.test_input_normalizer import BASE, gqga4_request, gqga4_spec, normalize

    spec = gqga4_spec.__wrapped__()
    problem = normalize(gqga4_request.__wrapped__(spec))
    context = RuleEvaluationContext(
        problem.period_order,
        dict(zip(problem.period_order, range(len(problem.period_order)))),
        tuple(p.prototype_id for p in problem.virtual_prototypes),
    )
    cache = RuleEdgeDecisionCache(problem, load_rule_set(spec), context)
    budget = SolveRuntimeBudget(0, 100, 110, 0, 0, None, clock=lambda: 0)
    graph = build_construction_dag(problem, cache, budget, seed=590531)
    expected = json.loads((BASE / "reference_stage_expectations.json").read_text())["path_cover"]
    assert graph.complete and budget.candidate_check_count == 0
    assert graph.ordered_node_ids == tuple(expected["ordered_node_ids"])
    assert graph.adjacency == {
        row["node_id"]: tuple(row["successor_node_ids"]) for row in expected["adjacency"]
    }
    assert graph.fingerprint == "7c5b6b5b50a1df725c91569527f9fcf96536f0f737bfbf32b4e1352c8f80a7ad"
    assert (graph.checked_edge_count, graph.allowed_edge_count) == (140715, 32536)
