"""Bounded whole-chain relocation: stable periods, exact objective and shared allowance."""

from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.chain_order import (
    chain_order_objective_index,
    has_inter_chain_width_rule,
    stable_group_plan,
)
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, SchedulePlan, SearchState
from apsgo_scheduler.core.neighborhoods import (
    SearchContext,
    improve_chain_order,
    try_complete_candidate,
)
from apsgo_scheduler.core.rules.base import (
    NumericProjection,
    QualityAggregation,
    QualityDirection,
    RuleEvaluationContext,
)
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.rules.test_inter_chain_width_gap import METRIC, rule, with_gap
from tests.core.search.test_single_real_node_relocation import Stop
from tests.core.test_quality_key import SeverityRule, criterion, ruleset
from tests.core.test_solver_orchestration import solver_case

D = Decimal


def chain(name, width=1000, period="period", **changes):
    member = replace(node(name, width=str(width), weight="800"), source_period=period, **changes)
    return Chain(name, (member,), period)


def search_case(chains=None, *, active=None, periods=("period",), limit=100, runtime=None):
    chains = (
        tuple(chains)
        if chains is not None
        else tuple(chain(name, width) for name, width in zip("ABC", (1000, 1500, 1200)))
    )
    active = with_gap(ruleset()) if active is None else active
    problem, active, policy, default_runtime = solver_case(
        nodes=tuple(item for current in chains for item in current.nodes),
        rule_set=active,
        period_order=periods,
        limit=limit,
    )
    runtime = default_runtime if runtime is None else runtime
    policy = replace(policy, candidate_check_limit=runtime.candidate_check_limit)
    evaluation_context = RuleEvaluationContext(
        periods, {period: i for i, period in enumerate(periods)}, ()
    )
    context = SearchContext(
        VirtualFactory(RuleEdgeDecisionCache(problem, active, evaluation_context), runtime), policy
    )
    plan = SchedulePlan(chains)
    return SearchState(plan, evaluate_plan(plan, active, evaluation_context)), context


def signature(plan):
    return tuple(item.chain_id for item in plan.chains)


@pytest.mark.parametrize("mode", ("absent", "disabled", "diagnostic", "objective"))
def test_enabled_rule_and_bound_objective_are_distinct_and_do_not_add_hidden_work(mode):
    base = ruleset()
    if mode == "objective":
        active = with_gap(base)
    else:
        items = () if mode == "absent" else (rule(enabled=mode != "disabled"),)
        active = replace(base, rules=base.rules + items, fingerprint=f"test-{mode}")
    assert has_inter_chain_width_rule(active) is (mode in {"diagnostic", "objective"})
    assert chain_order_objective_index(active) == (6 if mode == "objective" else None)
    state, context = search_case(active=active)
    before = fingerprint(state)
    improve_chain_order(state, context)
    if mode != "objective":
        a, b, c = state.current_plan.chains
        assert not try_complete_candidate(
            state,
            context,
            (a, c, b),
            affected_chain_ids=("B",),
            virtual_sequence=0,
            action_name="synthetic_order",
            chain_order_only=True,
        )
        assert fingerprint(state) == before
        assert context.factory.budget.candidate_check_count == 0
        assert context.complete_candidate_evaluation_count == 0
        assert context.accepted_move_traces == ()


@pytest.mark.parametrize(
    "change",
    (
        {"direction": QualityDirection.MAXIMIZE},
        {"aggregation": QualityAggregation.MAXIMUM},
        {"numeric_projection": NumericProjection.REFERENCE_FLOAT_ROUND_6},
    ),
)
def test_other_metric_bindings_do_not_silently_enable_the_minimum_exact_sum_stage(change):
    active = with_gap(ruleset())
    active = replace(
        active, quality_spec=(*active.quality_spec[:-1], replace(active.quality_spec[-1], **change))
    )
    assert has_inter_chain_width_rule(active)
    assert chain_order_objective_index(active) is None
    state, context = search_case(active=active)
    before = fingerprint(state)
    improve_chain_order(state, context)
    assert fingerprint(state) == before
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 0
    )


