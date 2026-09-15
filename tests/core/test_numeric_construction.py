"""Numeric graph, path cover and initial construction preserve deterministic semantics."""

from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest

from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core._numeric_construction import (
    NumericConstructionGraph,
    NumericPathCover,
    build_numeric_construction_graph,
    construct_numeric_initial_plan,
    numeric_minimum_path_cover,
)
from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
from apsgo_scheduler.core._numeric_rules import NumericRuleProgram
from apsgo_scheduler.core._numeric_state import NumericTask, readonly
from apsgo_scheduler.core._numeric_units import NumericValueError
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import SearchStopReason
from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput
from tests.app.test_input_normalizer import make_order, make_request
from tests.core.test_numeric_evaluation import evaluation_case, numeric_quality_spec
from tests.core.test_numeric_rules import attributes

D = Decimal


def budget(*, cancellation=None):
    return SolveRuntimeBudget(0, 100, 110, 0, 0, cancellation, clock=lambda: 1)


def construction_case(*, weights=("100", "100"), widths=("1000", "900")):
    orders = tuple(
        make_order(
            index,
            weight=D(weight),
            width=D(width),
            grade=f"G{index}",
            source_period="P0",
            rule_attributes=attributes(
                surface_grade="",
                grade_class="ordinary",
                customer_name="ordinary",
            ),
        )
        for index, (weight, width) in enumerate(zip(weights, widths))
    )
    spec = numeric_quality_spec()
    request = make_request(rule_set_spec=spec, orders=orders)
    timing = DeliveryTimingInput(
        "2026-06-01T00:00:00+08:00",
        tuple(OrderTimingInput(order.source_order_id, "2026-06-30", D("1")) for order in orders),
        {prototype.prototype_id: D("0.1") for prototype in request.virtual_prototypes},
    )
    request = replace(request, delivery_timing=timing)
    rules = load_rule_set(spec)
    task = NumericTask.build(normalize_input(request, rules), rules, timing)
    program = NumericRuleProgram.compile(task, rules)
    quality = NumericQualityProgram.compile(task, program, rules)
    return task, program, quality


def test_numeric_graph_cover_and_initial_plan_form_one_direct_chain():
    task, program, quality = construction_case()
    runtime = budget()
    graph = build_numeric_construction_graph(task, program, runtime, seed=590531)
    cover = numeric_minimum_path_cover(graph, runtime)
    initial = construct_numeric_initial_plan(task, program, quality, graph, cover, runtime)

    assert graph.complete and graph.ordered_rows.tolist() == [0, 1]
    assert graph.adjacency_offsets.tolist() == [0, 1, 1]
    assert graph.adjacency_rows.tolist() == [1]
    assert (graph.checked_edge_count, graph.allowed_edge_count) == (1, 1)
    assert cover.complete and cover.matching_successor.tolist() == [1, -1]
    assert cover.path_offsets.tolist() == [0, 2] and cover.path_rows.tolist() == [0, 1]
    assert initial.complete and initial.plan.chain_offsets.tolist() == [0, 2]
    assert initial.plan.node_rows.tolist() == [0, 1]
    assert initial.evaluation.quality_key.tolist()[-1] == 1
    assert runtime.candidate_check_count == 0 and runtime.stop_reason is None


def test_initial_construction_cuts_when_chain_profile_worsens_or_weight_exceeds_limit():
    _, task, program, quality, _ = evaluation_case()
    runtime = budget()
    graph = build_numeric_construction_graph(task, program, runtime, seed=7)
    cover = numeric_minimum_path_cover(graph, runtime)
    initial = construct_numeric_initial_plan(task, program, quality, graph, cover, runtime)
    assert initial.plan.chain_offsets.tolist() == [0, 1, 2]

    task, program, quality = construction_case(weights=("600", "600"))
    runtime = budget()
    graph = build_numeric_construction_graph(task, program, runtime, seed=7)
    cover = numeric_minimum_path_cover(graph, runtime)
    initial = construct_numeric_initial_plan(task, program, quality, graph, cover, runtime)
    assert initial.plan.chain_offsets.tolist() == [0, 1, 2]


def test_numeric_matching_rewires_an_augmenting_path():
    graph = NumericConstructionGraph(
        "task",
        "rules",
        readonly([0, 1, 2, 3], np.int64),
        readonly([0, 2, 4, 5, 5], np.int64),
        readonly([2, 1, 3, 2, 3], np.int64),
        6,
        5,
        0,
        None,
        0.0,
        "graph",
    )
    result = numeric_minimum_path_cover(graph, budget())
    assert result.matching_successor.tolist() == [1, 2, 3, -1]
    assert result.path_rows.tolist() == [0, 1, 2, 3]


def test_numeric_construction_observes_cancellation_without_signing_partial_state():
    class Cancelled:
        def is_cancelled(self):
            return True

    task, program, _ = construction_case()
    runtime = budget(cancellation=Cancelled())
    graph = build_numeric_construction_graph(task, program, runtime, seed=7)
    assert not graph.complete and graph.fingerprint is None
    assert graph.stop_reason is SearchStopReason.USER_CANCELLED
    assert graph.checked_edge_count == 0
    with pytest.raises(NumericValueError, match="complete numeric construction graph"):
        numeric_minimum_path_cover(graph, runtime)


def test_numeric_construction_rejects_cross_task_program():
    task, program, _ = construction_case()
    with pytest.raises(NumericValueError, match="matching numeric task"):
        build_numeric_construction_graph(
            replace(task, fingerprint="other"), program, budget(), seed=7
        )


def test_initial_construction_rejects_a_path_edge_absent_from_the_graph():
    task, program, quality = construction_case()
    graph = NumericConstructionGraph(
        task.fingerprint,
        program.fingerprint,
        readonly([0, 1], np.int64),
        readonly([0, 0, 0], np.int64),
        readonly([], np.int64),
        1,
        0,
        0,
        None,
        0.0,
        "graph",
    )
    cover = NumericPathCover(
        "graph",
        readonly([1, -1], np.int64),
        readonly([0, 2], np.int64),
        readonly([0, 1], np.int64),
        None,
        0.0,
        "cover",
    )
    with pytest.raises(NumericValueError, match="absent graph edge"):
        construct_numeric_initial_plan(task, program, quality, graph, cover, budget())
