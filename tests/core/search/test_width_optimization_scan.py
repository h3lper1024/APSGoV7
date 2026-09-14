"""Shared-budget scanning; production candidate families are added in later steps."""

from dataclasses import replace

import pytest

from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.neighborhoods import try_complete_candidate
from apsgo_scheduler.core.width_optimization import _alternate_recipes, _scan_width_families
from tests.core.search.test_width_optimization_baseline import width_case


def scan_case(limit=20):
    state, context = width_case()
    context.factory.budget.candidate_check_limit = limit
    context.policy = replace(context.policy, candidate_check_limit=limit)
    return state, context


def families(*sizes):
    return tuple(
        lambda state, context, group=group, size=size: (
            (group, position) for position in range(size)
        )
        for group, size in enumerate(sizes)
    )


def test_move_and_exchange_recipes_alternate_until_both_streams_finish():
    assert list(_alternate_recipes(iter("abc"), iter("12345"))) == list("a1b2c345")
    assert list(_alternate_recipes(iter(()), iter("12"))) == list("12")
    assert list(_alternate_recipes(iter("ab"), iter(()))) == list("ab")
    assert list(_alternate_recipes(iter(()), iter(()))) == []


def test_each_family_receives_a_batch_before_the_first_family_finishes():
    state, context = scan_case(16)
    before, visits = fingerprint(state), []

    def reject(current, bound, recipe):
        visits.append((recipe, bound.factory.budget.candidate_check_count))
        return False

    assert _scan_width_families(state, context, families(100, 100, 100, 100), reject) is state
    assert [recipe for recipe, _ in visits] == [
        (group, position) for group in range(4) for position in range(4)
    ]
    assert [count for _, count in visits] == list(range(1, 17))
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert context.factory.budget.candidate_check_count == 16
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before


def test_rejected_batches_resume_at_the_original_iterator_position():
    state, context = scan_case(20)
    visits = []

    def reject(current, bound, recipe):
        visits.append(recipe)
        return False

    _scan_width_families(state, context, families(13, 1, 1, 1), reject)
    assert visits == (
        [(0, i) for i in range(5)] + [(1, 0), (2, 0), (3, 0)] + [(0, i) for i in range(5, 13)]
    )
    assert context.factory.budget.candidate_check_count == 16
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


def test_real_acceptance_discards_stale_recipes_and_restarts_from_the_smallest_family():
    state, context = scan_case()
    visits, snapshots = [], []

    def first(current, bound):
        snapshots.append(fingerprint(current.current_plan))
        if not current.accepted_move_count:
            yield "reject"
            yield "accept"
            yield "stale"
        else:
            yield "new-plan"

    def second(current, bound):
        yield "second"

    def apply(current, bound, recipe):
        visits.append(recipe)
        if recipe != "accept":
            return False
        a, b = current.current_plan.chains
        return try_complete_candidate(
            current,
            bound,
            (replace(a, nodes=a.nodes[:-1]), replace(b, nodes=(*b.nodes, a.nodes[-1]))),
            affected_chain_ids=("A", "B"),
            virtual_sequence=current.virtual_sequence,
            action_name="scan_guard_test_move",
            width_optimization_only=True,
        )

    _scan_width_families(state, context, (first, second), apply)
    assert visits == ["reject", "accept", "new-plan", "second"]
    assert len(snapshots) == 2 and snapshots[0] != snapshots[1]
    assert state.current_evaluation.quality_key[4] == 300
    assert state.accepted_move_count == len(context.accepted_move_traces) == 1
    assert context.complete_candidate_evaluation_count == 1
    assert context.factory.budget.candidate_check_count == 4
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


@pytest.mark.parametrize("limit", (0, 1, 3, 4))
def test_actual_remaining_quota_is_never_renewed_or_exceeded(limit):
    state, context = scan_case(limit)
    visited = []

    def reject(current, bound, recipe):
        visited.append(recipe)
        return False

    _scan_width_families(state, context, families(10, 10, 10, 10), reject)
    assert len(visited) == context.factory.budget.candidate_check_count == limit
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED


