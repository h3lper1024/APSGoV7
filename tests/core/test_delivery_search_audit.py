"""Delivery-first moves still use the existing transaction, audit and budget."""

from dataclasses import replace
from decimal import Decimal

from apsgo_scheduler.api.request import RuleDefinitionSpec
from apsgo_scheduler.app.contract_audit import _request_binding
from apsgo_scheduler.app.delivery_report import build_delivery_report
from apsgo_scheduler.app.delivery_request import with_delivery_objective
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core.chain_order import stable_group_plan
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import CoreCandidateSnapshot, RuleScope
from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.final_audit import audit_core_without_search_cache
from apsgo_scheduler.core.model import Chain, SchedulePlan, SearchState
from apsgo_scheduler.core.neighborhoods import SearchContext, try_complete_candidate
from apsgo_scheduler.core.rules.base import RuleEvaluationContext
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.width_optimization import _width_improves, run_width_optimization
from tests.app.test_input_normalizer import make_order, make_request, make_spec, read_gqga4_spec
from tests.core.graph.test_bipartite_matching import budget

D = Decimal


def search_case(*, delivery=True, width=True):
    seven = read_gqga4_spec("gqga4_rule_set_spec.json")
    definitions = (
        RuleDefinitionSpec("weight", "ChainWeightRangeRule", "重量", RuleScope.CHAIN, True, "1", {"min_weight": D(1), "max_weight": D(2000), "target_weight": D(1200)}),
        RuleDefinitionSpec("width", "InterChainWidthGapRule", "宽差", RuleScope.PLAN, True, "1", {}),
    )
    spec = make_spec(rules=definitions, quality_spec=seven.quality_spec)
    if delivery:
        spec = with_delivery_objective(spec)
    if not width:
        spec = make_spec(rules=tuple(r for r in spec.rules if r.rule_type != "InterChainWidthGapRule"), quality_spec=tuple(c for c in spec.quality_spec if c.metric_key != "inter_chain_width_gap"))
    orders = tuple(make_order(i, weight=D(800), width=D(1000 + i * 100), source_period="P0") for i in range(3))
    request = make_request(rule_set_spec=spec, orders=orders, virtual_prototypes=())
    request = replace(request, policy=replace(request.policy, candidate_check_limit=100))
    if delivery:
        timing = DeliveryTimingInput("2026-06-01T00:00:00+08:00", tuple(
            OrderTimingInput(order.source_order_id, "2026-06-01" if i == 1 else "2026-06-30", D(20)) for i, order in enumerate(orders)
        ), {})
        request = replace(request, delivery_timing=timing)
    problem = normalize_input(request)
    rules = load_rule_set(spec)
    rule_context = RuleEvaluationContext(problem.period_order, {p: i for i, p in enumerate(problem.period_order)}, (), problem.delivery_timing)
    plan = SchedulePlan(tuple(Chain(str(i), (n,), "P0") for i, n in enumerate(problem.nodes)))
    state = SearchState(plan, evaluate_plan(plan, rules, rule_context))
    context = SearchContext(VirtualFactory(RuleEdgeDecisionCache(problem, rules, rule_context), budget(candidate_check_limit=100)), request.policy)
    return request, state, context


def move_middle_first(state, context):
    a, b, c = state.current_plan.chains
    return try_complete_candidate(state, context, (b, a, c), affected_chain_ids=(b.chain_id,), virtual_sequence=0, action_name="chain_order_relocation", chain_order_only=True, width_optimization_only=True)


def test_delivery_can_worsen_width_while_legacy_path_rejects():
    _, state, context = search_case()
    before = state.current_evaluation.quality_key
    a, b, c = state.current_plan.chains
    assert _width_improves((b, a, c), state, context)
    assert move_middle_first(state, context)
    after = state.current_evaluation.quality_key
    assert after[:4] == before[:4] == (0, 0, 0, 0)
    assert after[4] == 0 < before[4]
    assert after[6] > before[6]
    _, old, old_context = search_case(delivery=False)
    assert not move_middle_first(old, old_context)


def test_delivery_only_sequence_stage_and_shared_limit():
    _, state, context = search_case(width=False)
    before = state.current_evaluation.quality_key
    run_width_optimization(state, context)
    assert state.current_evaluation.quality_key < before
    assert context.factory.budget.candidate_check_count <= 100
    assert state.current_plan == stable_group_plan(state.current_plan, context.factory.cache.context.period_index)


def test_independent_audit_detects_forged_delivery_score():
    _, state, context = search_case()
    assert move_middle_first(state, context)
    problem, rules = context.factory.cache.problem, context.factory.cache.rule_set
    snapshot = CoreCandidateSnapshot(state.current_plan, state.current_evaluation)
    audit = audit_core_without_search_cache(snapshot, problem, rules, budget())
    assert audit.report.passed
    wrong = replace(snapshot, search_evaluation=replace(snapshot.search_evaluation, quality_key=(*snapshot.search_evaluation.quality_key[:4], D(1), *snapshot.search_evaluation.quality_key[5:])))
    assert not audit_core_without_search_cache(wrong, problem, rules, budget()).report.passed


def test_raw_request_timing_is_independently_bound():
    request, _, context = search_case()
    bad = replace(request, delivery_timing=replace(request.delivery_timing, schedule_start_at="2026-06-02T00:00:00+08:00"))
    errors = []
    _request_binding(bad, context.factory.cache.problem, lambda passed, code: errors.append(code) if not passed else None, budget())
    assert errors == ["request_problem_timing_mismatch"]


def test_public_backend_result_and_readonly_delivery_details():
    request, _, _ = search_case()
    result = solve_request(request)
    assert result.release is not None, result.issues
    report = build_delivery_report(request, result)
    assert report["kind"] == "audited_release"
    assert len(report["delivery_orders"]) == 3
    assert len(report["delivery_nodes"]) == 3
    assert report["delivery_summary"]["newly_late_weight"] == result.release.evaluation.quality_key[4]
