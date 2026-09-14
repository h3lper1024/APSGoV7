"""Critical scheduling keeps the complete old neighborhood and one shared allowance."""

from collections import Counter
from dataclasses import replace
from decimal import Decimal as D

import pytest

from apsgo_scheduler.core import width_optimization as search
from apsgo_scheduler.core.chain_order import critical_delivery_positions
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, SchedulePlan, SplitLineage, ControlledSplitMode
from tests.core.test_backlog_priority_objective import tradeoff_case


def case():
    _, state, context = tradeoff_case(second_precision=True, minimum="1")
    context.factory.budget.candidate_check_limit = 100000
    context.policy = replace(context.policy, candidate_check_limit=100000)
    return state, context


def signature(recipe):
    if recipe[0] in ("width_node_exchange", "width_block_exchange"):
        action, i, j, a, b, c, d = recipe
        return action, *sorted(((i, a, b), (j, c, d)))
    return recipe


@pytest.mark.parametrize("shape", [(2, 2), (1, 3), (3, 1), (4,), (1, 1, 2)])
def test_lane_union_matches_old_semantic_proposals_without_duplicate_swaps(shape):
    state, context = case()
    nodes = tuple(node for chain in state.current_plan.chains for node in chain.nodes)
    offset, chains = 0, []
    for index, count in enumerate(shape):
        chains.append(Chain(str(index), nodes[offset:offset + count], "P0"))
        offset += count
    state.current_plan = SchedulePlan(tuple(chains))
    old = Counter(signature(recipe) for stream in search._backlog_iterators(state, context) for recipe in stream)
    positions, critical = critical_delivery_positions(state.current_plan, context.factory.cache.context)
    lanes = [Counter(signature(recipe) for stream in search._backlog_iterators(
        state, context, positions=positions, critical_ids=frozenset(critical), critical_lane=lane)
        for recipe in stream) for lane in (True, False)]
    assert not lanes[0].keys() & lanes[1].keys()
    assert lanes[0] + lanes[1] == old


def test_one_rank_per_original_and_last_piece_first():
    state, context = case()
    a, b = state.current_plan.chains[0].nodes
    c, d = state.current_plan.chains[1].nodes
    plan = SchedulePlan((Chain("all", (replace(d, node_id="d1", weight=D(20)), a, b, c,
                                       replace(d, node_id="d2", weight=D(80))), "P0"),))
    positions, critical = critical_delivery_positions(plan, context.factory.cache.context)
    assert critical[0] == d.source_order_id and len(critical) == len(set(critical))
    assert positions[:2] == ((0, 4), (0, 0))


def test_unique_earliest_source_promotes_whole_chain_potential_and_multiple_owners_do_not():
    state, context = case()
    a, b = state.current_plan.chains[0].nodes
    c, d = state.current_plan.chains[1].nodes
    timing = context.factory.cache.context.delivery_timing
    # All are on time: selection must come only from postponement potential.
    timing = replace(timing, orders={key: replace(value, due_date="2026-06-30", due_hours=D(697))
                                    for key, value in timing.orders.items()})
    bound = replace(context.factory.cache.context, period_order=("P0", "P1", "P2"),
                    period_index={"P0": 0, "P1": 1, "P2": 2}, delivery_timing=timing)
    plan = SchedulePlan((Chain("one", (a, replace(b, source_period="P1"), replace(c, source_period="P1")), "P0"),
                         Chain("two", (d,), "P0")))
    _, critical = critical_delivery_positions(plan, bound)
    assert critical == (a.source_order_id,)
    lineage = SplitLineage("partition", b.node_id, b.source_order_id, b.source_resource_id,
        "P2", "P0", ControlledSplitMode.FUTURE_BORROW_RETURN, "P2", 1, b.weight * 2, 1, 2,
        "split-rule", "1", "decision", "reason")
    locked = replace(plan.chains[0].nodes[1], node_id="locked-piece", source_period="P2", split_lineage=lineage)
    locked_plan = replace(plan, chains=(replace(plan.chains[0], nodes=(a, locked, plan.chains[0].nodes[2])), plan.chains[1]))
    assert critical_delivery_positions(locked_plan, bound)[1] == ()
    plan = replace(plan, chains=(replace(plan.chains[0], nodes=(a, b, replace(c, source_period="P1"))), plan.chains[1]))
    assert critical_delivery_positions(plan, bound)[1] == ()


