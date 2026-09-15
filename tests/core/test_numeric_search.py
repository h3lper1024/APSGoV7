"""Numeric edits keep identity, budget and first-improvement acceptance atomic."""

from dataclasses import replace

import pytest

from apsgo_scheduler.core._numeric_construction import NumericInitialSolution
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan
from apsgo_scheduler.core._numeric_search import (
    NumericCandidateEdit,
    NumericSearchAction,
    NumericSearchState,
    apply_numeric_candidate,
    improve_numeric_chain_order,
    improve_numeric_real_node_relocation,
    improve_numeric_virtual_weight_fill,
    improve_numeric_whole_chain,
    run_numeric_first_search_prefix,
    run_numeric_search_with_split_replay,
)
from apsgo_scheduler.core._numeric_state import NumericPlan
from apsgo_scheduler.core._numeric_units import NumericValueError
from apsgo_scheduler.core.contracts import SearchStopReason
from tests.core.test_numeric_construction import budget, construction_case


def state_for(task, program, quality, rows, offsets, ids, periods):
    plan = NumericPlan.build(task, rows, offsets, ids, periods)
    return NumericSearchState(
        task,
        program,
        quality,
        plan,
        evaluate_numeric_plan(task, program, quality, plan),
    )


def initial_for(state):
    return NumericInitialSolution(
        "graph",
        "cover",
        state.plan,
        state.evaluation,
        None,
        0.0,
        "initial",
    )


def test_whole_chain_merge_consumes_candidates_and_accepts_first_improvement():
    task, program, quality = construction_case()
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    runtime = budget(candidate_limit=100)

    improve_numeric_whole_chain(task, program, quality, state, runtime, pair_scan_slack_weight=0)

    assert state.plan.chain_ids.tolist() == [20]
    assert state.plan.node_rows.tolist() == [0, 1]
    assert state.accepted_move_count == 1
    assert state.complete_candidate_evaluation_count == 1
    assert runtime.candidate_check_count == 2
    assert state.accepted_moves[0].action is NumericSearchAction.WHOLE_CHAIN_PREPEND
    assert state.accepted_moves[0].affected_sources == (0, 1)


def test_real_node_relocation_uses_stable_row_and_preserves_source_chain():
    task, program, quality = construction_case(
        weights=("200", "100", "100"), widths=("1000", "900", "800")
    )
    state = state_for(task, program, quality, (0, 1, 2), (0, 2, 3), (10, 20), (0, 0))
    runtime = budget(candidate_limit=100)

    improve_numeric_real_node_relocation(task, program, quality, state, runtime)

    assert state.plan.chain_ids.tolist() == [10, 20]
    assert state.plan.node_rows.tolist() == [0, 1, 2]
    assert state.plan.chain_offsets.tolist() == [0, 1, 3]
    assert state.accepted_move_count == 1
    assert state.accepted_moves[0].affected_rows == (1,)


def test_chain_order_uses_stable_anchor_and_accepts_delivery_improvement():
    task, program, quality = construction_case(
        weights=("100", "100"),
        widths=("1000", "900"),
        due_dates=("2026-06-30", "2026-05-31"),
    )
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    runtime = budget(candidate_limit=100)

    improve_numeric_chain_order(task, program, quality, state, runtime)

    assert state.plan.chain_ids.tolist() == [20, 10]
    assert state.accepted_move_count == 1
    assert state.accepted_moves[0].affected_chain_ids == (20,)


def test_candidate_identity_and_budget_stop_prevent_stale_or_late_publication():
    task, program, quality = construction_case(weights=("400", "400"))
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    edit = NumericCandidateEdit(
        task.fingerprint,
        state.plan.fingerprint,
        state.plan.generation,
        1,
        NumericSearchAction.WHOLE_CHAIN_APPEND,
        10,
        20,
        target_position=1,
    )
    with pytest.raises(NumericValueError, match="stale"):
        apply_numeric_candidate(task, replace(state.plan, generation=1), edit)

    runtime = budget()
    improve_numeric_whole_chain(task, program, quality, state, runtime, pair_scan_slack_weight=0)
    assert state.accepted_move_count == 0
    assert state.plan.chain_ids.tolist() == [10, 20]

    with pytest.raises(NumericValueError, match="matching numeric"):
        improve_numeric_real_node_relocation(
            replace(task, fingerprint="other"), program, quality, state, budget()
        )


