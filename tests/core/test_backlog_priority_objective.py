"""Confirmed backlog-first policy, using real evaluation, candidate acceptance and audit."""

from dataclasses import replace
from decimal import Decimal as D

import pytest

from apsgo_scheduler.app.delivery_request import with_delivery_objective
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.chain_order import chain_order_objective_index, refinement_candidate_allowed
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import CoreCandidateSnapshot, RuleScope
from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput, evaluate_delivery, normalize_delivery_timing
from apsgo_scheduler.core.evaluation import _quality_key, evaluate_plan
from apsgo_scheduler.core.final_audit import audit_core_without_search_cache
from apsgo_scheduler.core.model import Chain, SchedulePlan, SearchState
from apsgo_scheduler.core.neighborhoods import SearchContext, try_complete_candidate
from apsgo_scheduler.core.rules.base import PlanRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import DeliveryDuePerformanceRule
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.width_optimization import _try_segment_edit, run_width_optimization
from tests.app.test_input_normalizer import make_order, make_spec
from tests.core.graph.test_bipartite_matching import budget
from tests.core.test_delivery_objective import example, plan
from tests.core.test_delivery_search_audit import search_case

CLEARANCE = "old_backlog_last_completion_hours"
BURDEN = "delivery_wait_tardiness_tonne_hours"
NEW_KEYS = ("prohibited_violation_count", "prohibited_violation_severity", CLEARANCE, BURDEN,
            "underweight_chain_count", "underweight_total_gap", "inter_chain_width_gap",
            "generated_virtual_weight", "chain_count")


@pytest.mark.parametrize("due_a,due_b,expected", [
    ("2026-06-01", "2026-06-30", 0), ("2026-05-31", "2026-06-01", 10),
    ("2026-05-31", "2026-05-30", 31),
])
def test_clearance_is_backlog_maximum_not_total_or_weighted_mean(due_a, due_b, expected):
    a, b, virtual, timing = example(due_a, due_b)
    result = evaluate_delivery(plan(a, virtual, b), timing)
    assert result.old_backlog_last_completion_hours == expected


def test_last_backlog_fragment_and_intervening_virtual_time():
    a, b, virtual, timing = example()
    first, last = replace(a, node_id="a-first", weight=D(1)), replace(a, node_id="a-last", weight=D(9))
    result = evaluate_delivery(plan(first, b, virtual, last), timing)
    assert result.original_completion_hours["a"] == result.old_backlog_last_completion_hours == 31
    assert result.delivery_wait_tardiness_tonne_hours == 310


@pytest.mark.parametrize("start,backlog", [("2026-06-01T00:00:00+08:00", True),
    ("2026-06-01T01:02:03.123456+08:00", True), ("2026-05-31T23:59:59+08:00", False)])
def test_clearance_hours_remain_relative_to_exact_start(start, backlog):
    a, b, virtual, _ = example()
    raw = DeliveryTimingInput(start, (OrderTimingInput("a", "2026-05-31", D(10)),
                                    OrderTimingInput("b", "2026-06-30", D(20))), {})
    timing = normalize_delivery_timing(raw, (a, b), ())
    assert (timing.orders["a"].due_hours <= 0) is backlog
    assert evaluate_delivery(plan(b, a), timing).old_backlog_last_completion_hours == (30 if backlog else 0)


@pytest.mark.parametrize("parameters", [{"include_backlog_clearance": v} for v in (None, 0, 1, "true", [], {})] + [{"unexpected": True}])
def test_rule_rejects_invalid_enabled_options(parameters):
    with pytest.raises(ValueError, match="boolean include_backlog_clearance"):
        DeliveryDuePerformanceRule("delivery", "交期", RuleScope.PLAN, True, "2", parameters)


def test_rule_keeps_unscored_tonnage_and_disabled_semantics():
    a, b, virtual, timing = example()
    rule = DeliveryDuePerformanceRule("delivery", "交期", RuleScope.PLAN, True, "2", {"include_backlog_clearance": True})
    ctx = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",), timing)
    subject = PlanRuleSubject("plan", plan(a, virtual, b), None)
    assert {m.metric_key: m.value for m in rule.evaluate(subject, ctx).metrics} == {
        "newly_late_original_weight": 20, BURDEN: 240, CLEARANCE: 10}
    assert not replace(rule, enabled=False).evaluate(subject, replace(ctx, delivery_timing=None)).metrics
    assert len(replace(rule, parameters={}).metric_keys()) == 2


