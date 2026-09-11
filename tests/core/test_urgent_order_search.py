"""Targeted delivery search preserves full candidate and audit boundaries."""

from dataclasses import replace
from decimal import Decimal as D

from apsgo_scheduler.core.chain_order import delivery_node_positions
from apsgo_scheduler.core.model import Chain, SchedulePlan
from tests.core.test_delivery_search_audit import search_case


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
    orders = {k: replace(v, due_hours=D(0) if k == keys[0] else D(40) if k == keys[1] else D(80))
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
