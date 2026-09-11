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
