"""Whole-chain search follows reference enumeration and first-improvement semantics."""

from dataclasses import replace
from decimal import Decimal, Inexact, localcontext

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, SchedulePlan, SearchState
from apsgo_scheduler.core.neighborhoods import SearchContext, improve_whole_chain
from apsgo_scheduler.core.rules.base import (
    NumericProjection,
    QualityAggregation,
    QualityCriterion,
    QualityDirection,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleViolation,
)
from apsgo_scheduler.core.rules.concrete import (
    ChainWeightRangeRule,
    ContinuousNarrowSteelWeightRule,
    VirtualOutputRatioRule,
)
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.app.test_input_normalizer import make_request
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.search.test_virtual_material_factory import make_factory, prototype

D = Decimal


def weight_rule(*, minimum="0", maximum="100", enabled=True):
    return ChainWeightRangeRule(
        "weight",
        "weight",
        RuleScope.CHAIN,
        enabled,
        "1",
        {"min_weight": D(minimum), "max_weight": D(maximum), "target_weight": D(maximum)},
    )


def make_search(
    chains,
    *,
    rule_items=(),
    prototypes=(),
    allowed_edges=None,
    chain_count=False,
    limit=1000,
    slack="0",
    runtime=None,
):
    runtime = budget(candidate_check_limit=limit) if runtime is None else runtime
    factory = make_factory(
        prototypes,
        nodes=tuple(item for chain in chains for item in chain.nodes),
        allowed_edges=allowed_edges,
        rule_items=rule_items,
        runtime=runtime,
    )
    rule_set = factory.cache.rule_set
    if chain_count:
        quality = rule_set.quality_spec + (
            QualityCriterion(
                "chains",
                "chain_count",
                QualityDirection.MINIMIZE,
                QualityAggregation.NAMED_VALUE,
                NumericProjection.EXACT_DECIMAL,
            ),
        )
        rule_set = replace(
            rule_set, quality_spec=quality, fingerprint=fingerprint((rule_set.rules, quality))
        )
        factory = VirtualFactory(
            RuleEdgeDecisionCache(factory.cache.problem, rule_set, factory.cache.context),
            runtime,
        )
    policy = replace(
        make_request().policy,
        candidate_check_limit=runtime.candidate_check_limit,
        whole_chain_pair_scan_slack_weight=D(slack),
    )
    context = SearchContext(factory, policy)
    plan = SchedulePlan(tuple(chains))
    state = SearchState(plan, evaluate_plan(plan, rule_set, factory.cache.context))
    return state, context


def chain(name, *members):
    return Chain(name, tuple(members), "period")


def record_candidates(monkeypatch):
    observed = []
    original = neighborhoods.try_complete_candidate

    def record(state, context, chains, **kwargs):
        observed.append(
            (
                kwargs["affected_chain_ids"],
                kwargs["action_name"],
                tuple(item.node_id for item in chains[-1].nodes),
                context.factory.budget.candidate_check_count,
            )
        )
        return original(state, context, chains, **kwargs)

    monkeypatch.setattr(neighborhoods, "try_complete_candidate", record)
    return observed


def test_underweight_donors_first_then_remaining_donors_and_targets_in_current_order(monkeypatch):
    initial = (
        chain("A", node("a", weight="60")),
        chain("B", node("b", weight="20")),
        chain("C", node("c", weight="60")),
    )
    state, context = make_search(initial, rule_items=(weight_rule(minimum="50", maximum="500"),))
    observed = record_candidates(monkeypatch)
    result = improve_whole_chain(state, context)
    assert result is state and state.current_plan.chains == initial
    pairs = (("B", "A"), ("B", "C"), ("A", "B"), ("A", "C"), ("C", "A"), ("C", "B"))
    assert tuple(item[0] for item in observed) == tuple(pair for pair in pairs for _ in range(4))
    assert (
        tuple(item[1] for item in observed)
        == (
            "whole_chain_append",
            "whole_chain_prepend",
            "whole_chain_insertion",
            "whole_chain_insertion",
        )
        * 6
    )
    for offset in range(0, len(observed), 4):
        first, second, at_head, at_tail = observed[offset : offset + 4]
        assert first[2] == at_tail[2] and second[2] == at_head[2]
    assert tuple(item[3] for item in observed) == tuple(range(1, 25))
    assert context.complete_candidate_evaluation_count == 24
    assert context.factory.budget.stop_reason is None
    assert state.accepted_move_count == 0 and context.accepted_move_traces == ()


