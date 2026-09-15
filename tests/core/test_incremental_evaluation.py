"""Exact candidate reuse versus the independent, uncached evaluator."""

from dataclasses import replace
from decimal import Decimal as D, localcontext
from itertools import permutations
import pytest

from apsgo_scheduler.core.evaluation import evaluate_plan, _evaluate_candidate_plan
from apsgo_scheduler.core._rule_runs import RuleRuns
from apsgo_scheduler.core.model import Chain, SchedulePlan, SearchState
from apsgo_scheduler.core.rules.base import ChainRuleSubject, RuleScope
from apsgo_scheduler.core.rules.concrete import (
    HighSurfaceRunCountRule, ContinuousNarrowSteelWeightRule, SameSpecContinuousRealWeightRule,
)
from tests.core.test_plan_evaluation import node, ruleset, context


def test_resources_follow_new_order_and_exact_weights_without_retaining_rejects():
    ctx, active = context(), ruleset()
    nodes = (node("a", weight=D("1e35")), node("b", weight=D("0.000000000001")), node("c"))
    chains = tuple(Chain(n.node_id, (n,), "first") for n in nodes)
    plan = SchedulePlan(chains)
    state = SearchState(plan, evaluate_plan(plan, active, ctx))
    _, previous = _evaluate_candidate_plan(plan, state, active, ctx, None)
    for order in permutations(chains):
        candidate = SchedulePlan(order)
        with localcontext() as numeric:
            numeric.prec = 4
            actual, retained = _evaluate_candidate_plan(candidate, state, active, ctx, previous)
            assert actual == evaluate_plan(candidate, active, ctx)
        assert all(retained.resources[c.chain_id] is previous.resources[c.chain_id] for c in chains)
        assert retained.runs.previous is None
    changed = SchedulePlan((replace(chains[0], assigned_period="later"), *chains[1:]))
    actual, retained = _evaluate_candidate_plan(changed, state, active, ctx, previous)
    assert actual == evaluate_plan(changed, active, ctx)
    assert retained.resources["a"] is not previous.resources["a"]


def test_run_reuse_keeps_boundaries_and_does_not_confuse_replaced_nodes():
    rules = (
        HighSurfaceRunCountRule("surface", "surface", RuleScope.CHAIN, True, "1",
                                {"surface_grades": ("FC",), "max_run_count": 1}),
        ContinuousNarrowSteelWeightRule("narrow", "narrow", RuleScope.CHAIN, True, "1",
                                        {"grade_class": "IF", "width_upper_exclusive": D(1200), "max_real_weight": D(10)}),
        SameSpecContinuousRealWeightRule("same", "same", RuleScope.CHAIN, True, "1",
                                         {"group_by_fields": ("grade",), "max_real_weight": D(10)}),
    )
    nodes = tuple(replace(node(key), grade="SPHC", rule_attributes={"surface_grade": grade, "grade_class": "IF"})
                  for key, grade in (("a", "FC"), ("b", "FC"), ("c", "")))
    old = RuleRuns()
    ctx = context()
    for rule in rules:
        subject = ChainRuleSubject("old", Chain("old", nodes, "first"))
        assert rule.evaluate(subject, ctx, _run_cache=old) == rule.evaluate(subject, ctx)
    for order in permutations(nodes):
        current = RuleRuns(old)
        for rule in rules:
            subject = ChainRuleSubject("new", Chain("new", order, "first"))
            assert rule.evaluate(subject, ctx, _run_cache=current) == rule.evaluate(subject, ctx)
        assert current.key_hits == len(rules) * len(nodes)
    modified = (replace(nodes[0], rule_attributes={"surface_grade": "", "grade_class": "IF"}), *nodes[1:])
    current = RuleRuns(old)
    subject = ChainRuleSubject("changed", Chain("changed", modified, "first"))
    assert rules[0].evaluate(subject, ctx, _run_cache=current) == rules[0].evaluate(subject, ctx)
    assert current.key_hits == 2


@pytest.mark.parametrize("second", (False, True))
@pytest.mark.parametrize("hours", ("0.000125", "0.00375", "4", "1.2345678901234567890123456789", "1e35"))
def test_delivery_reuse_exact_clock_and_all_fragments(second, hours):
    from apsgo_scheduler.core.delivery_timing import evaluate_delivery, _evaluate_delivery_reused
    from tests.core.test_delivery_objective import example, plan
    a, b, virtual, timing = example(hours_a=hours)
    pieces = (replace(a, node_id="a-left", weight=D(3)), replace(a, node_id="a-right", weight=D(7)))
    original = plan(*pieces, virtual, b)
    previous = _evaluate_delivery_reused(original, timing, second)
    for ordered in permutations((*pieces, virtual, b)):
        candidate = plan(*ordered)
        reused = _evaluate_delivery_reused(candidate, timing, second, previous)
        expected = evaluate_delivery(candidate, timing, second_precision=second, details=True)
        assert reused.performance == replace(expected, node_times=())
        assert tuple(v.as_tuple() for v in reused.ends) == tuple(row[2].as_tuple() for row in expected.node_times)
        assert reused.reused_durations == 4
    removed = plan(b, *pieces)
    reused = _evaluate_delivery_reused(removed, timing, second, previous)
    assert reused.performance == evaluate_delivery(removed, timing, second_precision=second)
    assert reused.ends[-1] == evaluate_delivery(removed, timing, details=True).node_times[-1][2]
    changed_timing = replace(timing, virtual_hours_per_tonne={"prototype": D(2)})
    assert _evaluate_delivery_reused(original, changed_timing, second, previous).reused_durations == 0
    with pytest.raises(ValueError, match="conserved"):
        _evaluate_delivery_reused(plan(pieces[0], b), timing, second, previous)


def test_delivery_skips_only_exactly_matching_prefix_and_suffix():
    from apsgo_scheduler.core.delivery_timing import _evaluate_delivery_reused
    from tests.core.test_delivery_objective import example, plan
    a, b, virtual, timing = example()
    left, right = replace(a, node_id="left", weight=D(5)), replace(a, node_id="right", weight=D(5))
    previous = _evaluate_delivery_reused(plan(virtual, left, right, b), timing, True)
    reused = _evaluate_delivery_reused(plan(virtual, right, left, b), timing, True, previous)
    assert (reused.reused_prefix, reused.reused_suffix, reused.reused_orders) == (1, 1, 2)