def test_batches_alternate_and_both_lanes_finish_before_natural_stop(monkeypatch):
    state, context = case()
    calls = []

    def iterators(current, bound, *, critical_lane, **kwargs):
        lane = "critical" if critical_lane else "normal"
        return [iter(tuple((lane, family, index) for index in range(130))) for family in range(5)]

    monkeypatch.setattr(search, "_backlog_iterators", iterators)
    search._scan_critical_families(state, context, lambda s, c, r: calls.append(r) or False)
    assert [calls[i][0] for i in (0, 63, 64, 127, 128)] == ["critical", "critical", "normal", "normal", "critical"]
    assert len(calls) == (3 + 5) * 130
    assert context.factory.budget.candidate_check_count == len(calls)
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


def test_acceptance_switches_lane_rebuilds_indices_and_revisits_changed_critical(monkeypatch):
    state, context = case()
    identities, calls, revisits = [], [], []

    def iterators(current, bound, *, critical_lane, revisit=(), **kwargs):
        identities.append(current.current_plan)
        if critical_lane:
            revisits.append(revisit)
        return [iter(((critical_lane, current.current_plan),)) for _ in range(5)]

    def attempt(current, bound, recipe):
        assert recipe[1] is current.current_plan
        calls.append(recipe[0])
        if len(calls) == 1:
            chain = current.current_plan.chains[1]
            current.current_plan = replace(current.current_plan, chains=(current.current_plan.chains[0], replace(chain, nodes=tuple(reversed(chain.nodes)))))
            current.current_evaluation = evaluate_plan(current.current_plan, bound.factory.cache.rule_set, bound.factory.cache.context)
            return True
        return False

    monkeypatch.setattr(search, "_backlog_iterators", iterators)
    search._scan_critical_families(state, context, attempt)
    assert calls[:2] == [True, False]
    assert identities[0] is not identities[2]
    assert state.current_plan.chains[1].nodes[0].source_order_id in revisits[1]


def test_limit_keeps_accepted_state_and_does_not_claim_exhaustion():
    state, context = case()
    before = fingerprint(state)
    context.factory.budget.candidate_check_limit = 3
    search._scan_critical_families(state, context, lambda *args: False)
    assert context.factory.budget.candidate_check_count == 3
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert fingerprint(state) == before


def test_no_critical_orders_still_retains_normal_work():
    state, context = case()
    cache = context.factory.cache
    timing = replace(cache.context.delivery_timing, orders={key: replace(value, due_date="2026-06-30", due_hours=D(697))
                     for key, value in cache.context.delivery_timing.orders.items()})
    cache = replace(cache, problem=replace(cache.problem, delivery_timing=timing),
                    context=replace(cache.context, delivery_timing=timing))
    context.factory = replace(context.factory, cache=cache)
    assert critical_delivery_positions(state.current_plan, cache.context)[1] == ()
    visits = []
    search._scan_critical_families(state, context, lambda s, c, r: visits.append(r) or False)
    assert visits and context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


def test_cancel_during_attempt_preserves_state():
    state, context = case()
    before = fingerprint(state)
    class Cancel:
        def is_cancelled(self):
            return context.factory.budget.candidate_check_count >= 2
    context.factory.budget.cancellation = Cancel()
    search._scan_critical_families(state, context, lambda *args: False)
    assert context.factory.budget.stop_reason is SearchStopReason.USER_CANCELLED
    assert context.factory.budget.candidate_check_count == 2
    assert fingerprint(state) == before


def test_long_segments_and_split_pieces_keep_semantic_coverage_and_determinism():
    state, context = case()
    chains = tuple(replace(chain, nodes=tuple(replace(node, node_id=f"{node.node_id}-{part}", weight=node.weight / 4)
                       for node in chain.nodes for part in range(4))) for chain in state.current_plan.chains)
    state.current_plan = SchedulePlan(chains)
    positions, critical = critical_delivery_positions(state.current_plan, context.factory.cache.context)
    def proposals(lane):
        return [signature(recipe) for stream in search._backlog_iterators(state, context,
                positions=positions, critical_ids=frozenset(critical), critical_lane=lane) for recipe in stream]
    old = Counter(signature(recipe) for stream in search._backlog_iterators(state, context) for recipe in stream)
    critical_proposals, normal = proposals(True), proposals(False)
    assert Counter(critical_proposals) + Counter(normal) == old
    assert not set(critical_proposals) & set(normal)
    assert critical_proposals == proposals(True) and normal == proposals(False)
    assert any(recipe[0] == "width_block_move" and recipe[4] - recipe[3] > 3 for recipe in normal)