def test_donor_variant_is_outer_loop_target_variant_inner_and_endpoint_duplicates_count(
    monkeypatch,
):
    state, context = make_search(
        (chain("D", node("d0"), node("d1")), chain("T", node("t0"), node("t1"))), limit=20
    )
    observed = record_candidates(monkeypatch)
    improve_whole_chain(state, context)
    expected = []
    for donor in (("d0", "d1"), ("d1", "d0")):
        for target in (("t0", "t1"), ("t1", "t0")):
            expected.extend(
                (
                    target + donor,
                    donor + target,
                    donor + target,
                    target[:1] + donor + target[1:],
                    target + donor,
                )
            )
    assert tuple(item[2] for item in observed) == tuple(expected)
    assert all(item[0] == ("D", "T") for item in observed)
    assert context.factory.budget.candidate_check_count == 20
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert context.complete_candidate_evaluation_count == 20


class OrderProfileRule(Rule):
    supported_scope = RuleScope.CHAIN

    def required_fields(self):
        return ()

    def evaluate(self, subject, context):
        signature = "".join(item.node_id for item in subject.chain.nodes)
        count, severity = self.parameters["profiles"].get(signature, (3, D(100)))
        return RuleContribution(
            tuple(
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    f"{subject.subject_id}:{index}",
                    "ordered_test_profile",
                    "Synthetic order-dependent violation.",
                    RuleDisposition.PROHIBITED,
                    severity,
                )
                for index in range(count)
            ),
            (),
        )


@pytest.mark.parametrize(
    "original,reverse,reversal_allowed,checks",
    (
        ((2, D("0.5")), (1, D(10)), True, 18),
        ((1, D(10)), (2, D("0.1")), False, 9),
    ),
)
def test_reversal_profile_is_lexicographic_not_componentwise(
    original, reverse, reversal_allowed, checks, monkeypatch
):
    rule = OrderProfileRule(
        "profile",
        "profile",
        RuleScope.CHAIN,
        True,
        "1",
        {"profiles": {"ab": original, "ba": reverse, "c": (0, D(0))}},
    )
    state, context = make_search(
        (chain("A", node("a"), node("b")), chain("C", node("c"))), rule_items=(rule,)
    )
    observed = record_candidates(monkeypatch)
    improve_whole_chain(state, context)
    assert (("c", "b", "a") in [item[2] for item in observed]) is reversal_allowed
    assert context.factory.budget.candidate_check_count == checks
    assert state.accepted_move_count == 0


def test_first_improvement_appends_merged_chain_and_restarts_from_new_first_donor():
    initial = tuple(chain(name.upper(), node(name)) for name in "abc")
    state, context = make_search(initial, chain_count=True)
    improve_whole_chain(state, context)
    assert len(state.current_plan.chains) == 1
    (final,) = state.current_plan.chains
    assert final.chain_id == "B" and tuple(item.node_id for item in final.nodes) == ("b", "a", "c")
    assert tuple(item.affected_chain_ids for item in context.accepted_move_traces) == (
        ("A", "B"),
        ("C", "B"),
    )
    assert tuple(item.candidate_check_count for item in context.accepted_move_traces) == (1, 2)
    assert state.accepted_move_count == context.complete_candidate_evaluation_count == 2
    assert context.factory.budget.stop_reason is None
    assert tuple(tuple(item.node_id for item in original.nodes) for original in initial) == (
        ("a",),
        ("b",),
        ("c",),
    )


def test_fewer_narrow_run_violations_can_improve_while_total_severity_increases():
    narrow = ContinuousNarrowSteelWeightRule(
        "narrow",
        "narrow",
        RuleScope.CHAIN,
        True,
        "1",
        {"grade_class": "IF", "width_upper_exclusive": D(1400), "max_real_weight": D(500)},
    )
    nodes = tuple(
        replace(node(name, weight=weight), rule_attributes={"grade_class": "IF"})
        for name, weight in (("a", "570.3"), ("b", "600"))
    )
    state, context = make_search(
        tuple(chain(item.node_id, item) for item in nodes), rule_items=(narrow,)
    )
    before = state.current_evaluation.quality_key
    improve_whole_chain(state, context)
    assert before == (2, D("170.3"))
    assert state.current_evaluation.quality_key == (1, D("670.3"))
    assert state.accepted_move_count == 1
    assert context.factory.budget.candidate_check_count == 1