def test_non_direct_whole_chain_candidate_uses_private_bridge_and_publishes_only_selected_row():
    task, program, quality = construction_case(temperatures=(("700", "710"), ("800", "810")))
    starting_capacity = task.nodes.weight.size
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    runtime = budget(candidate_limit=100)

    improve_numeric_whole_chain(task, program, quality, state, runtime, pair_scan_slack_weight=0)

    assert state.accepted_move_count == state.virtual_sequence == 1
    assert state.bridge_required_candidate_count > 0
    assert state.complete_candidate_evaluation_count == 1
    assert state.task.nodes.weight.size == starting_capacity + 1
    assert state.plan.node_rows.size == 3
    assert state.accepted_moves[0].affected_rows == (starting_capacity,)


def test_missing_bridge_or_rejected_fill_does_not_publish_private_rows_or_sequences():
    task, program, quality = construction_case(temperatures=(("700", "710"), ("800", "810")))
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    runtime = budget(candidate_limit=100)

    improve_numeric_whole_chain(
        task,
        program,
        quality,
        state,
        runtime,
        pair_scan_slack_weight=0,
        maximum_virtual_bridge_nodes=0,
    )

    assert state.task is task and state.virtual_sequence == 0
    assert state.task.nodes.weight.size == task.nodes.weight.size
    assert state.accepted_move_count == state.complete_candidate_evaluation_count == 0


def test_virtual_fill_publishes_one_selected_row_and_advances_only_accepted_sequence():
    task, program, quality = construction_case(weights=("195",), widths=("1000",))
    state = state_for(task, program, quality, (0,), (0, 1), (10,), (0,))
    runtime = budget(candidate_limit=100)
    starting_capacity = task.nodes.weight.size

    improve_numeric_virtual_weight_fill(task, program, quality, state, runtime)

    assert state.accepted_move_count == state.virtual_sequence == 1
    assert state.task.nodes.weight.size == starting_capacity + 1
    assert state.plan.node_rows.size == 2
    generated = int(state.plan.node_rows[0])
    assert generated == starting_capacity
    assert state.task.node_ids[generated] == "virtual-000001"
    assert state.evaluation.quality_key.tolist()[2:4] == [0, 0]


@pytest.mark.parametrize(
    "source_period,expected_mode", (("P0", 0), ("P1", 1))
)
def test_controlled_split_conserves_weight_and_duration_then_replays_once(
    source_period, expected_mode
):
    task, program, quality = construction_case(
        weights=("600",),
        widths=("1000",),
        grade_class="IF钢",
        source_periods=(source_period,),
    )
    original = state_for(task, program, quality, (0,), (0, 1), (10,), (0,))
    runtime = budget(candidate_limit=1000)

    state, checkpoint = run_numeric_search_with_split_replay(
        task,
        program,
        quality,
        initial_for(original),
        runtime,
        pair_scan_slack_weight=0,
    )

    assert state.split_sequence == state.replay_count == 1
    assert state.virtual_sequence == 1
    assert state.task.split_groups.parent_row.tolist() == [0]
    assert state.task.split_groups.mode.tolist() == [expected_mode]
    assert state.task.split_groups.target_period.tolist() == [expected_mode]
    piece_rows = state.plan.source_piece_rows
    assert state.task.nodes.weight[piece_rows].tolist() == [50000, 10000]
    assert int(state.task.nodes.duration_ms[piece_rows].sum()) == int(task.originals.duration_ms[0])
    assert int(state.task.nodes.weight[piece_rows].sum()) == int(task.originals.weight[0])
    assert checkpoint.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


def test_first_search_prefix_runs_whole_chain_then_real_node_without_dynamic_material():
    task, program, quality = construction_case()
    original = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    runtime = budget(candidate_limit=100)

    state, checkpoint = run_numeric_first_search_prefix(
        task,
        program,
        quality,
        initial_for(original),
        runtime,
        pair_scan_slack_weight=0,
    )

    assert state.plan.chain_ids.tolist() == [20]
    assert checkpoint.plan_fingerprint == state.plan.fingerprint
    assert checkpoint.accepted_move_count == 1
    assert checkpoint.stop_reason is None
