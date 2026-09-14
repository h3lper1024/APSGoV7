"""Targeted delivery search preserves full candidate and audit boundaries."""

from dataclasses import replace
from decimal import Decimal as D

import pytest

from apsgo_scheduler.core.chain_order import delivery_node_positions, refinement_admissible, refinement_candidate_allowed
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.rules.base import RuleDisposition
from apsgo_scheduler.core.width_optimization import run_width_optimization
from apsgo_scheduler.core.width_optimization import _intra_recipes, _try_intra_move, _delivery_node_recipes, _try_segment_edit
from apsgo_scheduler.core.model import Chain, SchedulePlan
from apsgo_scheduler.core.model import SearchState
from apsgo_scheduler.core.contracts import CoreCandidateSnapshot
from apsgo_scheduler.core.final_audit import audit_core_without_search_cache
from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.neighborhoods import SearchContext
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.rules.base import RuleEvaluationContext
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from tests.app.test_input_normalizer import make_order, make_spec
from tests.core.graph.test_bipartite_matching import budget
from tests.core.test_delivery_search_audit import search_case


def underweight_case(delivery=True):
    request, state, context = search_case(delivery=delivery, allow_underweight=True)
    # Move nearly all of the last source into the first chain; the final fragment
    # leaves one 0.5-tonne chain and all source weights are still conserved.
    a, b, c = state.current_plan.chains
    head = replace(c.nodes[0], node_id="c-head", weight=D("799.5"))
    tail = replace(c.nodes[0], node_id="c-tail", weight=D("0.5"))
    plan = SchedulePlan((replace(a, nodes=(*a.nodes, head)), b, replace(c, nodes=(tail,))))
    state.current_plan = plan
    state.current_evaluation = evaluate_plan(plan, context.factory.cache.rule_set, context.factory.cache.context)
    return request, state, context


def test_rank_late_order_before_on_time_and_keep_input_stable_ties():
    _, state, context = search_case()
    timing = context.factory.cache.context.delivery_timing
    assert delivery_node_positions(state.current_plan, timing) == ((1, 0), (2, 0), (0, 0))
    nodes = tuple(c.nodes[0] for c in state.current_plan.chains)
    plan = SchedulePlan((Chain("one", nodes, "P0"),))
    assert delivery_node_positions(plan, timing)[0] == (0, 1)


def test_zero_slack_is_on_time_and_backlog_interleaves():
    _, state, context = search_case()
    timing = context.factory.cache.context.delivery_timing
    keys = tuple(timing.orders)
    orders = {k: replace(v, due_date="2026-05-31" if k == keys[0] else "2026-06-02" if k == keys[1] else "2026-06-04",
                         due_hours=D(0) if k == keys[0] else D(48) if k == keys[1] else D(96), hours_per_tonne=D("0.03"))
              for k, v in timing.orders.items()}
    timing = replace(timing, orders=orders)
    assert delivery_node_positions(state.current_plan, timing) == ((1, 0), (0, 0), (2, 0))


def test_backlog_tail_first_and_three_groups_interleave_without_changing_default():
    _, state, context = search_case()
    timing = context.factory.cache.context.delivery_timing
    keys = tuple(timing.orders)
    timing = replace(timing, orders={k: replace(v, due_date="2026-05-31", due_hours=D(0))
                                    if k != keys[1] else v for k, v in timing.orders.items()})
    # The late current-month order, then latest backlog, then earlier backlog.
    assert delivery_node_positions(state.current_plan, timing, backlog_first=True) == ((1, 0), (2, 0), (0, 0))
    _, ordinary, bound = search_case()
    timing = bound.factory.cache.context.delivery_timing
    assert delivery_node_positions(ordinary.current_plan, timing, backlog_first=True) == delivery_node_positions(ordinary.current_plan, timing)


def test_backlog_priority_does_not_reward_a_split_original_with_extra_queue_slots():
    _, state, context = search_case()
    timing = context.factory.cache.context.delivery_timing
    timing = replace(timing, orders={k: replace(v, due_date="2026-05-31", due_hours=D(0)) for k, v in timing.orders.items()})
    a, b, c = (chain.nodes[0] for chain in state.current_plan.chains)
    plan = SchedulePlan((Chain("one", (replace(a, node_id="first", weight=D(799)), b, c,
                                        replace(a, node_id="last", weight=D(1))), "P0"),))
    assert delivery_node_positions(plan, timing, backlog_first=True) == ((0, 3), (0, 0), (0, 2), (0, 1))


