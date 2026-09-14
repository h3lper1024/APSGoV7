"""Second-resolution comparison without rounding production or relaxing safety."""

from dataclasses import replace
from decimal import Decimal as D, localcontext, ROUND_DOWN

import pytest

from apsgo_scheduler.core.chain_order import chain_order_objective_index, refinement_candidate_allowed
from apsgo_scheduler.core.contracts import CoreCandidateSnapshot, RuleScope, sum_weights
from apsgo_scheduler.core.delivery_timing import (
    evaluate_delivery, score_time_seconds, weighted_wait_seconds, score_seconds_to_hours,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.final_audit import audit_core_without_search_cache
from apsgo_scheduler.core.model import SchedulePlan
from apsgo_scheduler.core.neighborhoods import try_complete_candidate
from apsgo_scheduler.core.rules.concrete import DeliveryDuePerformanceRule
from apsgo_scheduler.core.width_optimization import _try_segment_edit, run_width_optimization
from tests.core.graph.test_bipartite_matching import budget
from tests.core.test_backlog_priority_objective import BURDEN, CLEARANCE, tradeoff_case
from tests.core.test_delivery_objective import example, plan

SECOND_KEYS = ("prohibited_violation_count", "prohibited_violation_severity", "underweight_chain_count",
               "underweight_total_gap", CLEARANCE, BURDEN, "inter_chain_width_gap", "generated_virtual_weight", "chain_count")


@pytest.mark.parametrize("hours,seconds", [
    ("0", 0), ("0.000125", 0), ("0.000375", 1), ("0.000625", 2), ("0.000875", 3),
    ("0.00125", 4), ("0.00375", 14),
    ("1.0000000000000000000000001", 3600), ("0.9999999999999999999999999", 3600),
])
def test_second_quantization_is_exact_and_independent_of_callers_context(hours, seconds):
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        assert score_time_seconds(D(hours)) == seconds


@pytest.mark.parametrize("bad", [D(-1), D("NaN"), D("Infinity"), 1.0, None])
def test_invalid_time_rejected(bad):
    with pytest.raises(ValueError):
        score_time_seconds(bad)


def test_weighted_seconds_keep_fractional_weights_and_divide_only_after_sum():
    weight = D("0.123456789012345678901234567890123456789")
    with localcontext() as context:
        context.prec = 2
        product = weighted_wait_seconds(weight, D("0.00125"))
    assert product == D("0.493827156049382715604938271560493827156")
    products = [weighted_wait_seconds(D("1.25"), D("0.000375"))] * 3
    assert sum_weights(products) == D("3.75")
    assert score_seconds_to_hours(sum_weights(products)) == D("0.001041666666666666666666666667")


def test_clock_is_not_rounded_per_node_and_raw_statistics_are_unchanged():
    a, b, virtual, timing = example("2026-05-31", "2026-05-31", "0.0001", "0.0001")
    first, last = replace(a, node_id="first", weight=D(5)), replace(a, node_id="last", weight=D(5))
    p = plan(first, b, last)
    raw = evaluate_delivery(p, timing, details=True)
    new = evaluate_delivery(p, timing, details=True, second_precision=True)
    assert raw.original_completion_hours == new.original_completion_hours
    assert raw.node_times == new.node_times
    assert new.original_completion_hours["a"] == D("0.0002")  # .72 seconds, not zero per node.
    assert new.old_backlog_last_completion_hours == score_seconds_to_hours(D(1))
    assert new.delivery_wait_tardiness_tonne_hours == score_seconds_to_hours(D(30))


def test_subsecond_actual_lateness_is_still_counted_without_a_grace_period():
    a, b, virtual, timing = example("2026-06-01", "2026-06-01", "4", "20.0001")
    result = evaluate_delivery(plan(a, b), timing, second_precision=True)
    assert result.newly_late_original_weight == 20
    assert result.delivery_wait_tardiness_tonne_hours == result.old_backlog_last_completion_hours == 0


def test_wait_is_subtracted_before_rounding_not_difference_of_rounded_endpoints():
    a, b, virtual, timing = example("2026-06-01", "2026-06-01", "0.0001", "0.0001")
    # A timezone-aware fractional start makes the due offset fractional as well.
    from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput, normalize_delivery_timing
    raw = DeliveryTimingInput("2026-06-01T23:59:59.600000+08:00", (
        OrderTimingInput("a", "2026-06-01", D("0.0001")), OrderTimingInput("b", "2026-06-01", D("0.0001"))), {})
    timing = normalize_delivery_timing(raw, (a, b), ())
    result = evaluate_delivery(plan(a, b), timing, second_precision=True)
    assert result.newly_late_original_weight == 20
    assert result.delivery_wait_tardiness_tonne_hours == 0  # .72 - .4 = .32 sec -> zero.


@pytest.mark.parametrize("parameters,version", [
    ({"include_backlog_clearance": True, "score_time_unit": v}, "3") for v in (None, "hour", "seconds", True, 1)
] + [({"score_time_unit": "second"}, "3"), ({"include_backlog_clearance": True}, "3"),
     ({"include_backlog_clearance": True, "score_time_unit": "second"}, "2")])
def test_explicit_precision_requires_approved_version_and_combination(parameters, version):
    with pytest.raises(ValueError):
        DeliveryDuePerformanceRule("delivery", "交期", RuleScope.PLAN, True, version, parameters)


def test_underweight_comes_before_clearance_in_real_node_acceptance():
    _, state, context = tradeoff_case(second_precision=True)
    before = state.current_evaluation
    assert tuple(c.metric_key for c in context.factory.cache.rule_set.quality_spec) == SECOND_KEYS
    assert chain_order_objective_index(context.factory.cache.rule_set) == 4
    assert not _try_segment_edit(state, context, ("width_node_move", 1, 0, 1, 2, 0, 0))
    assert state.current_evaluation == before  # Earlier backlog would create an underweight chain.
    first, last = state.current_plan.chains
    assert try_complete_candidate(state, context, (last, first), affected_chain_ids=(last.chain_id,),
        virtual_sequence=0, action_name="chain_order_relocation", chain_order_only=True)
    assert state.current_evaluation.quality_key[:4] == before.quality_key[:4]
    assert state.current_evaluation.quality_key[4] < before.quality_key[4]


def test_real_underweight_improvement_can_outweigh_worse_clearance():
    _, state, context = tradeoff_case(second_precision=True)
    first, last = state.current_plan.chains
    source = SchedulePlan((replace(first, nodes=(last.nodes[-1], *first.nodes)), replace(last, nodes=last.nodes[:1])))
    state.current_plan = source
    state.current_evaluation = evaluate_plan(source, context.factory.cache.rule_set, context.factory.cache.context)
    before = state.current_evaluation
    assert before.quality_key[2] == 1
    assert try_complete_candidate(state, context, (first, last), affected_chain_ids=(first.chain_id,last.chain_id),
        virtual_sequence=0, action_name="width_node_move", width_optimization_only=True)
    after = state.current_evaluation
    assert after.quality_key[2] == 0 and after.quality_key[4] > before.quality_key[4]
    assert audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, after),
        context.factory.cache.problem, context.factory.cache.rule_set, budget()).report.passed