def test_checks_spent_by_previous_phases_are_not_reset():
    state, context = scan_case(10)
    context.factory.budget.candidate_check_count = 7
    visited = []

    def reject(current, bound, recipe):
        visited.append(bound.factory.budget.candidate_check_count)
        return False

    _scan_width_families(state, context, families(20, 20), reject)
    assert visited == [8, 9, 10]


@pytest.mark.parametrize("sizes", ((), (0,), (0, 0, 0, 0), (1, 1, 1, 1)))
def test_natural_exhaustion_is_distinct_from_a_batch_or_budget_stop(sizes):
    state, context = scan_case(sum(sizes))
    _scan_width_families(state, context, families(*sizes), lambda *_: False)
    assert context.factory.budget.candidate_check_count == sum(sizes)
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


@pytest.mark.parametrize("reason", tuple(SearchStopReason))
def test_scanner_does_not_clear_any_preexisting_stop_marker(reason):
    state, context = scan_case()
    context.factory.budget.stop_reason = reason

    def forbidden(*_):
        raise AssertionError("stopped search must not create an iterator or candidate")

    _scan_width_families(state, context, (forbidden,), forbidden)
    assert context.factory.budget.stop_reason is reason
    assert context.factory.budget.candidate_check_count == 0


def test_cancellation_after_recipe_generation_prevents_business_construction():
    state, context = scan_case()

    class Cancellation:
        stopped = False

        def is_cancelled(self):
            return self.stopped

    token = Cancellation()
    context.factory.budget.cancellation = token

    def recipes(current, bound):
        token.stopped = True
        yield (0, 0)

    def forbidden(*_):
        raise AssertionError("cancelled recipe must not be built")

    before = fingerprint(state)
    _scan_width_families(state, context, (recipes,), forbidden)
    assert fingerprint(state) == before
    assert context.factory.budget.candidate_check_count == 0
    assert context.factory.budget.stop_reason is SearchStopReason.USER_CANCELLED


def test_failed_candidate_construction_is_counted_without_publishing_partial_state():
    state, context = scan_case()
    before = fingerprint(state)

    def fail(*_):
        raise ValueError("construction failed")

    with pytest.raises(ValueError, match="construction failed"):
        _scan_width_families(state, context, families(1), fail)
    assert fingerprint(state) == before
    assert context.factory.budget.candidate_check_count == 1
    assert context.accepted_move_traces == ()


def test_small_batches_resume_all_streams_and_skip_empty_ones():
    from apsgo_scheduler.core.width_optimization import _round_robin
    _, context = scan_case(100)
    assert list(_round_robin((iter(range(7)), iter(()), iter(range(10, 16))), context.factory.budget)) == [
        0, 1, 2, 3, 10, 11, 12, 13, 4, 5, 6, 14, 15,
    ]
    assert context.factory.budget.candidate_check_count == 0  # Enumeration is not execution.


def test_backlog_families_retain_complete_shapes_and_unique_exchange_ownership(monkeypatch):
    from apsgo_scheduler.core import width_optimization as width
    from tests.core.search.test_width_optimization_blocks import block_case
    state, context = block_case(((1600, 300), (1400, 300), (1200, 300), (800, 300)),
                                ((1500, 300), (1300, 300), (1100, 300), (900, 300)))
    positions = tuple((i, j) for i in range(2) for j in range(3, -1, -1))
    monkeypatch.setattr(width, "delivery_node_positions", lambda *a, **kw: positions)

    def canonical(recipe):
        if recipe[0] not in ("width_node_exchange", "width_block_exchange"):
            return recipe
        action, i, j, start, stop, other_start, other_stop = recipe
        return (action, *sorted(((i, start, stop), (j, other_start, other_stop))))

    old = width._delivery_iterators(state, context)
    new = width._backlog_iterators(state, context)
    for before, after in zip(old, new):
        expected = {canonical(recipe) for recipe in before}
        actual = [canonical(recipe) for recipe in after]
        assert set(actual) == expected
        assert len(actual) == len(set(actual))