def test_last_fragment_ranked_first_without_duplicate_original_priority():
    _, state, context = search_case()
    timing = context.factory.cache.context.delivery_timing
    a, b, c = (chain.nodes[0] for chain in state.current_plan.chains)
    # Ordering is based on conserved source pieces; authorization belongs to candidate audit.
    first = replace(b, node_id="piece1", weight=D(400))
    last = replace(b, node_id="piece2", weight=D(400))
    plan = SchedulePlan((Chain("one", (first, a, last, c), "P0"),))
    assert delivery_node_positions(plan, timing)[:2] == ((0, 2), (0, 0))


def test_existing_underweight_can_refine_without_increasing_either_metric():
    _, state, context = underweight_case()
    before = state.current_evaluation
    assert refinement_admissible(before, context.factory.cache.rule_set)
    run_width_optimization(state, context)
    assert state.accepted_move_count > 0
    assert state.current_evaluation.quality_key < before.quality_key
    assert all(a <= b for a, b in zip(state.current_evaluation.quality_key[2:4], before.quality_key[2:4]))


def test_legacy_underweight_still_cannot_refine():
    _, state, context = underweight_case(False)
    before = state.current_plan
    assert not refinement_admissible(state.current_evaluation, context.factory.cache.rule_set)
    run_width_optimization(state, context)
    assert state.current_plan is before and context.factory.budget.candidate_check_count == 0


@pytest.mark.parametrize("count,gap,allowed", [(0, D(1), False), (1, D("0.6"), False), (2, D("0.1"), False), (0, D(0), True), (1, D("0.5"), True)])
def test_count_and_gap_are_independent_guards(count, gap, allowed):
    _, state, context = underweight_case()
    before = state.current_evaluation
    after = replace(before, quality_key=(*before.quality_key[:2], count, gap, *before.quality_key[4:]))
    assert refinement_candidate_allowed(before, after, context.factory.cache.rule_set) is allowed


def test_other_or_prohibited_deviation_cannot_enter():
    _, state, context = underweight_case()
    evaluation = state.current_evaluation
    violation = evaluation.violations[0]
    for item in (replace(violation, reason_code="other"), replace(violation, disposition=RuleDisposition.PROHIBITED)):
        assert not refinement_admissible(replace(evaluation, violations=(item,)), context.factory.cache.rule_set)


def test_intra_move_advances_order_and_preserves_all_nodes():
    _, state, context = search_case()
    a, b, c = (chain.nodes[0] for chain in state.current_plan.chains)
    plan = SchedulePlan((Chain("ab", (a, b), "P0"), Chain("c", (c,), "P0")))
    state.current_plan = plan
    state.current_evaluation = evaluate_plan(plan, context.factory.cache.rule_set, context.factory.cache.context)
    positions = delivery_node_positions(plan, context.factory.cache.context.delivery_timing)
    recipe = next(_intra_recipes(state, positions))
    assert recipe == ("delivery_intra_move", 0, 1, 0)
    assert _try_intra_move(state, context, recipe)
    assert state.current_plan.chains[0].nodes == (b, a)
    assert state.current_evaluation.quality_key[4] == 0
    assert not _try_intra_move(state, context, ("delivery_intra_move", 0, 0, 0))


def test_directed_exchange_can_target_a_non_underweight_chain():
    _, state, context = search_case()
    positions = delivery_node_positions(state.current_plan, context.factory.cache.context.delivery_timing)
    recipes = list(_delivery_node_recipes(state, positions))
    swap = next(r for r in recipes if r[:4] == ("width_node_exchange", 1, 0, 0))
    assert _try_segment_edit(state, context, swap)
    assert state.current_evaluation.quality_key[4] == 0
    swaps = [tuple(sorted(((r[1], r[3]), (r[2], r[5])))) for r in recipes if r[0] == "width_node_exchange"]
    assert len(swaps) == len(set(swaps)) == 3


