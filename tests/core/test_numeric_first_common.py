"""First-search control uses shared numeric attempts, not old object preparation."""

import pytest

from apsgo_scheduler.core import _numeric_search as search
from apsgo_scheduler.core._numeric_state import NumericCandidateWorkspace
from tests.core.test_numeric_construction import construction_case, budget
from tests.core.test_numeric_search import state_for
from tests.core.test_numeric_candidate_publication import compare_states


@pytest.mark.parametrize("action", ("direct", "bridge", "real", "fill", "order"))
def test_all_first_actions_accept_with_old_object_preparation_disabled(action, monkeypatch):
    options = {}
    rows, offsets, ids, periods = (0, 1), (0, 1, 2), (10, 20), (0, 0)
    function = search.improve_numeric_whole_chain
    arguments = dict(pair_scan_slack_weight=0)
    if action == "bridge":
        options["temperatures"] = (("700", "710"), ("800", "810"))
    elif action == "real":
        options.update(weights=("200", "100", "100"), widths=("1000", "900", "800"))
        rows, offsets = (0, 1, 2), (0, 2, 3)
        function, arguments = search.improve_numeric_real_node_relocation, {}
    elif action == "fill":
        options.update(weights=("195",), widths=("1000",))
        rows, offsets, ids, periods = (0,), (0, 1), (10,), (0,)
        function, arguments = search.improve_numeric_virtual_weight_fill, {}
    elif action == "order":
        options["due_dates"] = ("2026-06-30", "2026-05-31")
        function, arguments = search.improve_numeric_chain_order, {}
    task, program, quality = construction_case(**options)
    state = state_for(task, program, quality, rows, offsets, ids, periods)

    def forbidden(*args, **kwargs):
        raise AssertionError("first search re-entered object preparation")

    for name in ("_layout", "_variants", "_resource_whole_chain_candidate", "apply_numeric_candidate",
                 "_try_prepared_candidate", "extend_resource_workspace", "numeric_rows_prohibited_profile"):
        assert not hasattr(search, name)
    function(task, program, quality, state, budget(candidate_limit=100), **arguments)
    assert state.accepted_move_count >= 1
    if action == "order":
        assert state.accepted_moves[0].affected_rows == ()


def test_first_rejected_candidate_does_not_build_or_extend_formal_state(monkeypatch):
    task, program, quality = construction_case()
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))

    def forbidden(*args, **kwargs):
        raise AssertionError("rejected candidate constructed formal state")

    for name in ("_resource_whole_chain_candidate", "extend_resource_workspace"):
        assert not hasattr(search, name)
    for name in ("_build_plan", "materialize_private_resources", "materialize_numeric_evaluation"):
        monkeypatch.setattr(search, name, forbidden)
    runtime = budget(candidate_limit=1)
    search.improve_numeric_whole_chain(task, program, quality, state, runtime,
        pair_scan_slack_weight=0, maximum_virtual_bridge_nodes=0)
    assert runtime.candidate_check_count == 1
    assert state.accepted_move_count == state.complete_candidate_evaluation_count == state.virtual_sequence == 0


def test_first_capacity_growth_retries_same_description_without_charging_again(monkeypatch):
    task, program, quality = construction_case(temperatures=(("700", "710"), ("800", "810")))
    first = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    second = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    normal_budget, tiny_budget = budget(candidate_limit=100), budget(candidate_limit=100)
    search.improve_numeric_whole_chain(task, program, quality, first, normal_budget, pair_scan_slack_weight=0)
    original = search._first_workspace

    def tiny(state):
        _, *rest = original(state)
        work = NumericCandidateWorkspace.allocate(state.task, state.plan, changed_capacity=0,
            chain_capacity=state.plan.chain_ids.size, node_capacity=0, group_capacity=0, event_capacity=0)
        return work, *rest

    monkeypatch.setattr(search, "_first_workspace", tiny)
    search.improve_numeric_whole_chain(task, program, quality, second, tiny_budget, pair_scan_slack_weight=0)
    compare_states(first, second)
    assert normal_budget.candidate_check_count == tiny_budget.candidate_check_count
    assert first.bridge_required_candidate_count == second.bridge_required_candidate_count
