"""Initial construction against the frozen reference stage, without running search."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import fingerprint, sum_weights
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.initial_solution import construct_initial_plan
from apsgo_scheduler.core.model import MaterialRole
from apsgo_scheduler.core.path_cover import PathCoverResult
from apsgo_scheduler.core.rules.base import RuleDisposition, RuleEvaluationContext
from tests.app.test_input_normalizer import gqga4_request, gqga4_six_level_spec
from tests.core.graph.test_bipartite_matching import budget, make_dag

D = Decimal


@pytest.fixture(scope="module")
def initial_stage():
    frozen = json.loads(
        (
            Path(__file__).parents[2] / "baselines/gqga4/reference_stage_expectations.json"
        ).read_text(),
        parse_float=D,
    )
    spec = gqga4_six_level_spec.__wrapped__()
    rules = load_rule_set(spec)
    problem = normalize_input(gqga4_request.__wrapped__(spec), rules)
    before = fingerprint(problem)
    context = RuleEvaluationContext(
        problem.period_order,
        {period: index for index, period in enumerate(problem.period_order)},
        tuple(item.prototype_id for item in problem.virtual_prototypes),
    )
    cache = RuleEdgeDecisionCache(problem, rules, context)
    cover = frozen["path_cover"]
    ids = tuple(cover["ordered_node_ids"])
    dag = make_dag(
        ids,
        {row["node_id"]: row["successor_node_ids"] for row in cover["adjacency"]},
    )
    paths = PathCoverResult(
        dag.fingerprint,
        tuple((left, cover["match_left"][left]) for left in ids if cover["match_left"][left]),
        tuple(tuple(path) for path in cover["paths"]),
        None,
        0.0,
    )
    runtime = budget()
    result = construct_initial_plan(problem, dag, paths, cache, runtime)
    assert fingerprint(problem) == before
    assert result.complete and result.candidate is not None
    return frozen["initial_plan"], problem, cache, runtime, result


def test_all_initial_chains_match_reference_in_original_path_order(initial_stage):
    expected, _, _, runtime, result = initial_stage
    plan = result.candidate.plan
    assert len(plan.chains) == len(expected["plan"]) == 31
    for index, (chain, frozen_chain) in enumerate(zip(plan.chains, expected["plan"]), 1):
        assert chain.chain_id == f"initial-{index:06d}"
        assert (chain.assigned_period, tuple(node.node_id for node in chain.nodes)) == (
            frozen_chain["assigned_period"],
            tuple(frozen_chain["node_ids"]),
        ), f"first differing initial chain: {index}"
    assert Counter(chain.assigned_period for chain in plan.chains) == {
        "BR_00000001": 24,
        "BR_00000002": 3,
        "BR_00000006": 4,
    }
    assert result.plan_fingerprint == fingerprint(plan)
    assert result.stop_reason is runtime.stop_reason is None
    assert runtime.candidate_check_count == runtime.candidate_check_limit == 0
    assert expected["candidate_checks"] == 0


def test_initial_quality_keeps_reference_first_six_and_borrowing_as_diagnostic(initial_stage):
    expected, _, cache, _, result = initial_stage
    evaluation = result.candidate.search_evaluation
    assert evaluation.quality_key == (2, D("170.3"), 15, D("4003.93"), 31, D(0))
    assert evaluation.quality_key == tuple(expected["quality"][:6])
    # The seventh reference component was deliberately removed from the quality order.
    assert len(expected["quality"]) == 7
    assert evaluation.metrics["borrowed_future_weight"] == expected["quality"][6] == D("21354.53")
    assert evaluate_plan(result.candidate.plan, cache.rule_set, cache.context) == evaluation


def test_initial_stage_preserves_every_input_node_and_generates_no_resources(initial_stage):
    expected, problem, cache, _, result = initial_stage
    plan = result.candidate.plan
    by_id = {node.node_id: node for node in problem.nodes}
    scheduled = tuple(node for chain in plan.chains for node in chain.nodes)
    assert len(scheduled) == len(by_id) == 531
    assert Counter(node.node_id for node in scheduled) == Counter(by_id.keys())
    assert all(node is by_id[node.node_id] for node in scheduled)
    assert sum_weights(node.weight for node in scheduled) == D("29333.91")
    assert sum_weights(node.weight for node in scheduled) == D(expected["metrics"]["real_weight"])
    assert Counter(node.material_role for node in scheduled) == {
        MaterialRole.NORMAL_REAL: 470,
        MaterialRole.ACTUAL_TRANSITION: 61,
    }
    assert all(node.split_lineage is node.virtual_lineage is None for node in scheduled)
    for chain, evaluation in zip(plan.chains, result.candidate.search_evaluation.chain_evaluations):
        assert chain.nodes
        assert chain.assigned_period == min(
            (node.source_period for node in chain.nodes), key=cache.context.period_index.__getitem__
        )
        assert chain.total_weight <= D("2000.000001")
        assert (
            evaluation.summary.total_weight == evaluation.summary.real_weight == chain.total_weight
        )
        assert evaluation.summary.virtual_node_count == evaluation.summary.split_piece_count == 0
        assert evaluation.summary.virtual_weight == 0


def test_reference_atomic_narrow_violations_are_retained_for_later_search(initial_stage):
    _, _, _, _, result = initial_stage
    evaluation = result.candidate.search_evaluation
    chains = {chain.chain_id: chain for chain in result.candidate.plan.chains}
    prohibited = tuple(
        (
            detail.chain_id,
            tuple(node.node_id for node in chains[detail.chain_id].nodes),
            chains[detail.chain_id].total_weight,
            chains[detail.chain_id].assigned_period,
            violation.reason_code,
            violation.severity,
        )
        for detail in evaluation.chain_evaluations
        for violation in detail.violations
        if violation.disposition is RuleDisposition.PROHIBITED
    )
    assert prohibited == (
        (
            "initial-000010",
            ("0002002055-000120",),
            D("570.3"),
            "BR_00000001",
            "if_narrow_run_weight",
            D("70.3"),
        ),
        (
            "initial-000012",
            ("0002002073-000010",),
            D("600.0"),
            "BR_00000006",
            "if_narrow_run_weight",
            D("100.0"),
        ),
    )
    allowed = tuple(
        violation
        for violation in evaluation.violations
        if violation.disposition is RuleDisposition.ALLOWED_FINAL_DEVIATION
    )
    assert len(evaluation.violations) == 17 and len(allowed) == 15
    assert {violation.reason_code for violation in allowed} == {"chain_weight_below_minimum"}