@pytest.mark.parametrize(
    "second_weight,expected_checks", (("80", 8), ("80.000001", 8), ("80.000001001", 0))
)
def test_pair_maximum_plus_slack_epsilon_is_checked_before_candidate_count(
    second_weight, expected_checks
):
    state, context = make_search(
        (chain("A", node("a", weight="60")), chain("B", node("b", weight=second_weight))),
        rule_items=(weight_rule(),),
        slack="40",
    )
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        decimal_context.traps[Inexact] = True
        improve_whole_chain(state, context)
    assert context.factory.budget.candidate_check_count == expected_checks
    assert context.complete_candidate_evaluation_count == 0
    assert state.accepted_move_count == 0 and context.factory.budget.stop_reason is None


def test_disabled_chain_weight_rule_has_no_pair_filter_or_merged_weight_filter():
    state, context = make_search(
        (chain("A", node("a", weight="100")), chain("B", node("b", weight="100"))),
        rule_items=(weight_rule(enabled=False),),
        chain_count=True,
    )
    improve_whole_chain(state, context)
    assert state.accepted_move_count == 1
    assert state.current_plan.chains[0].total_weight == D(200)
    assert context.factory.budget.candidate_check_count == 1


@pytest.mark.parametrize("filter_kind", ("chain_weight", "virtual_ratio"))
def test_bridged_weight_and_virtual_ratio_rejections_are_after_candidate_count(filter_kind):
    items = (
        (weight_rule(),)
        if filter_kind == "chain_weight"
        else (
            VirtualOutputRatioRule(
                "ratio", "ratio", RuleScope.PLAN, True, "1", {"max_ratio": D("0.1")}
            ),
        )
    )
    nodes = tuple(replace(node(name, weight="50"), grade=name) for name in "ab")
    state, context = make_search(
        tuple(chain(item.node_id, item) for item in nodes),
        rule_items=items,
        prototypes=(prototype("p"),),
        allowed_edges={("a", "p"), ("p", "b"), ("b", "p"), ("p", "a")},
    )
    initial = state.current_plan
    improve_whole_chain(state, context)
    assert context.factory.budget.candidate_check_count == 8
    assert context.complete_candidate_evaluation_count == 0
    assert state.current_plan is initial and state.virtual_sequence == 0
    assert context.factory.budget.stop_reason is None


def insertion_search(*, runtime=None):
    nodes = tuple(replace(node(name), grade=name) for name in ("d", "t0", "t1"))
    return make_search(
        (chain("D", nodes[0]), chain("T", *nodes[1:])),
        prototypes=(prototype("p"), prototype("q")),
        allowed_edges={("t0", "t1"), ("t0", "p"), ("p", "d"), ("d", "q"), ("q", "t1")},
        chain_count=True,
        runtime=runtime,
    )


def test_middle_insertion_bridges_both_boundaries_with_continuous_private_numbers():
    state, context = insertion_search()
    improve_whole_chain(state, context)
    (result,) = state.current_plan.chains
    assert tuple(item.grade for item in result.nodes) == ("t0", "p", "d", "q", "t1")
    assert tuple(
        item.virtual_lineage.accepted_sequence for item in result.nodes if item.virtual_lineage
    ) == (1, 2)
    assert state.virtual_sequence == 2 and state.accepted_move_count == 1
    (trace,) = context.accepted_move_traces
    assert trace.action_name == "whole_chain_insertion" and trace.candidate_check_count == 4
    assert trace.affected_chain_ids == ("D", "T")


def test_cancel_after_first_insertion_bridge_cannot_publish_partial_bridge_or_numbers(monkeypatch):
    class Cancellation:
        cancelled = False

        def is_cancelled(self):
            return self.cancelled

    cancellation = Cancellation()
    state, context = insertion_search(
        runtime=budget(candidate_check_limit=1000, cancellation=cancellation)
    )
    initial = fingerprint(state)
    original = VirtualFactory.bridge

    def interrupt_after_first(self, left, right, **kwargs):
        result = original(self, left, right, **kwargs)
        if (left.node_id, right.node_id) == ("t0", "d") and result:
            cancellation.cancelled = True
        return result

    monkeypatch.setattr(VirtualFactory, "bridge", interrupt_after_first)
    improve_whole_chain(state, context)
    assert cancellation.cancelled
    assert fingerprint(state) == initial
    assert context.factory.budget.candidate_check_count == 4
    assert context.factory.budget.stop_reason is SearchStopReason.USER_CANCELLED
    assert context.complete_candidate_evaluation_count == 0
    assert context.accepted_move_traces == ()