def tradeoff_case(*, backlog_priority=True, backlog=True, minimum="800", second_precision=False):
    base, _, _ = search_case(delivery=False, allow_underweight=True)
    weight, width = base.rule_set_spec.rules
    spec = make_spec(rules=(replace(weight, parameters={**weight.parameters, "min_weight": D(minimum)}), width),
                     quality_spec=base.rule_set_spec.quality_spec,
                     allowed_final_deviation_codes=base.rule_set_spec.allowed_final_deviation_codes)
    spec = with_delivery_objective(spec, include_backlog_clearance=backlog_priority, second_precision=second_precision)
    orders = tuple(make_order(i, weight=D(w), width=D(1000), source_period="P0")
                   for i, w in enumerate(("700", "100", "700", "100")))
    raw = DeliveryTimingInput("2026-06-01T23:00:00+08:00", tuple(
        OrderTimingInput(order.source_order_id, "2026-05-31" if i == 3 and backlog else
                         "2026-06-01" if i in (0, 3) else "2026-06-30", D(1))
        for i, order in enumerate(orders)), {})
    request = replace(base, rule_set_spec=spec, orders=orders, delivery_timing=raw)
    problem, rules = normalize_input(request), load_rule_set(spec)
    ctx = RuleEvaluationContext(problem.period_order, {p: i for i, p in enumerate(problem.period_order)}, (), problem.delivery_timing)
    candidate = SchedulePlan((Chain("first", problem.nodes[:2], "P0"), Chain("last", problem.nodes[2:], "P0")))
    state = SearchState(candidate, evaluate_plan(candidate, rules, ctx))
    context = SearchContext(VirtualFactory(RuleEdgeDecisionCache(problem, rules, ctx), budget(candidate_check_limit=100)), request.policy)
    return request, state, context


@pytest.mark.parametrize("priority", [True, False])
def test_real_node_move_trades_clearance_for_burden_late_tonnage_and_underweight(priority):
    _, state, context = tradeoff_case(backlog_priority=priority)
    before = state.current_evaluation
    assert context.factory.budget.consume_candidate_check()
    accepted = _try_segment_edit(state, context, ("width_node_move", 1, 0, 1, 2, 0, 0))
    assert accepted is priority
    if priority:
        after = state.current_evaluation
        assert after.quality_key[:6] == (0, 0, 1, 800, 1, 100)
        assert before.quality_key[:6] == (0, 0, 4, 400, 0, 0)
        assert after.metrics["newly_late_original_weight"] == 700 > before.metrics["newly_late_original_weight"]
        assert audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, after),
            context.factory.cache.problem, context.factory.cache.rule_set, budget()).report.passed
    else:
        assert state.current_evaluation == before and state.accepted_move_count == 0
    assert context.factory.budget.candidate_check_count == 1


def test_new_score_is_used_in_regular_search_and_in_pure_chain_order():
    for order_only in (False, True):
        _, state, context = tradeoff_case()
        first, last = state.current_plan.chains
        assert chain_order_objective_index(context.factory.cache.rule_set) == 2
        assert try_complete_candidate(state, context, (last, first), affected_chain_ids=(last.chain_id,),
            virtual_sequence=0, action_name="chain_order_relocation", chain_order_only=order_only)
        assert state.current_evaluation.quality_key[2] == 2


def test_new_mode_enters_refinement_without_late_tonnage_in_score():
    _, state, context = tradeoff_case()
    before = state.current_evaluation.quality_key
    run_width_optimization(state, context)
    assert state.accepted_move_count > 0 and state.current_evaluation.quality_key < before
    assert context.factory.budget.candidate_check_count <= 100


def test_no_backlog_still_uses_burden_before_underweight():
    _, state, context = tradeoff_case(backlog=False)
    # Current-month tail: moving it first lowers burden from 300 to zero, but creates underweight.
    timing = context.factory.cache.context.delivery_timing
    key = state.current_plan.chains[0].nodes[0].source_order_id
    timing = replace(timing, orders={k: replace(v, due_date="2026-06-30", due_hours=D(697)) if k == key else v
                                    for k, v in timing.orders.items()})
    ctx = replace(context.factory.cache.context, delivery_timing=timing)
    problem = replace(context.factory.cache.problem, delivery_timing=timing)
    rules = context.factory.cache.rule_set
    context = SearchContext(VirtualFactory(RuleEdgeDecisionCache(problem, rules, ctx), budget()), context.policy)
    state.current_evaluation = evaluate_plan(state.current_plan, rules, ctx)
    assert _try_segment_edit(state, context, ("width_node_move", 1, 0, 1, 2, 0, 0))
    assert state.current_evaluation.quality_key[:6] == (0, 0, 0, 0, 1, 100)