def test_stable_grouping_uses_task_order_without_renaming_or_copying_chains():
    periods = ("Z-first", "A-next", "M-empty", "B-last", "Q-unused")
    current = SchedulePlan(
        (
            chain("z-late", period="B-last"),
            chain("z-middle", period="A-next"),
            chain("first", period="Z-first"),
            chain("a-middle", period="A-next"),
        )
    )
    before = fingerprint(current)
    index = dict(zip(periods, range(len(periods))))
    result = stable_group_plan(current, index)
    assert signature(result) == ("first", "z-middle", "a-middle", "z-late")
    original = {item.chain_id: item for item in current.chains}
    assert all(item is original[item.chain_id] for item in result.chains)
    assert fingerprint(current) == before
    assert stable_group_plan(result, index) == result
    with pytest.raises(ValueError, match="period"):
        stable_group_plan(current, {"Z-first": 0})


def test_first_improvement_restarts_from_new_order_and_counts_each_actual_move_once(monkeypatch):
    state, context = search_case()
    original = {item.chain_id: item for item in state.current_plan.chains}
    before_quality = state.current_evaluation.quality_key
    problem_before = fingerprint(context.factory.cache.problem)

    def forbidden(*_):
        raise AssertionError("pure chain relocation must not create or test a new internal edge")

    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", forbidden)
    assert improve_chain_order(state, context) is state
    assert signature(state.current_plan) == ("A", "C", "B")
    assert state.current_evaluation.metrics[METRIC] == D(500)
    assert state.current_evaluation.quality_key[:-1] == before_quality[:-1]
    assert all(item is original[item.chain_id] for item in state.current_plan.chains)
    assert state.virtual_sequence == state.split_sequence == 0
    assert state.accepted_move_count == 2
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 9
    )
    assert tuple(item.candidate_check_count for item in context.accepted_move_traces) == (1, 3)
    assert tuple(item.affected_chain_ids for item in context.accepted_move_traces) == (
        ("A",),
        ("B",),
    )
    assert all(
        item.action_name == "chain_order_relocation" for item in context.accepted_move_traces
    )
    assert tuple(
        (item.quality_before[-1], item.quality_after[-1]) for item in context.accepted_move_traces
    ) == ((D(800), D(700)), (D(700), D(500)))
    assert all(
        item.quality_before[:-1] == item.quality_after[:-1] for item in context.accepted_move_traces
    )
    assert fingerprint(context.factory.cache.problem) == problem_before
    assert state.current_evaluation == evaluate_plan(
        state.current_plan, context.factory.cache.rule_set, context.factory.cache.context
    )
    assert context.factory.budget.stop_reason is None


def test_equal_widths_enumerate_six_non_original_same_period_moves_without_accepting(monkeypatch):
    state, context = search_case(tuple(chain(name) for name in "ABC"))
    before = fingerprint(state)
    observed = []
    original = neighborhoods.try_complete_candidate

    def record(current, current_context, chains, **options):
        observed.append(
            (
                tuple(item.chain_id for item in chains),
                current_context.factory.budget.candidate_check_count,
            )
        )
        assert options["chain_order_only"] is True
        return original(current, current_context, chains, **options)

    monkeypatch.setattr(neighborhoods, "try_complete_candidate", record)
    improve_chain_order(state, context)
    assert observed == [
        (("B", "A", "C"), 1),
        (("B", "C", "A"), 2),
        (("B", "A", "C"), 3),
        (("A", "C", "B"), 4),
        (("C", "A", "B"), 5),
        (("A", "C", "B"), 6),
    ]
    assert context.complete_candidate_evaluation_count == 6
    assert fingerprint(state) == before and context.accepted_move_traces == ()


