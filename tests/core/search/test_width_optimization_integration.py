"""The post-repair phase shares four real families and the existing acceptor."""

from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.core import neighborhoods, width_optimization
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.width_optimization import (
    _block_recipes,
    _cut_recipes,
    _node_recipes,
    _order_recipes,
    _scan_width_batch,
    _try_width_recipe,
    run_width_optimization,
)
from tests.core.rules.test_inter_chain_width_gap import rule
from tests.core.search.test_chain_order import chain, search_case
from tests.core.search.test_complete_candidate_lifecycle import Stop
from tests.core.search.test_width_optimization_baseline import width_case
from tests.core.search.test_width_optimization_blocks import block_case, exchange_case
from tests.core.search.test_width_optimization_guard import bind_rules, joint_candidate
from tests.core.search.test_width_optimization_nodes import nodes_by_id
from tests.core.test_quality_key import ruleset

D = Decimal
ORDER = "width_chain_order_relocation"


def equal_case():
    return block_case(((1000, 300),) * 3, ((1000, 300),) * 3)


def order_case():
    _, base = width_case()
    return search_case(
        tuple(chain(name, width) for name, width in zip("ABC", (1000, 1500, 1200))),
        active=base.factory.cache.rule_set,
    )


def block_move_case():
    return block_case(
        ((1600, 500), (1200, 500), (900, 350), (800, 350)), ((1500, 500), (1000, 300))
    )


@pytest.mark.parametrize("mode", ("missing", "disabled", "diagnostic"))
@pytest.mark.parametrize("reason", (None, SearchStopReason.LOCAL_SEARCH_COMPLETE))
def test_phase_skips_unavailable_objective_without_clearing_old_completion(
    monkeypatch, mode, reason
):
    state, context = width_case()
    active = ruleset()
    if mode != "missing":
        active = replace(active, rules=active.rules + (rule(enabled=mode != "disabled"),))
    bind_rules(state, context, active)
    before = fingerprint(state)
    context.factory.budget.stop_reason = reason

    def forbidden(*args):
        raise AssertionError("an undeclared width objective must not start scanning")

    monkeypatch.setattr(width_optimization, "_scan_width_families", forbidden)
    assert run_width_optimization(state, context) is state
    assert fingerprint(state) == before
    assert context.factory.budget.stop_reason is reason
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize("violation", ("underweight", "prohibited"))
def test_any_remaining_rule_violation_skips_the_phase(monkeypatch, violation):
    state, context = width_case()
    a, b = state.current_plan.chains
    if violation == "underweight":
        b = replace(b, nodes=tuple(replace(item, weight=D(200)) for item in b.nodes))
    else:
        a = replace(a, nodes=(replace(a.first_node, width=D(700)), *a.nodes[1:]))
    state, context = search_case((a, b), active=context.factory.cache.rule_set)
    assert state.current_evaluation.violations
    before = fingerprint(state)
    context.factory.budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE

    def forbidden(*args):
        raise AssertionError("width optimization cannot take priority over unfinished repair")

    monkeypatch.setattr(width_optimization, "_scan_width_families", forbidden)
    assert run_width_optimization(state, context) is state
    assert fingerprint(state) == before
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize(
    "reason",
    tuple(item for item in SearchStopReason if item is not SearchStopReason.LOCAL_SEARCH_COMPLETE),
)
def test_phase_never_resumes_a_real_stop_or_resets_previous_checks(monkeypatch, reason):
    state, context = width_case()
    before = fingerprint(state)
    budget = context.factory.budget
    budget.candidate_check_count = 7
    budget.stop_reason = reason

    def forbidden(*args):
        raise AssertionError("a stopped phase must not enumerate or build candidates")

    monkeypatch.setattr(width_optimization, "_node_recipes", forbidden)
    monkeypatch.setattr(width_optimization, "_try_width_recipe", forbidden)
    assert run_width_optimization(state, context) is state
    assert fingerprint(state) == before
    assert budget.stop_reason is reason and budget.candidate_check_count == 7
    assert context.complete_candidate_evaluation_count == 0


