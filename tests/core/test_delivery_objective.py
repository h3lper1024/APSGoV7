"""Full-plan delivery metrics: backlog, deadline equality, virtual time and fragments."""

from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.app.delivery_request import with_delivery_objective
from apsgo_scheduler.app.input_normalizer import InputNormalizationError, normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.delivery_timing import (
    DeliveryTimingInput, OrderTimingInput, evaluate_delivery, normalize_delivery_timing,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, SchedulePlan
from apsgo_scheduler.core.rules.base import PlanRuleSubject, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import DeliveryDuePerformanceRule
from tests.core.test_reference_numeric_projection import node
from tests.app.test_input_normalizer import gqga4_request, gqga4_spec

D = Decimal


def example(due_a="2026-05-31", due_b="2026-06-01", hours_a="10", hours_b="20"):
    a, b, virtual = node("a", "10"), node("b", "20"), node("v", "10", virtual=True)
    raw = DeliveryTimingInput("2026-06-01T00:00:00+08:00", (
        OrderTimingInput("a", due_a, D(hours_a)), OrderTimingInput("b", due_b, D(hours_b)),
    ), {"prototype": D("0.1")})
    timing = normalize_delivery_timing(raw, (a, b), (type("Prototype", (), {"prototype_id": "prototype"})(),))
    return a, b, virtual, timing


def plan(*nodes):
    return SchedulePlan((Chain("chain", nodes, "period"),))


def test_backlog_and_newly_late_weight_are_separate():
    a, b, v, timing = example()
    result = evaluate_delivery(plan(a, v, b), timing, details=True)
    assert result.newly_late_original_weight == 20
    assert result.delivery_wait_tardiness_tonne_hours == 10 * 10 + 20 * 7
    assert result.node_times == (("a", D(0), D(10)), ("v", D(10), D(11)), ("b", D(11), D(31)))
    early = evaluate_delivery(plan(b, v, a), timing)
    assert early.newly_late_original_weight == 0
    assert early.delivery_wait_tardiness_tonne_hours == 310


def test_end_of_due_day_equality_is_on_time():
    a, b, v, timing = example(hours_a="4")
    assert evaluate_delivery(plan(a, b), timing).newly_late_original_weight == 0
    assert evaluate_delivery(plan(a, v, b), timing).newly_late_original_weight == 20


def test_split_last_piece_and_original_weight_counted_once():
    a, b, v, timing = example()
    first = replace(b, node_id="b-first", weight=D(5))
    last = replace(b, node_id="b-last", weight=D(15))
    result = evaluate_delivery(plan(first, a, v, last), timing)
    assert result.original_completion_hours["b"] == 31
    assert result.newly_late_original_weight == 20
    assert result.delivery_wait_tardiness_tonne_hours == 10 * 15 + 20 * 7


def test_missing_orders_duplicate_nodes_and_weight_loss_cannot_improve_score():
    a, b, v, timing = example()
    for candidate in (plan(a), plan(a, replace(b, weight=D(19)))):
        with pytest.raises(ValueError):
            evaluate_delivery(candidate, timing)
    with pytest.raises(ValueError):
        SchedulePlan((Chain("c1", (a, b), "period"), Chain("c2", (a,), "period")))


def test_rule_is_optional_and_emits_no_violations():
    a, b, v, timing = example()
    rule = DeliveryDuePerformanceRule("delivery", "交期", RuleScope.PLAN, True, "1", {})
    context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",), timing)
    subject = PlanRuleSubject("plan", plan(a, v, b), None)
    result = rule.evaluate(subject, context)
    assert not result.violations
    assert [value.value for value in result.metrics] == [20, 240]
    assert not replace(rule, enabled=False).evaluate(subject, replace(context, delivery_timing=None)).metrics


def test_nine_level_loading_and_required_timing(gqga4_request):
    request = gqga4_request
    spec = with_delivery_objective(request.rule_set_spec)
    assert [item.metric_key for item in spec.quality_spec[4:6]] == ["newly_late_original_weight", "delivery_wait_tardiness_tonne_hours"]
    assert spec.quality_spec[:4] == request.rule_set_spec.quality_spec[:4]
    assert spec.quality_spec[6:] == request.rule_set_spec.quality_spec[4:]
    assert spec.fingerprint != request.rule_set_spec.fingerprint
    with pytest.raises(InputNormalizationError):
        normalize_input(replace(request, rule_set_spec=spec))
    raw = DeliveryTimingInput("2026-06-01T00:00:00+08:00", tuple(
        OrderTimingInput(order.source_order_id, "2026-06-30", D(1)) for order in request.orders
    ), {p.prototype_id: D("0.1") for p in request.virtual_prototypes})
    problem = normalize_input(replace(request, rule_set_spec=spec, delivery_timing=raw))
    context = RuleEvaluationContext(problem.period_order, {p: i for i, p in enumerate(problem.period_order)}, tuple(p.prototype_id for p in problem.virtual_prototypes), problem.delivery_timing)
    candidate = SchedulePlan(tuple(Chain(str(i), (n,), n.source_period) for i, n in enumerate(sorted(problem.nodes, key=lambda n: context.period_index[n.source_period]))))
    evaluation = evaluate_plan(candidate, load_rule_set(spec), context)
    assert len(evaluation.quality_key) == 9
    assert evaluation.quality_key[4:6] == (0, 0)
