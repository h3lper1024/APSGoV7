"""Virtual bridge, weight-fill and split-separator choices keep distinct rankings."""

from dataclasses import replace
from decimal import ROUND_UP, Decimal, Inexact, localcontext

import pytest

from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, fingerprint
from apsgo_scheduler.core.model import (
    MaterialRole,
    SchedulingProblem,
    VirtualMaterialPrototype,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    RuleViolation,
)
from apsgo_scheduler.core.rules.concrete import ConsecutiveVirtualMaterialRule
from apsgo_scheduler.core.virtual_material import VirtualFactory, virtual_smoothness
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node, rules

D = Decimal


class AllowedConnectionsRule(Rule):
    supported_scope = RuleScope.EDGE

    def required_fields(self):
        return ()

    def edge_semantic_fields(self):
        return ("grade",)

    def evaluate(self, subject, context):
        if (subject.left.grade, subject.right.grade) in self.parameters["allowed_edges"]:
            return RuleContribution((), ())
        return RuleContribution(
            (
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "synthetic_edge_denied",
                    "This grade pair is not in the test connection graph.",
                    RuleDisposition.PROHIBITED,
                    D(1),
                ),
            ),
            (),
        )


def prototype(name, *, width="1000", thickness="1", weight="20", **changes):
    values = dict(
        prototype_id=name,
        unit_weight=D(weight),
        width=None if width is None else D(width),
        thickness=None if thickness is None else D(thickness),
        min_temperature=None,
        max_temperature=None,
        grade=name,
        rule_attributes={},
    )
    return VirtualMaterialPrototype(**(values | changes))


def make_factory(prototypes=(), *, allowed_edges=None, rule_items=(), nodes=None, runtime=None):
    nodes = (
        tuple(replace(node(name), grade=name) for name in ("left", "right"))
        if nodes is None
        else tuple(nodes)
    )
    items = tuple(rule_items)
    if allowed_edges is not None:
        items += (
            AllowedConnectionsRule(
                "connections",
                "connections",
                RuleScope.EDGE,
                True,
                "1",
                {"allowed_edges": tuple(sorted(allowed_edges))},
            ),
        )
    rule_set = replace(rules(), rules=items, fingerprint=fingerprint(items))
    problem = SchedulingProblem(
        "problem",
        rule_set.product_line_code,
        rule_set.process_code,
        rule_set.scenario,
        nodes,
        ("period",),
        tuple(prototypes),
        fingerprint((nodes, tuple(prototypes))),
    )
    context = RuleEvaluationContext(
        problem.period_order,
        {"period": 0},
        tuple(item.prototype_id for item in problem.virtual_prototypes),
    )
    return VirtualFactory(
        RuleEdgeDecisionCache(problem, rule_set, context),
        budget() if runtime is None else runtime,
    )


class VirtualPenaltyRule(Rule):
    supported_scope = RuleScope.CHAIN

    def required_fields(self):
        return ()

    def evaluate(self, subject, context):
        return RuleContribution(
            tuple(
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    f"{subject.subject_id}:{position}:{index}",
                    "synthetic_virtual_penalty",
                    "Synthetic chain penalty for testing candidate ranking.",
                    RuleDisposition.PROHIBITED,
                    item.rule_attributes["penalty_severity"],
                )
                for position, item in enumerate(subject.chain.nodes)
                if item.material_role is MaterialRole.GENERATED_VIRTUAL
                for index in range(item.rule_attributes.get("penalty_count", 0))
            ),
            (),
        )


def penalty_rule():
    return VirtualPenaltyRule("penalty", "penalty", RuleScope.CHAIN, True, "1", {})


def selected_prototypes(bridge):
    return None if bridge is None else tuple(item.virtual_lineage.prototype_id for item in bridge)


def test_direct_edge_returns_empty_bridge_even_when_virtual_limits_are_zero():
    cap = ConsecutiveVirtualMaterialRule("cap", "cap", RuleScope.CHAIN, True, "1", {"max_count": 0})
    factory = make_factory((prototype("p"),), rule_items=(cap,))
    assert factory.bridge(*factory.cache.problem.nodes, max_nodes=0, first_sequence=1) == ()


def test_single_bridge_is_preferred_even_when_a_double_bridge_is_smoother():
    factory = make_factory(
        (prototype("single", width="2000"), prototype("first"), prototype("second")),
        allowed_edges={
            ("left", "single"),
            ("single", "right"),
            ("left", "first"),
            ("first", "second"),
            ("second", "right"),
        },
    )
    result = factory.bridge(*factory.cache.problem.nodes, max_nodes=2, first_sequence=1)
    assert selected_prototypes(result) == ("single",)