def test_same_period_move_recomputes_both_cross_period_boundaries():
    periods = ("Z-first", "A-middle", "M-empty", "B-last")
    state, context = search_case(
        (
            chain("previous", 1000, "Z-first"),
            chain("C", 1600, "A-middle"),
            chain("B", 1200, "A-middle"),
            chain("next", 2000, "B-last"),
        ),
        periods=periods,
    )
    original = {item.chain_id: item for item in state.current_plan.chains}
    assert state.current_evaluation.metrics[METRIC] == D(1800)
    improve_chain_order(state, context)
    assert signature(state.current_plan) == ("previous", "B", "C", "next")
    assert state.current_evaluation.metrics[METRIC] == D(1000)
    assert state.accepted_move_count == 1
    assert context.accepted_move_traces[0].candidate_check_count == 1
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 3
    )
    assert all(item is original[item.chain_id] for item in state.current_plan.chains)


def test_generic_quality_position_is_found_without_assuming_seven_entries():
    active = with_gap(ruleset(criteria=()))
    active = replace(active, quality_spec=active.quality_spec + (criterion("chain_count"),))
    assert chain_order_objective_index(active) == 2
    state, context = search_case(active=active)
    before = state.current_evaluation.quality_key
    improve_chain_order(state, context)
    assert len(state.current_evaluation.quality_key) == 4
    assert state.current_evaluation.quality_key[:2] == before[:2]
    assert state.current_evaluation.quality_key[2] == D(500) < before[2]
    assert state.current_evaluation.quality_key[3:] == before[3:]
    assert state.accepted_move_count == 2


def test_pure_order_rejects_real_float_prefix_drift_even_when_full_quality_improves():
    severity = SeverityRule("severity", "severity", RuleScope.CHAIN, True, "1", {})
    active = with_gap(ruleset((severity,)))
    state, context = search_case(
        (
            chain("A", 1000, rule_attributes={"severity": D(1)}),
            chain("B", 1500, rule_attributes={"severity": D(1)}),
            chain("C", 1200, rule_attributes={"severity": D("1e16")}),
        ),
        active=active,
    )
    before = fingerprint(state)
    candidate = (state.current_plan.chains[2], *state.current_plan.chains[:2])
    evaluation = evaluate_plan(SchedulePlan(candidate), active, context.factory.cache.context)
    assert evaluation.quality_key[1] < state.current_evaluation.quality_key[1]
    assert evaluation.quality_key[-1] == D(700) < state.current_evaluation.quality_key[-1]
    assert evaluation.quality_key < state.current_evaluation.quality_key
    assert context.factory.budget.consume_candidate_check()
    assert not try_complete_candidate(
        state,
        context,
        candidate,
        affected_chain_ids=("C",),
        virtual_sequence=0,
        action_name="synthetic_order",
        chain_order_only=True,
    )
    assert context.complete_candidate_evaluation_count == 1
    assert fingerprint(state) == before and context.accepted_move_traces == ()


@pytest.mark.parametrize("change", ("period", "identity", "node_value", "virtual_sequence"))
def test_pure_mode_rejects_any_structure_or_sequence_rewrite_before_evaluation(change):
    state, context = search_case()
    before = fingerprint(state)
    a, b, c = state.current_plan.chains
    sequence = 0
    if change == "period":
        a = replace(a, assigned_period="caller-placeholder")
    elif change == "identity":
        a = replace(a, chain_id="new-id")
    elif change == "node_value":
        a = replace(a, nodes=(replace(a.nodes[0], width=D(999)),))
    else:
        sequence = 1
    assert context.factory.budget.consume_candidate_check()
    assert not try_complete_candidate(
        state,
        context,
        (a, c, b),
        affected_chain_ids=("A", "B", "C"),
        virtual_sequence=sequence,
        action_name="synthetic_order",
        chain_order_only=True,
    )
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before and context.accepted_move_traces == ()


@pytest.mark.parametrize("flag", (None, 1))
def test_pure_mode_flag_must_be_an_actual_boolean(flag):
    state, context = search_case()
    before = fingerprint(state)
    with pytest.raises(ValueError, match="boolean"):
        try_complete_candidate(
            state,
            context,
            state.current_plan.chains,
            affected_chain_ids=("A",),
            virtual_sequence=0,
            action_name="synthetic_order",
            chain_order_only=flag,
        )
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before