def test_no_componentwise_underweight_veto_and_no_unscored_tie_break():
    _, state, context = tradeoff_case(second_precision=True)
    before = replace(state.current_evaluation, quality_key=(0,0,2,100,4,400,0,0,2))
    after = replace(before, quality_key=(0,0,1,200,5,800,0,0,2))
    assert after.quality_key < before.quality_key
    assert refinement_candidate_allowed(before, after, context.factory.cache.rule_set)
    # These are comparator unit values, not a fabricated accepted physical candidate.
    before = replace(before, quality_key=(0,0,0,0,score_seconds_to_hours(score_time_seconds(D("1.0000000000000000000000001"))),10,0,0,2))
    after = replace(before, quality_key=(0,0,0,0,score_seconds_to_hours(score_time_seconds(D(1))),11,0,0,2))
    assert not after.quality_key < before.quality_key


def test_refinement_enters_and_independent_audit_detects_forged_second_score():
    _, state, context = tradeoff_case(second_precision=True)
    run_width_optimization(state, context)
    assert context.factory.budget.candidate_check_count <= 100 and state.accepted_move_count > 0
    rules, problem = context.factory.cache.rule_set, context.factory.cache.problem
    snapshot = CoreCandidateSnapshot(state.current_plan, state.current_evaluation)
    assert audit_core_without_search_cache(snapshot, problem, rules, budget()).report.passed
    value = snapshot.search_evaluation
    forged = replace(value, quality_key=(*value.quality_key[:4], value.quality_key[4] + D("1e-25"), *value.quality_key[5:]))
    assert not audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, forged), problem, rules, budget()).report.passed