def test_unscored_metric_never_breaks_ties_and_new_mode_drops_component_veto():
    _, state, context = tradeoff_case()
    rules, before = context.factory.cache.rule_set, state.current_evaluation
    assert tuple(c.metric_key for c in rules.quality_spec) == NEW_KEYS
    modified = replace(before, metrics={**before.metrics, "newly_late_original_weight": D(999)})
    assert modified.quality_key == before.quality_key
    assert _quality_key(rules.quality_spec, before.metrics, {k: (v,) for k, v in before.metrics.items()}, ()) == (
        _quality_key(rules.quality_spec, modified.metrics, {k: (v,) for k, v in modified.metrics.items()}, ())
    )
    assert refinement_candidate_allowed(before, modified, rules)
    # These are acceptance-policy unit values, not a fabricated physical plan/audit.
    before = replace(before, quality_key=(0, 0, 4, 400, 2, 100, 0, 0, 2))
    after = replace(before, quality_key=(0, 0, 4, 400, 1, 200, 0, 0, 2))
    assert after.quality_key < before.quality_key and refinement_candidate_allowed(before, after, rules)


def test_burden_improvement_with_same_clearance_is_not_blocked_by_underweight():
    _, state, context = tradeoff_case()
    first, last = state.current_plan.chains
    # Last chain holds the latest backlog and fixes clearance; swap the two earlier orders.
    initial = SchedulePlan((replace(first, nodes=tuple(reversed(first.nodes))), last))
    state.current_plan = initial
    state.current_evaluation = evaluate_plan(initial, context.factory.cache.rule_set, context.factory.cache.context)
    before = state.current_evaluation
    assert before.quality_key[2:4] == (4, 1100)
    assert try_complete_candidate(state, context, (first, last), affected_chain_ids=(first.chain_id,),
        virtual_sequence=0, action_name="delivery_intra_move", width_optimization_only=True)
    after = state.current_evaluation
    assert after.quality_key[2:4] == (4, 400)
    # The same policy also permits an underweight tradeoff when those two leading values improve.
    burden_tradeoff = replace(after, quality_key=(*after.quality_key[:4], 1, D(100), *after.quality_key[6:]))
    assert burden_tradeoff.quality_key < before.quality_key
    assert refinement_candidate_allowed(before, burden_tradeoff, context.factory.cache.rule_set)


@pytest.mark.parametrize("field", ["score", "clearance", "tonnage"])
def test_independent_audit_rejects_forged_score_and_unscored_statistics(field):
    _, state, context = tradeoff_case()
    value = state.current_evaluation
    if field == "score":
        value = replace(value, quality_key=(*value.quality_key[:2], D(0), *value.quality_key[3:]))
    else:
        key = CLEARANCE if field == "clearance" else "newly_late_original_weight"
        value = replace(value, metrics={**value.metrics, key: value.metrics[key] + 1})
    audit = audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, value),
        context.factory.cache.problem, context.factory.cache.rule_set, budget())
    assert not audit.report.passed


def test_backlog_mode_does_not_allow_prohibited_or_unapproved_deviations():
    _, state, context = tradeoff_case()
    before = state.current_evaluation
    assert _try_segment_edit(state, context, ("width_node_move", 1, 0, 1, 2, 0, 0))
    after, rules = state.current_evaluation, context.factory.cache.rule_set
    assert len(after.violations) == 1
    violation = after.violations[0]
    for changed in (replace(violation, disposition=RuleDisposition.PROHIBITED),
                    replace(violation, reason_code="unapproved_deviation")):
        assert not refinement_candidate_allowed(before, replace(after, violations=(changed,)), rules)


@pytest.mark.parametrize("change", ["drop_source", "change_weight", "backward_sequence"])
def test_new_score_cannot_bypass_complete_candidate_safety(change):
    _, state, context = tradeoff_case()
    first, last = state.current_plan.chains
    candidate = (last, first)
    if change == "drop_source":
        candidate = (replace(last, nodes=last.nodes[:1]), first)
    elif change == "change_weight":
        candidate = (replace(last, nodes=(last.nodes[0], replace(last.nodes[1], weight=D(99)))), first)
    else:
        state.virtual_sequence = 1
    kwargs = dict(affected_chain_ids=(first.chain_id, last.chain_id), virtual_sequence=0,
                  action_name="width_node_move", width_optimization_only=True)
    if change == "backward_sequence":
        with pytest.raises(ValueError, match="cannot move backwards"):
            try_complete_candidate(state, context, candidate, **kwargs)
    else:
        assert not try_complete_candidate(state, context, candidate, **kwargs)
    assert state.accepted_move_count == 0