def audited_case(*, cross_period=False):
    request, _, _ = search_case(allow_underweight=True)
    rules = tuple(replace(r, parameters={"min_weight": D(700), "max_weight": D(2000), "target_weight": D(1200)})
                  if r.rule_type == "ChainWeightRangeRule" else r for r in request.rule_set_spec.rules)
    spec = make_spec(rules=rules, quality_spec=request.rule_set_spec.quality_spec,
                     allowed_final_deviation_codes=("chain_weight_below_minimum",))
    orders = tuple(make_order(i, weight=D(300 if i == 4 else 400), width=D(1000),
                              source_period="P1" if cross_period and i in (2, 3) else "P0") for i in range(5))
    timing = DeliveryTimingInput("2026-06-01T00:00:00+08:00", tuple(
        OrderTimingInput(o.source_order_id, "2026-06-01" if i == (2 if cross_period else 1) else "2026-06-30", D(20))
        for i, o in enumerate(orders)), {})
    request = replace(request, orders=orders, rule_set_spec=spec, delivery_timing=timing)
    problem = normalize_input(request)
    active = load_rule_set(spec)
    rule_context = RuleEvaluationContext(problem.period_order, {p: i for i, p in enumerate(problem.period_order)}, (), problem.delivery_timing)
    from apsgo_scheduler.core.chain_order import stable_group_plan
    plan = stable_group_plan(SchedulePlan((Chain("a", problem.nodes[:2], "P0"),
                         Chain("b", problem.nodes[2:4], "P1" if cross_period else "P0"),
                         Chain("c", problem.nodes[4:], "P0"))), rule_context.period_index)
    state = SearchState(plan, evaluate_plan(plan, active, rule_context))
    context = SearchContext(VirtualFactory(RuleEdgeDecisionCache(problem, active, rule_context), budget(candidate_check_limit=100)), request.policy)
    return request, state, context


def test_directed_scan_with_underweight_finishes_with_independent_audit():
    _, state, context = audited_case()
    before = state.current_evaluation.quality_key
    run_width_optimization(state, context)
    assert any(t.action_name == "delivery_intra_move" for t in context.accepted_move_traces)
    assert state.current_evaluation.quality_key < before
    assert all(t.quality_after[2] <= t.quality_before[2] and t.quality_after[3] <= t.quality_before[3]
               for t in context.accepted_move_traces)
    audit = audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
                                           context.factory.cache.problem, context.factory.cache.rule_set, budget())
    assert audit.report.passed
    assert context.factory.budget.candidate_check_count <= 100


def test_cross_period_exchange_preserves_original_sources_and_audits():
    _, state, context = audited_case(cross_period=True)
    # Chain b is third after stable period grouping. Exchange its urgent head
    # with an ordinary first-period node; normalization may bring both into P0.
    assert _try_segment_edit(state, context, ("width_node_exchange", 2, 0, 0, 1, 0, 1))
    audit = audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
                                           context.factory.cache.problem, context.factory.cache.rule_set, budget())
    assert audit.report.passed


def test_directed_blocks_cover_legacy_shapes_without_duplicate_pairs():
    from apsgo_scheduler.core.width_optimization import _block_recipes, _delivery_block_recipes
    from tests.core.search.test_width_optimization_blocks import block_case
    state, context = block_case(((1600, 300), (1400, 300), (1200, 300), (800, 300)),
                                ((1500, 300), (1300, 300), (1100, 300), (900, 300)))
    positions = tuple((i, j) for i in range(2) for j in range(3, -1, -1))
    def canonical(r):
        action, i, j, start, stop, other_start, other_stop = r
        if action == "width_block_move":
            return r
        return (action, *sorted(((i, start, stop), (j, other_start, other_stop))))
    old = {canonical(r) for r in _block_recipes(state, context)}
    new = [canonical(r) for r in _delivery_block_recipes(state, positions)]
    assert len(new) == len(set(new))
    assert set(new) == old


def test_intra_failed_seam_and_cancellation_leave_state_untouched(monkeypatch):
    _, state, context = audited_case()
    before = state.current_plan
    monkeypatch.setattr(VirtualFactory, "bridge", lambda *a, **kw: None)
    assert not _try_intra_move(state, context, ("delivery_intra_move", 0, 1, 0))
    assert state.current_plan is before and state.accepted_move_count == 0
    from apsgo_scheduler.core.contracts import SearchStopReason
    context.factory.budget.stop_reason = SearchStopReason.USER_CANCELLED
    run_width_optimization(state, context)
    assert context.factory.budget.stop_reason is SearchStopReason.USER_CANCELLED
    assert state.current_plan is before
