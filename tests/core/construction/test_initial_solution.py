"""Direct append/cut construction produces only complete, evaluated input coverage."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, Inexact, localcontext
from itertools import pairwise
from unittest.mock import Mock

import pytest

from apsgo_scheduler.core import initial_solution
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, SearchStopReason, fingerprint
from apsgo_scheduler.core.initial_solution import InitialSolutionResult, construct_initial_plan
from apsgo_scheduler.core.model import MaterialRole
from apsgo_scheduler.core.path_cover import minimum_path_cover
from apsgo_scheduler.core.rules.base import (
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    RuleViolation,
)
from apsgo_scheduler.core.rules.concrete import ChainWeightRangeRule, HighSurfaceRunCountRule
from tests.core.graph.test_bipartite_matching import budget, make_dag
from tests.core.graph.test_construction_order import node, rules, setup_graph


def weight_rule(*, minimum="0", maximum="50", enabled=True):
    return ChainWeightRangeRule(
        "weight",
        "weight",
        RuleScope.CHAIN,
        enabled,
        "1",
        {
            "min_weight": Decimal(minimum),
            "max_weight": Decimal(maximum),
            "target_weight": Decimal(maximum),
        },
    )


def surface_rule(maximum=1):
    return HighSurfaceRunCountRule(
        "surface",
        "surface",
        RuleScope.CHAIN,
        True,
        "1",
        {"surface_grades": ("FC",), "max_run_count": maximum},
    )


def inputs(nodes=None, *, rule_items=(), paths=None):
    nodes = tuple(node(name) for name in "abc") if nodes is None else tuple(nodes)
    current_rules = replace(
        rules(),
        rules=rule_items,
        fingerprint=fingerprint(rule_items),
        allowed_final_deviation_codes=frozenset(("chain_weight_below_minimum",)),
    )
    problem, cache, runtime = setup_graph(nodes, current_rules)
    paths = (tuple(item.node_id for item in nodes),) if paths is None else paths
    ids = tuple(name for path in paths for name in path)
    dag = make_dag(ids, {left: (right,) for path in paths for left, right in pairwise(path)})
    return problem, dag, minimum_path_cover(dag, budget()), cache, runtime


def plan_paths(result):
    return tuple(
        tuple(node.node_id for node in chain.nodes) for chain in result.candidate.plan.chains
    )


def assert_complete(result, original):
    assert result.complete and result.stop_reason is None
    assert result.plan_fingerprint == fingerprint(result.candidate.plan)
    plan = result.candidate.plan
    nodes = tuple(node for chain in plan.chains for node in chain.nodes)
    assert len(nodes) == len(original.nodes)
    assert {node.node_id: node for node in nodes} == {node.node_id: node for node in original.nodes}
    assert all(node.material_role is not MaterialRole.GENERATED_VIRTUAL for node in nodes)
    assert tuple(chain.chain_id for chain in plan.chains) == tuple(
        f"initial-{index:06d}" for index in range(1, len(plan.chains) + 1)
    )
    evaluation = result.candidate.search_evaluation
    assert tuple(item.chain_id for item in evaluation.chain_evaluations) == tuple(
        chain.chain_id for chain in plan.chains
    )
    assert evaluation.metrics["generated_virtual_weight"] == 0


@pytest.mark.parametrize(
    "paths", ((("a", "b", "c"),), (("b", "c"), ("a",)), (("c",), ("b",), ("a",)))
)
def test_initial_chains_keep_path_and_node_order_and_cover_every_input(paths):
    arguments = inputs(paths=paths)
    original = arguments[0]
    result = construct_initial_plan(*arguments)
    assert_complete(result, original)
    assert plan_paths(result) == paths
    assert arguments[-1].candidate_check_count == 0
    assert arguments[-1].stop_reason is None
    assert all(chain.assigned_period == "period" for chain in result.candidate.plan.chains)


@pytest.mark.parametrize(
    "second,expected",
    (
        ("20", (("a", "b"),)),
        ("20.000000999", (("a", "b"),)),
        ("20.000001", (("a", "b"),)),
        ("20.000001001", (("a",), ("b",))),
    ),
)
def test_weight_upper_bound_uses_exact_sum_and_existing_epsilon(second, expected):
    arguments = inputs(
        (node("a", weight="30"), node("b", weight=second)), rule_items=(weight_rule(),)
    )
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        result = construct_initial_plan(*arguments)
    assert_complete(result, arguments[0])
    assert plan_paths(result) == expected
    assert result.candidate.search_evaluation.metrics["overweight_chain_count"] == 0


def test_weight_rejection_precedes_quick_profile_and_never_discards_the_next_node(monkeypatch):
    arguments = inputs(
        (node("a", weight="30"), node("b", weight="30")), rule_items=(weight_rule(),)
    )
    profile = Mock(side_effect=AssertionError("an overweight append needs no profile"))
    monkeypatch.setattr(initial_solution, "quick_chain_prohibited_profile", profile)
    result = construct_initial_plan(*arguments)
    assert plan_paths(result) == (("a",), ("b",))
    profile.assert_not_called()


@pytest.mark.parametrize("include_disabled", (False, True))
def test_absent_or_disabled_chain_weight_rule_has_no_hidden_upper_bound(include_disabled):
    arguments = inputs(
        (node("a", weight="9000"), node("b", weight="9000")),
        rule_items=(weight_rule(enabled=False),) if include_disabled else (),
    )
    result = construct_initial_plan(*arguments)
    assert_complete(result, arguments[0])
    assert plan_paths(result) == (("a", "b"),)
    assert "overweight_chain_count" not in result.candidate.search_evaluation.metrics


def test_underweight_and_target_deviation_do_not_block_initial_construction():
    arguments = inputs(rule_items=(weight_rule(minimum="700", maximum="2000"),))
    result = construct_initial_plan(*arguments)
    assert_complete(result, arguments[0])
    assert plan_paths(result) == (("a", "b", "c"),)
    evaluation = result.candidate.search_evaluation
    assert evaluation.metrics["underweight_chain_count"] == 1
    assert evaluation.metrics["underweight_total_gap"] == Decimal("640")
    assert evaluation.metrics["prohibited_violation_count"] == 0
    assert evaluation.quality_key == (0, Decimal(0))


def test_real_continuous_rule_cuts_before_profile_worsens():
    values = tuple(replace(node(name), rule_attributes={"surface_grade": "FC"}) for name in "abc")
    arguments = inputs(values, rule_items=(surface_rule(),))
    result = construct_initial_plan(*arguments)
    assert_complete(result, arguments[0])
    assert plan_paths(result) == (("a",), ("b",), ("c",))
    assert result.candidate.search_evaluation.metrics["prohibited_violation_count"] == 0


def test_actual_transition_is_preserved_and_breaks_the_real_continuous_run():
    values = tuple(replace(node(name), rule_attributes={"surface_grade": "FC"}) for name in "abc")
    values = (
        values[0],
        replace(values[1], material_role=MaterialRole.ACTUAL_TRANSITION),
        values[2],
    )
    arguments = inputs(values, rule_items=(surface_rule(),))
    result = construct_initial_plan(*arguments)
    assert_complete(result, arguments[0])
    assert plan_paths(result) == (("a", "b", "c"),)
    assert result.candidate.plan.chains[0].nodes[1] is values[1]
    assert result.candidate.search_evaluation.metrics["max_high_surface_run_count"] == 1


class PrefixProfileRule(Rule):
    supported_scope = RuleScope.CHAIN

    def required_fields(self):
        return ()

    def evaluate(self, subject, context):
        index = min(len(subject.chain.nodes) - 1, 1)
        return RuleContribution(
            tuple(
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    f"example-{position}",
                    "Controlled profile for tuple-order verification.",
                    RuleDisposition.PROHIBITED,
                    self.parameters["severities"][index],
                )
                for position in range(self.parameters["counts"][index])
            ),
            (),
        )


@pytest.mark.parametrize(
    "counts,severities,append",
    (
        ((2, 1), ("1", "100"), True),
        ((1, 2), ("100", "1"), False),
        ((1, 1), ("5", "4"), True),
        ((1, 1), ("5", "6"), False),
        ((1, 1), ("5", "5"), True),
    ),
)
def test_prohibited_profile_uses_tuple_less_or_equal_not_componentwise_comparison(
    counts, severities, append
):
    current = PrefixProfileRule(
        "profile",
        "profile",
        RuleScope.CHAIN,
        True,
        "1",
        {"counts": counts, "severities": tuple(map(Decimal, severities))},
    )
    arguments = inputs((node("a"), node("b")), rule_items=(current,))
    result = construct_initial_plan(*arguments)
    assert_complete(result, arguments[0])
    assert plan_paths(result) == ((("a", "b"),) if append else (("a",), ("b",)))


def test_unavoidable_singleton_business_violation_is_an_initial_candidate_not_input_loss():
    values = tuple(replace(node(name), rule_attributes={"surface_grade": "FC"}) for name in "ab")
    arguments = inputs(values, rule_items=(surface_rule(maximum=0),))
    result = construct_initial_plan(*arguments)
    assert_complete(result, arguments[0])
    assert plan_paths(result) == (("a",), ("b",))
    assert result.candidate.search_evaluation.metrics["prohibited_violation_count"] == 2


def test_period_is_earliest_real_source_in_task_order_without_resorting_chains():
    arguments = inputs(paths=(("b", "c"), ("a",)))
    problem, dag, cover, cache, runtime = arguments
    periods = ("z-early", "a-middle", "m-late")
    sources = {"a": "z-early", "b": "m-late", "c": "a-middle"}
    problem = replace(
        problem,
        nodes=tuple(
            replace(value, source_period=sources[value.node_id]) for value in problem.nodes
        ),
        period_order=periods,
        input_fingerprint="different-period-input",
    )
    context = RuleEvaluationContext(periods, {value: i for i, value in enumerate(periods)}, ())
    cache = RuleEdgeDecisionCache(problem, cache.rule_set, context)
    result = construct_initial_plan(problem, dag, cover, cache, runtime)
    assert_complete(result, problem)
    assert plan_paths(result) == (("b", "c"), ("a",))
    assert tuple(chain.assigned_period for chain in result.candidate.plan.chains) == (
        "a-middle",
        "z-early",
    )


def test_semantically_rejected_edge_cuts_the_chain_without_mutating_the_frozen_graph():
    arguments = inputs((node("a", width="1000"), node("b", width="1010")))
    problem, dag, cover, _, runtime = arguments
    _, cache, _ = setup_graph(problem.nodes, rules(maximum_increase="0"))
    before = dag.fingerprint, tuple(dag.adjacency.items()), cover.paths
    result = construct_initial_plan(problem, dag, cover, cache, runtime)
    assert_complete(result, problem)
    assert plan_paths(result) == (("a",), ("b",))
    assert before == (dag.fingerprint, tuple(dag.adjacency.items()), cover.paths)


def test_complete_evaluation_runs_once_and_unchanged_prefix_profiles_are_reused(monkeypatch):
    arguments = inputs()
    complete = Mock(wraps=initial_solution.evaluate_plan)
    quick = Mock(wraps=initial_solution.quick_chain_prohibited_profile)
    monkeypatch.setattr(initial_solution, "evaluate_plan", complete)
    monkeypatch.setattr(initial_solution, "quick_chain_prohibited_profile", quick)
    result = construct_initial_plan(*arguments)
    assert_complete(result, arguments[0])
    complete.assert_called_once_with(
        result.candidate.plan, arguments[3].rule_set, arguments[3].context
    )
    prefixes = [tuple(node.node_id for node in call.args[0].nodes) for call in quick.call_args_list]
    assert len(prefixes) == len(set(prefixes))
    assert set(prefixes) == {("a",), ("a", "b"), ("a", "b", "c")}


@pytest.mark.parametrize("index", range(5))
def test_input_types_are_rejected_before_construction(index):
    arguments = list(inputs())
    arguments[index] = None
    with pytest.raises(ValueError):
        construct_initial_plan(*arguments)


@pytest.mark.parametrize(
    "invalid",
    (
        "partial_graph",
        "partial_cover",
        "other_graph",
        "missing_path_node",
        "missing_edge",
        "other_cache",
    ),
)
def test_graph_path_and_cache_must_describe_exactly_the_current_input(invalid):
    problem, dag, cover, cache, runtime = inputs(paths=(("a", "b"), ("c",)))
    if invalid == "partial_graph":
        dag = replace(dag, stop_reason=SearchStopReason.USER_CANCELLED)
    elif invalid == "partial_cover":
        cover = replace(cover, paths=(), stop_reason=SearchStopReason.USER_CANCELLED)
    elif invalid == "other_graph":
        cover = replace(cover, graph_fingerprint="not-this-graph")
    elif invalid == "missing_path_node":
        cover = replace(cover, paths=(("a", "b"),))
    elif invalid == "missing_edge":
        cover = replace(cover, matching_edges=(("a", "c"),), paths=(("a", "c"), ("b",)))
    else:
        changed = replace(
            problem, nodes=(replace(problem.nodes[0], weight=Decimal(21)), *problem.nodes[1:])
        )
        cache = RuleEdgeDecisionCache(changed, cache.rule_set, cache.context)
    with pytest.raises(ValueError):
        construct_initial_plan(problem, dag, cover, cache, runtime)
    assert runtime.stop_reason is None and runtime.candidate_check_count == 0


def test_graph_with_different_nodes_is_rejected_even_when_its_cover_is_self_consistent():
    problem, _, _, cache, runtime = inputs()
    dag = make_dag(("a", "b", "foreign"), {})
    cover = minimum_path_cover(dag, budget())
    with pytest.raises(ValueError):
        construct_initial_plan(problem, dag, cover, cache, runtime)


def test_equal_problem_value_need_not_be_the_same_python_object():
    problem, dag, cover, cache, runtime = inputs()
    copied = replace(problem)
    assert copied == problem and copied is not problem
    assert_complete(construct_initial_plan(copied, dag, cover, cache, runtime), copied)


def test_oversized_atomic_input_that_bypassed_normalization_is_rejected():
    arguments = inputs((node("a", weight="50.000002"),), rule_items=(weight_rule(),))
    with pytest.raises(ValueError):
        construct_initial_plan(*arguments)


class Stop:
    active = False

    def is_cancelled(self):
        return self.active

    def clock(self):
        return 100.0 if self.active else 1.0


def assert_stopped(result, runtime, reason):
    assert not result.complete and result.stop_reason is reason
    assert result.candidate is result.plan_fingerprint is None
    assert runtime.stop_reason is reason and runtime.candidate_check_count == 0


@pytest.mark.parametrize("kind", ("cancel", "time", "existing"))
def test_stopped_before_construction_never_returns_a_candidate(kind):
    arguments = list(inputs())
    signal = Stop()
    signal.active = True
    if kind == "cancel":
        reason, runtime = SearchStopReason.USER_CANCELLED, budget(cancellation=signal)
    elif kind == "time":
        reason, runtime = SearchStopReason.SEARCH_TIME_LIMIT_REACHED, budget(clock=signal.clock)
    else:
        reason = SearchStopReason.CANDIDATE_LIMIT_REACHED
        runtime = budget(stop_reason=reason)
    arguments[-1] = runtime
    assert_stopped(construct_initial_plan(*arguments), runtime, reason)


@pytest.mark.parametrize("stage", ("edge", "quick", "evaluate", "fingerprint"))
@pytest.mark.parametrize("kind", ("cancel", "time"))
def test_stop_during_a_computation_is_observed_before_any_candidate_is_returned(
    monkeypatch, stage, kind
):
    arguments = list(inputs())
    signal = Stop()
    runtime = budget(cancellation=signal) if kind == "cancel" else budget(clock=signal.clock)
    reason = (
        SearchStopReason.USER_CANCELLED
        if kind == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )
    arguments[-1] = runtime
    full = Mock(wraps=initial_solution.evaluate_plan)
    monkeypatch.setattr(initial_solution, "evaluate_plan", full)
    owner, attribute = (
        (RuleEdgeDecisionCache, "allows")
        if stage == "edge"
        else (
            initial_solution,
            {
                "quick": "quick_chain_prohibited_profile",
                "evaluate": "evaluate_plan",
                "fingerprint": "fingerprint",
            }[stage],
        )
    )
    original = getattr(owner, attribute)

    def stop_after(*args, **kwargs):
        value = original(*args, **kwargs)
        signal.active = True
        return value

    monkeypatch.setattr(owner, attribute, stop_after)
    result = construct_initial_plan(*arguments)
    assert signal.active
    assert_stopped(result, runtime, reason)
    assert full.call_count == (1 if stage in ("evaluate", "fingerprint") else 0)


@pytest.mark.parametrize(
    "stage", ("quick_chain_prohibited_profile", "evaluate_plan", "fingerprint")
)
def test_computation_errors_propagate_without_fabricated_stop_or_mutated_inputs(monkeypatch, stage):
    arguments = inputs()
    problem, dag, cover, _, runtime = arguments
    original = fingerprint(problem), dag.fingerprint, cover.path_fingerprint
    error = RuntimeError("deliberate calculation failure")
    monkeypatch.setattr(initial_solution, stage, Mock(side_effect=error))
    with pytest.raises(RuntimeError) as raised:
        construct_initial_plan(*arguments)
    assert raised.value is error
    assert runtime.stop_reason is None and runtime.candidate_check_count == 0
    assert original == (fingerprint(problem), dag.fingerprint, cover.path_fingerprint)


def test_result_is_immutable_and_keeps_candidate_and_fingerprint_together():
    result = construct_initial_plan(*inputs())
    with pytest.raises(FrozenInstanceError):
        result.candidate = None
    assert replace(result, elapsed_seconds=99.0).plan_fingerprint == result.plan_fingerprint


@pytest.mark.parametrize(
    "changes",
    (
        {"candidate": None},
        {"candidate": object()},
        {"plan_fingerprint": None},
        {"plan_fingerprint": " "},
        {"stop_reason": SearchStopReason.USER_CANCELLED},
        {"elapsed_seconds": -1.0},
        {"elapsed_seconds": float("inf")},
        {"elapsed_seconds": True},
    ),
)
def test_result_rejects_partial_or_invalid_completion(changes):
    result = construct_initial_plan(*inputs())
    with pytest.raises(ValueError):
        replace(result, **changes)


def test_incomplete_result_cannot_claim_natural_completion_without_a_candidate():
    with pytest.raises(ValueError):
        InitialSolutionResult(None, None, None, 0.0)