def test_bridge_ranking_uses_only_smoothness_not_chain_penalties_or_weight():
    factory = make_factory(
        (
            prototype(
                "smooth",
                weight="200",
                rule_attributes={"penalty_count": 2, "penalty_severity": D(100)},
            ),
            prototype("clean", width="900", weight="1"),
        ),
        allowed_edges={("left", name) for name in ("smooth", "clean")}
        | {(name, "right") for name in ("smooth", "clean")},
        rule_items=(penalty_rule(),),
    )
    result = factory.bridge(*factory.cache.problem.nodes, max_nodes=2, first_sequence=1)
    assert selected_prototypes(result) == ("smooth",)


@pytest.mark.parametrize("order", (("a", "b"), ("b", "a")))
def test_equal_single_bridge_scores_keep_prototype_catalog_order(order):
    factory = make_factory(
        tuple(prototype(name) for name in order),
        allowed_edges={("left", name) for name in order} | {(name, "right") for name in order},
    )
    result = factory.bridge(*factory.cache.problem.nodes, max_nodes=2, first_sequence=1)
    assert selected_prototypes(result) == (order[0],)


@pytest.mark.parametrize(
    "first_width,order,expected",
    (
        ("1000", ("a", "b", "d", "c"), ("a", "d")),
        ("1000", ("b", "a", "c", "d"), ("b", "c")),
        ("500", ("a", "b", "c", "d"), ("b", "c")),
    ),
)
def test_double_bridge_uses_sum_of_both_triplet_scores_then_nested_catalog_order(
    first_width, order, expected
):
    factory = make_factory(
        tuple(prototype(name, width=first_width if name == "a" else "1000") for name in order),
        allowed_edges={("left", first) for first in ("a", "b")}
        | {(first, second) for first in ("a", "b") for second in ("c", "d")}
        | {(second, "right") for second in ("c", "d")},
    )
    result = factory.bridge(*factory.cache.problem.nodes, max_nodes=2, first_sequence=1)
    assert selected_prototypes(result) == expected


@pytest.mark.parametrize(
    "double,max_nodes,cap,enabled,expected",
    (
        (False, 0, 2, True, None),
        (False, 2, 0, True, None),
        (False, 2, 1, True, ("a",)),
        (False, 1, 0, False, ("a",)),
        (True, 1, 2, True, None),
        (True, 2, 1, True, None),
        (True, 2, 2, True, ("a", "b")),
        (True, 2, 0, False, ("a", "b")),
    ),
)
def test_bridge_node_limit_is_minimum_of_policy_and_enabled_rule(
    double, max_nodes, cap, enabled, expected
):
    limit = ConsecutiveVirtualMaterialRule(
        "cap", "cap", RuleScope.CHAIN, enabled, "1", {"max_count": cap}
    )
    edges = (
        {("left", "a"), ("a", "b"), ("b", "right")} if double else {("left", "a"), ("a", "right")}
    )
    factory = make_factory(
        (prototype("a"), prototype("b")), allowed_edges=edges, rule_items=(limit,)
    )
    result = factory.bridge(*factory.cache.problem.nodes, max_nodes=max_nodes, first_sequence=1)
    assert selected_prototypes(result) == expected


@pytest.mark.parametrize("prototypes", ((), (prototype("p"),)))
def test_missing_catalog_or_missing_second_edge_has_no_bridge_or_separator(prototypes):
    factory = make_factory(prototypes, allowed_edges={("left", "p")})
    anchors = factory.cache.problem.nodes
    assert factory.bridge(*anchors, max_nodes=2, first_sequence=1) is None
    assert factory.separator(*anchors, first_sequence=1, related_partition_id="partition") is None
    assert factory.budget.stop_reason is None


def test_separator_is_forced_even_when_anchors_can_connect_directly():
    factory = make_factory((prototype("p"),))
    anchors = factory.cache.problem.nodes
    assert factory.bridge(*anchors, max_nodes=2, first_sequence=1) == ()
    result = factory.separator(*anchors, first_sequence=1, related_partition_id="partition")
    assert result.virtual_lineage.prototype_id == "p"
    assert result.virtual_lineage.purpose is VirtualPurpose.SPLIT_SEPARATOR
    assert result.virtual_lineage.related_partition_id == "partition"


@pytest.mark.parametrize(
    "first,second,expected",
    (
        ((2, "1", "1000"), (1, "100", "2000"), "b"),
        ((1, "3", "1000"), (1, "2", "2000"), "b"),
        ((1, "2", "2000"), (1, "2", "1000"), "b"),
        ((1, "2", "1000"), (1, "2", "1000"), "a"),
    ),
)
def test_separator_ranks_prohibited_count_then_severity_then_smoothness_then_catalog(
    first, second, expected
):
    prototypes = tuple(
        prototype(
            name,
            width=width,
            rule_attributes={"penalty_count": count, "penalty_severity": D(severity)},
        )
        for name, (count, severity, width) in zip(("a", "b"), (first, second))
    )
    factory = make_factory(prototypes, rule_items=(penalty_rule(),))
    result = factory.separator(
        *factory.cache.problem.nodes, first_sequence=1, related_partition_id="partition"
    )
    assert result.virtual_lineage.prototype_id == expected
    assert factory.budget.candidate_check_count == 0