def test_pure_mode_rejects_duplicate_chain_identity_even_when_dictionary_matches():
    state, context = search_case()
    before = fingerprint(state)
    candidate = (*state.current_plan.chains, state.current_plan.chains[-1])
    assert {item.chain_id: item for item in candidate} == {
        item.chain_id: item for item in state.current_plan.chains
    }
    assert not try_complete_candidate(
        state,
        context,
        candidate,
        affected_chain_ids=("A", "B", "C"),
        virtual_sequence=0,
        action_name="synthetic_order",
        chain_order_only=True,
    )
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before and context.accepted_move_traces == ()


def test_pure_mode_rechecks_chain_mapping_after_period_normalization():
    state, context = search_case(
        (
            replace(chain("A", 1000, "later"), assigned_period="early"),
            chain("B", 1500, "later"),
            chain("C", 1200, "later"),
        ),
        periods=("early", "later"),
    )
    before = fingerprint(state)
    a, b, c = state.current_plan.chains
    assert a.assigned_period == "early" and a.nodes[0].source_period == "later"
    assert not try_complete_candidate(
        state,
        context,
        (b, a, c),
        affected_chain_ids=("A",),
        virtual_sequence=0,
        action_name="synthetic_order",
        chain_order_only=True,
    )
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before and context.accepted_move_traces == ()


@pytest.mark.parametrize("stop_kind", ("cancel", "time"))
@pytest.mark.parametrize("when", ("before", "after_evaluation"))
def test_interruption_never_publishes_an_unfinished_order(stop_kind, when, monkeypatch):
    stop = Stop()
    runtime = budget(
        candidate_check_limit=100,
        cancellation=stop if stop_kind == "cancel" else None,
        clock=lambda: 100.0 if stop.active and stop_kind == "time" else 1.0,
    )
    state, context = search_case(runtime=runtime)
    before = fingerprint(state)
    if when == "before":
        stop.active = True
    else:
        original = neighborhoods.evaluate_plan

        def interrupt(*args):
            result = original(*args)
            stop.active = True
            return result

        monkeypatch.setattr(neighborhoods, "evaluate_plan", interrupt)
    improve_chain_order(state, context)
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if stop_kind == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )
    assert (
        runtime.candidate_check_count
        == context.complete_candidate_evaluation_count
        == int(when != "before")
    )
    assert fingerprint(state) == before and context.accepted_move_traces == ()


def test_zero_allowance_stops_before_candidate_entry_or_evaluation(monkeypatch):
    state, context = search_case(limit=0)
    before = fingerprint(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("candidate work cannot precede its allowance")

    monkeypatch.setattr(neighborhoods, "try_complete_candidate", forbidden)
    improve_chain_order(state, context)
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 0
    )
    assert fingerprint(state) == before


@pytest.mark.parametrize("periods", (("only",), ("Z-first", "M-empty", "A-last")))
def test_no_same_period_target_needs_no_allowance_and_does_not_claim_truncation(periods):
    state, context = search_case(
        tuple(
            chain(f"c{i}", period=period) for i, period in enumerate(periods) if period != "M-empty"
        ),
        periods=periods,
        limit=0,
    )
    before = fingerprint(state)
    improve_chain_order(state, context)
    assert context.factory.budget.stop_reason is None
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 0
    )
    assert fingerprint(state) == before and context.accepted_move_traces == ()


def test_exact_allowance_keeps_the_last_accepted_order_and_does_not_finish_for_free():
    state, context = search_case(limit=3)
    improve_chain_order(state, context)
    assert signature(state.current_plan) == ("A", "C", "B")
    assert state.current_evaluation.metrics[METRIC] == D(500)
    assert state.accepted_move_count == 2
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 3
    )
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert tuple(item.candidate_check_count for item in context.accepted_move_traces) == (1, 3)
    before = fingerprint(state)
    improve_chain_order(state, context)
    assert fingerprint(state) == before