def test_natural_completion_resumes_same_budget_and_all_four_real_families(monkeypatch):
    state, context = equal_case()
    before = fingerprint(state)
    budget = context.factory.budget
    budget.candidate_check_count = 7
    budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
    deadlines = (
        budget.started_at_monotonic,
        budget.search_deadline_monotonic,
        budget.final_deadline_monotonic,
    )
    original = width_optimization._try_width_recipe
    visits = []

    def record(current, bound, recipe):
        assert current is state and bound is context and bound.factory.budget is budget
        visits.append((recipe, budget.candidate_check_count))
        return original(current, bound, recipe)

    monkeypatch.setattr(width_optimization, "_try_width_recipe", record)
    assert run_width_optimization(state, context) is state
    assert {recipe[0] for recipe, _ in visits} == {
        "width_node_move",
        "width_node_exchange",
        "width_block_move",
        "width_block_exchange",
        "width_chain_cut",
        ORDER,
    }
    first_block = next(
        i for i, (recipe, _) in enumerate(visits) if recipe[0].startswith("width_block")
    )
    last_node = max(i for i, (recipe, _) in enumerate(visits) if recipe[0].startswith("width_node"))
    assert first_block < last_node
    assert len(visits) == 91 and [count for _, count in visits] == list(range(8, 99))
    assert len({recipe for recipe, _ in visits}) == 91
    assert budget.candidate_check_count == 98
    assert budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert (
        budget.started_at_monotonic,
        budget.search_deadline_monotonic,
        budget.final_deadline_monotonic,
    ) == deadlines
    assert fingerprint(state) == before
    assert context.complete_candidate_evaluation_count == 0


def test_real_combined_phase_accepts_restarts_then_finishes_without_new_state(monkeypatch):
    state, context = width_case()
    original = width_optimization._node_recipes
    old = nodes_by_id(state.current_plan)
    starts = []

    def record(current, bound):
        starts.append((current.accepted_move_count, bound.factory.budget.candidate_check_count))
        yield from original(current, bound)

    monkeypatch.setattr(width_optimization, "_node_recipes", record)
    assert run_width_optimization(state, context) is state
    assert starts == [(0, 0), (1, 6), (2, 18), (3, 36)]
    assert state.current_evaluation.quality_key == (0, D(0), 0, D(0), D(100), D(0), 2)
    assert [item.quality_after[4] for item in context.accepted_move_traces] == [400, 300, 100]
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == old
    assert state.current_evaluation == evaluate_plan(
        state.current_plan, context.factory.cache.rule_set, context.factory.cache.context
    )
    assert context.factory.budget.candidate_check_count == 78
    assert context.complete_candidate_evaluation_count == state.accepted_move_count == 3
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert state.virtual_sequence == state.split_sequence == 0


def test_real_combined_phase_reaches_block_improvement_and_honestly_reports_cutoff():
    state, context = exchange_case()
    old = nodes_by_id(state.current_plan)
    run_width_optimization(state, context)
    assert state.current_evaluation.quality_key[4] == 300
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == old
    (trace,) = context.accepted_move_traces
    assert trace.action_name == "width_block_exchange" and trace.candidate_check_count == 35
    assert context.complete_candidate_evaluation_count == state.accepted_move_count == 1
    assert context.factory.budget.candidate_check_count == 100
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED


@pytest.mark.parametrize(
    "make_case,family,recipe,gap",
    (
        (width_case, _node_recipes, ("width_node_move", 0, 1, 2, 3, 2, 2), 300),
        (width_case, _node_recipes, ("width_node_exchange", 0, 1, 1, 2, 0, 1), 400),
        (block_move_case, _block_recipes, ("width_block_move", 0, 1, 2, 4, 2, 2), 300),
        (exchange_case, _block_recipes, ("width_block_exchange", 0, 1, 1, 2, 1, 3), 300),
        (width_case, _cut_recipes, ("width_chain_cut", 0, 2, 0, 2), 400),
        (order_case, _order_recipes, (ORDER, 0, 2), 500),
    ),
)
def test_shared_dispatch_builds_and_accepts_each_real_action(make_case, family, recipe, gap):
    state, context = make_case()
    old = nodes_by_id(state.current_plan)
    actual = next(item for item in family(state, context) if item == recipe)
    assert _scan_width_batch(state, context, iter((actual,)), _try_width_recipe, 1) == (True, False)
    assert state.current_evaluation.quality_key[4] == gap
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == old
    assert context.complete_candidate_evaluation_count == state.accepted_move_count == 1
    assert context.factory.budget.candidate_check_count == 1
    assert context.accepted_move_traces[0].action_name == recipe[0]
    assert state.virtual_sequence == state.split_sequence == 0


def test_order_recipes_reuse_legacy_positions_within_each_period_only():
    _, base = width_case()
    periods = ("Z-first", "M-empty", "A-later")
    state, context = search_case(
        (
            chain("A", 1000, periods[0]),
            chain("B", 1500, periods[0]),
            chain("C", 1200, periods[2]),
            chain("D", 1800, periods[2]),
        ),
        active=base.factory.cache.rule_set,
        periods=periods,
    )
    assert width_optimization._chain_order_positions is neighborhoods._chain_order_positions
    assert width_optimization._relocate_chain is neighborhoods._relocate_chain
    assert tuple(_order_recipes(state, context)) == (
        (ORDER, 0, 1),
        (ORDER, 1, 0),
        (ORDER, 2, 3),
        (ORDER, 3, 2),
    )
    assert context.factory.budget.candidate_check_count == 0


