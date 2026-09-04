"""Frozen reference ordering, including shuffle ties and nullable sort fields."""

import random
from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache, build_construction_dag
from apsgo_scheduler.core.contracts import RuleScope, fingerprint
from apsgo_scheduler.core.model import MaterialRole, Node, SchedulingProblem
from apsgo_scheduler.core.rules.base import (
    NumericProjection,
    QualityAggregation,
    QualityCriterion,
    QualityDirection,
    RuleEvaluationContext,
)
from apsgo_scheduler.core.rules.concrete import SyntheticNodePriorityRule, SyntheticWidthLimitRule
from apsgo_scheduler.core.rules.rule_set import PROHIBITED_METRIC_KEYS, ProcessRuleSet


def node(name, *, width="1000", thickness="1", temperature="700", weight="20", priority=0):
    return Node(
        name,
        name,
        name,
        "period",
        Decimal(weight),
        None if width is None else Decimal(width),
        None if thickness is None else Decimal(thickness),
        None if temperature is None else Decimal(temperature),
        Decimal("900"),
        "steel",
        MaterialRole.NORMAL_REAL,
        {"priority": priority},
    )


def rules(*, priority=False, maximum_increase=None):
    items = []
    if priority:
        items.append(
            SyntheticNodePriorityRule(
                "priority", "priority", RuleScope.NODE, True, "1", {"attribute": "priority"}
            )
        )
    if maximum_increase is not None:
        items.append(
            SyntheticWidthLimitRule(
                "width",
                "width",
                RuleScope.EDGE,
                True,
                "1",
                {"maximum_increase": Decimal(maximum_increase)},
            )
        )
    quality = tuple(
        QualityCriterion(
            k,
            k,
            QualityDirection.MINIMIZE,
            QualityAggregation.NAMED_VALUE,
            NumericProjection.EXACT_DECIMAL,
        )
        for k in PROHIBITED_METRIC_KEYS
    )
    return ProcessRuleSet(
        "synthetic",
        "coating",
        "month",
        "1",
        tuple(items),
        quality,
        frozenset(),
        fingerprint(tuple(items)),
    )


def setup_graph(nodes, ruleset=None, *, clock=lambda: 0.0, cancellation=None):
    ruleset = rules() if ruleset is None else ruleset
    problem = SchedulingProblem(
        "problem",
        "synthetic",
        "coating",
        "month",
        tuple(nodes),
        ("period",),
        (),
        fingerprint(tuple(nodes)),
    )
    context = RuleEvaluationContext(("period",), {"period": 0}, ())
    cache = RuleEdgeDecisionCache(problem, ruleset, context)
    budget = SolveRuntimeBudget(0, 100, 110, 0, 0, cancellation, clock=clock)
    return problem, cache, budget


def build(nodes, ruleset=None, *, seed=7):
    problem, cache, budget = setup_graph(nodes, ruleset)
    return build_construction_dag(problem, cache, budget, seed=seed)


def test_equal_keys_keep_both_reference_shuffle_orders():
    graph = build([node(name) for name in "abcdef"])
    assert graph.ordered_node_ids == tuple("eafdbc")
    assert dict(graph.adjacency) == {
        "e": tuple("dbfca"),
        "a": tuple("cbfd"),
        "f": tuple("dcb"),
        "d": tuple("cb"),
        "b": ("c",),
        "c": (),
    }


def test_fixed_baseline_seed_has_its_own_frozen_order():
    graph = build([node(name) for name in "abcdef"], seed=590531)
    assert graph.ordered_node_ids == tuple("fecadb")
    assert graph.adjacency["f"] == tuple("cdeab")
    assert graph.adjacency["e"] == tuple("bdac")


def test_main_keys_are_width_priority_thickness_temperature_in_that_order():
    nodes = [
        node("narrow", width="900", priority=-1),
        node("ordinary", priority=1),
        node("thick", thickness="2"),
        node("hot", temperature="750"),
        node("cool"),
    ]
    assert build(nodes, rules(priority=True)).ordered_node_ids == (
        "cool",
        "hot",
        "thick",
        "ordinary",
        "narrow",
    )


@pytest.mark.parametrize("field", ("width", "thickness", "temperature"))
def test_null_main_sort_field_follows_present_values(field):
    assert build([node("missing", **{field: None}), node("present")]).ordered_node_ids == (
        "present",
        "missing",
    )


def test_disabled_priority_does_not_read_input_priority_or_create_hidden_rank():
    nodes = [node(name, priority="not-an-enabled-input") for name in "abcdef"]
    assert build(nodes).ordered_node_ids == tuple("eafdbc")


def test_priority_is_computed_once_per_input_node(monkeypatch):
    original = ProcessRuleSet.construction_priority
    calls = []

    def tracked(self, value):
        calls.append(value.node_id)
        return original(self, value)

    monkeypatch.setattr(ProcessRuleSet, "construction_priority", tracked)
    build([node(name) for name in "abcdef"], rules(priority=True))
    assert sorted(calls) == list("abcdef")


def test_physical_float_ties_keep_shuffle_order_not_exact_decimal_order():
    nodes = [
        node(
            name,
            width=f"1000.00000000000000{i}",
            thickness=f"1.0000000000000000{i}",
            temperature=f"700.00000000000000{i}",
        )
        for i, name in enumerate("abcdef")
    ]
    assert build(nodes).ordered_node_ids == tuple("eafdbc")


def test_successor_weight_float_ties_keep_second_shuffle_order():
    nodes = [node(name, weight=f"100.00000000000000{i}") for i, name in enumerate("abcdef")]
    graph = build(nodes)
    assert graph.adjacency["e"] == tuple("dbfca")
    assert [n.weight for n in nodes] == [Decimal(f"100.00000000000000{i}") for i in range(6)]


def test_successor_sort_prefers_thickness_then_weight_then_priority():
    left = node("left", width="1100", thickness="1")
    close_low = node("close-low", thickness="1.1", weight="10", priority=-1)
    close_high = node("close-high", thickness="1.1", weight="30", priority=1)
    close_priority = node("close-priority", thickness="1.1", weight="30", priority=0)
    far = node("far", thickness="1.4", weight="90", priority=-2)
    graph = build([left, close_low, close_high, close_priority, far], rules(priority=True))
    assert graph.adjacency["left"] == ("close-priority", "close-high", "close-low", "far")


def test_null_successor_thickness_is_zero_for_difference():
    graph = build(
        [
            node("left", width="1100", thickness=None),
            node("thin", thickness="1"),
            node("missing", thickness=None),
        ]
    )
    assert graph.adjacency["left"] == ("missing", "thin")


def test_seed_is_private_and_output_fingerprint_excludes_elapsed_time():
    state = random.getstate()
    nodes = [node(name) for name in "abcdef"]
    first, second = build(nodes), build(nodes)
    assert first.fingerprint == second.fingerprint
    assert first.adjacency == second.adjacency
    assert random.getstate() == state
    assert (
        replace(first, elapsed_seconds=first.elapsed_seconds + 10).fingerprint == first.fingerprint
    )


@pytest.mark.parametrize("seed", (True, None, "7", 1.1))
def test_invalid_seed_rejected(seed):
    with pytest.raises(ValueError):
        build([node("a")], seed=seed)


def test_negative_integer_seed_is_supported():
    assert build([node("a")], seed=-1).ordered_node_ids == ("a",)


@pytest.mark.parametrize("field", ("width", "thickness", "weight"))
def test_nonfinite_sort_projection_fails_explicitly(field):
    with pytest.raises(ValueError, match="finite"):
        build([node("huge", **{field: "1e10000"})])