@pytest.mark.parametrize(
    "left_interval,right_interval,expected",
    (
        (("600", "700"), ("650", "800"), ("600", "800")),
        ((None, None), ("500", "600"), ("500", "600")),
        ((None, None), (None, None), (None, None)),
        ((None, "700"), ("500", None), ("500", "700")),
        ((None, None), (None, "900"), (None, "900")),
        (("-50", "0"), ("-80", "-10"), ("-80", "0")),
    ),
)
def test_materialized_temperature_is_anchor_envelope_not_prototype_interval(
    left_interval, right_interval, expected
):
    anchors = tuple(
        replace(
            node(name),
            min_temperature=None if lower is None else D(lower),
            max_temperature=None if upper is None else D(upper),
        )
        for name, (lower, upper) in zip(("left", "right"), (left_interval, right_interval))
    )
    proto = prototype("p", min_temperature=D(850), max_temperature=D(950))
    factory = make_factory((proto,), nodes=anchors)
    result = factory.materialize(proto, *anchors, purpose=VirtualPurpose.WEIGHT_FILL, sequence=1)
    assert (result.min_temperature, result.max_temperature) == tuple(
        None if value is None else D(value) for value in expected
    )


def test_fill_materializes_supplied_prototype_without_selecting_or_evaluating_candidates():
    first = prototype("smooth")
    requested = prototype(
        "raw grade",
        width="1000.00000000000000001",
        thickness="1.123456789",
        weight="21.23456789",
        rule_attributes={"custom": " unchanged ", "number": 0},
    )
    factory = make_factory((first, requested), allowed_edges=())
    anchors = factory.cache.problem.nodes
    before = fingerprint(factory.cache.problem)
    result = factory.materialize(
        requested, *anchors, purpose=VirtualPurpose.WEIGHT_FILL, sequence=1
    )
    assert result.grade == requested.grade
    assert (result.width, result.thickness, result.weight, result.rule_attributes) == (
        requested.width,
        requested.thickness,
        requested.unit_weight,
        requested.rule_attributes,
    )
    assert result.material_role is MaterialRole.GENERATED_VIRTUAL
    assert result.source_order_id is result.source_resource_id is result.source_period is None
    assert result.split_lineage is None
    assert result.virtual_lineage.prototype_id == requested.prototype_id
    assert result.virtual_lineage.purpose is VirtualPurpose.WEIGHT_FILL
    assert factory.cache.entry_count == factory.budget.candidate_check_count == 0
    assert fingerprint(factory.cache.problem) == before


@pytest.mark.parametrize("position", (0, 1))
def test_fill_at_chain_ends_reuses_the_single_existing_anchor_twice(position):
    anchors = (
        replace(node("left"), min_temperature=D(600), max_temperature=D(700)),
        replace(node("right"), min_temperature=D(800), max_temperature=D(900)),
    )
    proto = prototype("p")
    factory = make_factory((proto,), nodes=anchors)
    anchor = anchors[position]
    result = factory.materialize(
        proto, anchor, anchor, purpose=VirtualPurpose.WEIGHT_FILL, sequence=1
    )
    assert (result.min_temperature, result.max_temperature) == (
        anchor.min_temperature,
        anchor.max_temperature,
    )


@pytest.mark.parametrize(
    "widths,thicknesses,expected",
    (
        ((None, None, None), (None, None, None), 0.0),
        ((None, "20", "30"), (None, "1", "2"), 230.0),
        (("1000", "900", "1000"), ("1", "2", "1"), 400.0),
    ),
)
def test_smoothness_uses_float_triplet_formula_and_zero_for_missing_dimensions(
    widths, thicknesses, expected
):
    triplet = tuple(
        node(name, width=width, thickness=thickness)
        for name, width, thickness in zip("abc", widths, thicknesses)
    )
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert virtual_smoothness(*triplet) == expected


@pytest.mark.parametrize(
    "widths,thicknesses",
    (
        (("1e309", "1", "1"), ("1", "1", "1")),
        (("1e308", "1", "1e308"), ("1", "1", "1")),
        (("1", "1", "1"), ("1e308", "1", "1")),
    ),
)
def test_smoothness_rejects_nonfinite_projection_or_derived_arithmetic(widths, thicknesses):
    triplet = tuple(
        node(name, width=width, thickness=thickness)
        for name, width, thickness in zip("abc", widths, thicknesses)
    )
    with pytest.raises(ValueError, match="finite"):
        virtual_smoothness(*triplet)