def test_order_dispatch_uses_both_protections_and_preserves_whole_chains(monkeypatch):
    state, context = order_case()
    old = state.current_plan.chains
    original = width_optimization.try_complete_candidate
    options = []

    def record(current, bound, candidate, **kwargs):
        options.append(kwargs)
        return original(current, bound, candidate, **kwargs)

    monkeypatch.setattr(width_optimization, "try_complete_candidate", record)
    assert _scan_width_batch(state, context, iter(((ORDER, 0, 2),)), _try_width_recipe, 1) == (
        True,
        False,
    )
    assert state.current_plan.chains == (old[1], old[2], old[0])
    assert options[0]["chain_order_only"] is True
    assert options[0]["width_optimization_only"] is True
    assert options[0]["affected_chain_ids"] == ("A",)
    assert context.complete_candidate_evaluation_count == 1


def test_order_dispatch_cannot_smuggle_a_width_improving_cut(monkeypatch):
    state, context = width_case()
    before = fingerprint(state)
    candidate = joint_candidate(state)
    monkeypatch.setattr(width_optimization, "_relocate_chain", lambda *args: candidate)
    assert _scan_width_batch(state, context, iter(((ORDER, 0, 1),)), _try_width_recipe, 1) == (
        False,
        False,
    )
    assert fingerprint(state) == before
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 0
    assert context.accepted_move_traces == ()


@pytest.mark.parametrize("kind", ("cancel", "time", "quota"))
def test_clearing_old_completion_does_not_renew_actual_runtime_allowance(kind):
    state, context = width_case()
    before = fingerprint(state)
    budget = context.factory.budget
    budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
    budget.candidate_check_count = 7
    if kind == "cancel":
        stop = Stop()
        stop.active = True
        budget.cancellation = stop
        expected = SearchStopReason.USER_CANCELLED
    elif kind == "time":
        budget.clock = lambda: budget.search_deadline_monotonic
        expected = SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    else:
        budget.candidate_check_limit = 7
        context.policy = replace(context.policy, candidate_check_limit=7)
        expected = SearchStopReason.CANDIDATE_LIMIT_REACHED
    run_width_optimization(state, context)
    assert budget.stop_reason is expected
    assert budget.candidate_check_count == 7
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before


@pytest.mark.parametrize(
    "limit,reason",
    (
        (0, SearchStopReason.CANDIDATE_LIMIT_REACHED),
        (1, SearchStopReason.CANDIDATE_LIMIT_REACHED),
        (91, SearchStopReason.LOCAL_SEARCH_COMPLETE),
    ),
)
def test_phase_distinguishes_insufficient_quota_from_exact_natural_exhaustion(limit, reason):
    state, context = equal_case()
    before = fingerprint(state)
    budget = context.factory.budget
    budget.candidate_check_limit = limit
    context.policy = replace(context.policy, candidate_check_limit=limit)
    run_width_optimization(state, context)
    assert budget.candidate_check_count == limit
    assert budget.stop_reason is reason
    assert context.complete_candidate_evaluation_count == 0
    assert fingerprint(state) == before


def test_last_available_check_can_accept_but_does_not_buy_another_scan():
    state, context = order_case()
    budget = context.factory.budget
    budget.candidate_check_limit = 1
    context.policy = replace(context.policy, candidate_check_limit=1)
    old = nodes_by_id(state.current_plan)
    run_width_optimization(state, context)
    assert state.current_evaluation.quality_key[4] == 700
    assert state.current_evaluation.violations == ()
    assert context.complete_candidate_evaluation_count == state.accepted_move_count == 1
    assert budget.candidate_check_count == 1
    assert budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert nodes_by_id(state.current_plan) == old


def test_phase_locates_objective_from_declaration_instead_of_fixed_fifth_position():
    state, context = search_case()
    old = nodes_by_id(state.current_plan)
    assert state.current_evaluation.quality_key[-1] == 800
    run_width_optimization(state, context)
    assert state.current_evaluation.quality_key[-1] == 500
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == old


def test_phase_validates_bound_state_before_skipping_or_scanning():
    state, context = width_case()
    with pytest.raises(ValueError, match="SearchState and SearchContext"):
        run_width_optimization(None, context)
    state.current_evaluation = replace(state.current_evaluation, quality_key=())
    with pytest.raises(ValueError, match="quality does not match"):
        run_width_optimization(state, context)
    assert context.factory.budget.candidate_check_count == 0
