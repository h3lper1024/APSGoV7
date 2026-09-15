"""Post-search numeric refinement keeps actions, resources and shared quota coherent."""

import apsgo_scheduler.core._numeric_refinement as refinement
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan
from apsgo_scheduler.core._numeric_refinement import (
    NumericRefinementDiagnostics,
    _critical_sources,
    _round_robin,
    _scan_family,
    _source_recipe_stream,
    improve_numeric_refinement,
    run_numeric_serial_search,
)
from apsgo_scheduler.core._numeric_resources import extend_resource_workspace, virtual_node
from apsgo_scheduler.core._numeric_search import NumericSearchAction, NumericSearchState
from apsgo_scheduler.core._numeric_state import NumericPlan
from apsgo_scheduler.core.contracts import SearchStopReason
from apsgo_scheduler.core.model import VirtualPurpose
from tests.core.test_numeric_construction import budget, construction_case
from tests.core.test_numeric_search import initial_for


def _state(task, program, quality, rows, offsets, ids, periods, *, virtual_sequence=0):
    plan = NumericPlan.build(task, rows, offsets, ids, periods)
    return NumericSearchState(
        task,
        program,
        quality,
        plan,
        evaluate_numeric_plan(task, program, quality, plan),
        virtual_sequence=virtual_sequence,
    )


def test_refinement_recipe_families_cover_all_structural_granularities():
    task, program, quality = construction_case(
        weights=("100",) * 6,
        widths=("1000", "990", "980", "970", "960", "950"),
    )
    state = _state(task, program, quality, range(6), (0, 3, 6), (10, 20), (0, 0))

    actions = {
        family: {recipe[0] for recipe in _source_recipe_stream(state, 4, family)}
        for family in ("intra", "node", "block", "cut", "order")
    }

    assert actions["intra"] == {NumericSearchAction.DELIVERY_INTRA_MOVE}
    assert actions["node"] == {
        NumericSearchAction.NODE_MOVE,
        NumericSearchAction.NODE_EXCHANGE,
    }
    assert actions["block"] == {
        NumericSearchAction.BLOCK_MOVE,
        NumericSearchAction.BLOCK_EXCHANGE,
    }
    assert actions["cut"] == {NumericSearchAction.CHAIN_CUT}
    assert actions["order"] == {NumericSearchAction.CHAIN_ORDER_RELOCATION}


def test_refinement_keeps_four_proposals_per_source_and_sixty_four_per_family(
    monkeypatch,
):
    runtime = budget(candidate_limit=100)
    assert list(_round_robin((range(6), range(10, 16)), runtime, 4)) == [
        0,
        1,
        2,
        3,
        10,
        11,
        12,
        13,
        4,
        5,
        14,
        15,
    ]

    monkeypatch.setattr(
        refinement,
        "_try_recipe",
        lambda state, current, recipe, maximum_virtual_bridge_nodes, *args: False,
    )
    runtime = budget(candidate_limit=100)
    accepted, exhausted = _scan_family(None, runtime, iter(range(100)))
    assert (accepted, exhausted) == (False, False)
    assert runtime.candidate_check_count == 64


def test_refinement_diagnostics_separate_generated_and_consumed_work(monkeypatch):
    diagnostics = NumericRefinementDiagnostics()
    runtime = budget(candidate_limit=2)
    state = type("State", (), {"complete_candidate_evaluation_count": 0})()
    monkeypatch.setattr(refinement, "_try_recipe", lambda *args: False)

    accepted, exhausted = _scan_family(
        state,
        runtime,
        iter(("first", "second", "third")),
        diagnostics=diagnostics,
        diagnostic_key="regular:node",
    )

    assert (accepted, exhausted) == (False, False)
    assert runtime.candidate_check_count == 2
    assert diagnostics.candidate_checks == {"regular:node": 2}
    assert diagnostics.complete_evaluations == {}
    assert diagnostics.maximum_generator_advance_seconds >= 0


def test_refinement_reclaims_only_ordinary_bridge_and_keeps_split_separator():
    task, program, quality = construction_case(
        weights=("100",) * 7,
        widths=("1000", "990", "980", "970", "960", "950", "940"),
    )
    ordinary = virtual_node(task, 0, 0, 1, purpose=VirtualPurpose.EDGE_BRIDGE, sequence=1)
    first = extend_resource_workspace(task, program, quality, (ordinary,))
    separator = virtual_node(
        first.task, 0, 1, 2, purpose=VirtualPurpose.SPLIT_SEPARATOR, sequence=2
    )
    workspace = extend_resource_workspace(first.task, first.program, first.quality, (separator,))
    ordinary_row, separator_row = first.rows[0], workspace.rows[0]
    rows = (0, ordinary_row, 1, separator_row, 2, 3, 4, 5, 6)
    state = _state(
        workspace.task,
        workspace.program,
        workspace.quality,
        rows,
        (0, len(rows)),
        (10,),
        (0,),
        virtual_sequence=2,
    )
    runtime = budget(candidate_limit=1)

    improve_numeric_refinement(state, runtime)

    assert ordinary_row not in state.plan.node_rows
    assert separator_row in state.plan.node_rows
    assert state.accepted_moves[0].action is NumericSearchAction.BRIDGE_RECLAMATION
    assert state.virtual_sequence == 2


def test_critical_source_order_and_complete_serial_runner_share_numeric_state():
    task, program, quality = construction_case(
        weights=("100",) * 7,
        widths=("1000", "990", "980", "970", "960", "950", "940"),
        due_dates=(
            "2026-05-31",
            "2026-06-30",
            "2026-06-30",
            "2026-06-30",
            "2026-06-30",
            "2026-06-30",
            "2026-06-30",
        ),
    )
    state = _state(task, program, quality, range(7), (0, 7), (10,), (0,))
    assert _critical_sources(state)[0] == 0

    runtime = budget(candidate_limit=20)
    result, checkpoint = run_numeric_serial_search(
        task,
        program,
        quality,
        initial_for(state),
        runtime,
        pair_scan_slack_weight=0,
    )

    assert result.program.task_fingerprint == result.task.fingerprint
    assert result.quality.task_fingerprint == result.task.fingerprint
    assert result.plan.task_fingerprint == result.task.fingerprint
    assert result.evaluation.task_fingerprint == result.task.fingerprint
    assert checkpoint.task_fingerprint == result.task.fingerprint
    assert checkpoint.candidate_check_count == runtime.candidate_check_count
    assert checkpoint.stop_reason in {
        SearchStopReason.LOCAL_SEARCH_COMPLETE,
        SearchStopReason.CANDIDATE_LIMIT_REACHED,
    }
