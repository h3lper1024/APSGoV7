"""Direct-first positions are ordered hints; full candidate rules still decide."""

from dataclasses import replace

import pytest

from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.width_optimization import _direct_insertion_slots, _scan_width_batch
from tests.core.search.test_width_optimization_baseline import width_case


def test_real_directed_rules_prioritize_middle_slot_and_keep_both_non_direct_ends():
    state, context = width_case()
    donor, target = state.current_plan.chains
    node = donor.nodes[1]  # 1200 between 1500 and 900, but not before 1500 or after 900.
    cache = context.factory.cache
    before = fingerprint(state)
    assert cache.allows(target.nodes[0], node)
    assert not cache.allows(node, target.nodes[0])
    assert list(_direct_insertion_slots(node, target.nodes, range(3), context)) == [1, 0, 2]
    assert fingerprint(state) == before
    assert context.factory.budget.candidate_check_count == context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize("allowed,expected", [
    ({("a-middle", "b-head")}, [0, 1, 2]),
    ({("b-tail", "a-middle")}, [2, 0, 1]),
    (set(), [0, 1, 2]),
])
def test_head_tail_and_no_direct_positions_use_only_existing_sides(monkeypatch, allowed, expected):
    state, context = width_case()
    donor, target = state.current_plan.chains
    calls = []

    def check(cache, left, right):
        calls.append((left.node_id, right.node_id))
        return calls[-1] in allowed

    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", check)
    assert list(_direct_insertion_slots(donor.nodes[1], target.nodes, range(3), context)) == expected
    assert all(left != right for left, right in calls)
    assert len(calls) <= 4  # Two ends plus at most two queries for the middle slot.


@pytest.mark.parametrize("direct", [True, False])
def test_each_partition_preserves_rotated_order_and_all_slots_once(monkeypatch, direct):
    state, context = width_case()
    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", lambda *args: direct)
    donor, target = state.current_plan.chains
    assert list(_direct_insertion_slots(donor.nodes[1], target.nodes, (2, 0, 1), context)) == [2, 0, 1]


def test_direct_partition_precedes_saved_non_direct_position_and_rebuild_uses_new_neighbors():
    state, context = width_case()
    donor, target = state.current_plan.chains
    node = donor.nodes[1]
    assert list(_direct_insertion_slots(node, target.nodes, (2, 0, 1), context)) == [1, 2, 0]
    # New immutable target nodes cannot reuse an old positional classification.
    changed = (replace(target.nodes[0], width=node.width),)
    assert list(_direct_insertion_slots(node, changed, range(2), context)) == [0, 1]


def test_repeat_queries_reuse_existing_semantic_cache_without_spending_candidate_quota():
    state, context = width_case()
    donor, target = state.current_plan.chains
    cache = context.factory.cache
    first = list(_direct_insertion_slots(donor.nodes[1], target.nodes, range(3), context))
    hits, misses = cache.hit_count, cache.miss_count
    assert list(_direct_insertion_slots(donor.nodes[1], target.nodes, range(3), context)) == first
    assert cache.hit_count > hits and cache.miss_count == misses
    assert context.factory.budget.candidate_check_count == 0


@pytest.mark.parametrize("after_query", [1, 2, 3, 4])
def test_cancel_during_prescan_never_constructs_or_charges_a_candidate(monkeypatch, after_query):
    state, context = width_case()
    donor, target = state.current_plan.chains

    class Token:
        stopped = False

        def is_cancelled(self):
            return self.stopped

    token = Token()
    context.factory.budget.cancellation = token
    calls = 0

    def reject(cache, left, right):
        nonlocal calls
        calls += 1
        token.stopped = calls == after_query
        return left is target.nodes[0]  # Only the first side of the middle slot passes.

    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", reject)
    recipes = _direct_insertion_slots(donor.nodes[1], target.nodes, range(3), context)

    def forbidden(*args):
        raise AssertionError("cancelled prescan must not build a candidate")

    assert _scan_width_batch(state, context, recipes, forbidden, 1) == (False, True)
    assert context.factory.budget.stop_reason is SearchStopReason.USER_CANCELLED
    assert context.factory.budget.candidate_check_count == 0


def test_deadline_and_preexisting_budget_stop_prevent_connection_queries(monkeypatch):
    state, context = width_case()
    donor, target = state.current_plan.chains

    def forbidden(*args):
        raise AssertionError("stopped enumeration must not read edge rules")

    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", forbidden)
    for reason in (SearchStopReason.CANDIDATE_LIMIT_REACHED, SearchStopReason.USER_CANCELLED):
        context.factory.budget.stop_reason = reason
        assert list(_direct_insertion_slots(donor.nodes[1], target.nodes, range(3), context)) == []
    context.factory.budget.stop_reason = None
    context.factory.budget.clock = lambda: context.factory.budget.search_deadline_monotonic + 1
    assert list(_direct_insertion_slots(donor.nodes[1], target.nodes, range(3), context)) == []
