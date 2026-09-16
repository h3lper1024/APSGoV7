"""Post-search numeric refinement keeps actions, resources and shared quota coherent."""

import numpy as np

import apsgo_scheduler.core._numeric_refinement as refinement
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan
from apsgo_scheduler.core._numeric_refinement import (
    NumericRefinementDiagnostics,
    NumericRefinementIndex,
    _critical_sources,
    _delivery_sources,
    _direct_slots,
    _family_stream,
    _recipe_real_sources,
    _round_robin,
    _scan_family,
    _source_recipe_stream,
    improve_numeric_refinement,
    run_numeric_serial_search,
)
from apsgo_scheduler.core._numeric_resources import (
    extend_resource_workspace,
    split_piece_node,
    virtual_node,
)
from apsgo_scheduler.core._numeric_search import NumericSearchAction, NumericSearchState
from apsgo_scheduler.core._numeric_state import NumericPlan, NumericSplitGroup, readonly
from apsgo_scheduler.core._numeric_units import allocate_piece_milliseconds
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

    sources = _delivery_sources(state)
    actions = {
        family: {
            recipe[0]
            for source in sources
            for recipe in _source_recipe_stream(
                state,
                source,
                family,
                ordered_sources=sources,
                owner_sources=sources,
            )
        }
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
    monkeypatch.setattr(refinement, "_prepare_recipe_batch", lambda s, b, r, *a: [None] * len(r))
    accepted, exhausted = _scan_family(None, runtime, iter(range(100)))
    assert (accepted, exhausted) == (False, False)
    assert runtime.candidate_check_count == 64


def test_refinement_diagnostics_separate_generated_and_consumed_work(monkeypatch):
    diagnostics = NumericRefinementDiagnostics()
    runtime = budget(candidate_limit=2)
    state = type("State", (), {"complete_candidate_evaluation_count": 0})()
    monkeypatch.setattr(refinement, "_try_recipe", lambda *args: False)
    monkeypatch.setattr(refinement, "_prepare_recipe_batch", lambda s, b, r, *a: [None] * len(r))

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


def test_serial_batch_consumes_in_order_and_discards_stale_descriptions(monkeypatch):
    diagnostics = NumericRefinementDiagnostics()
    runtime = budget(candidate_limit=100)
    state = type("State", (), {"complete_candidate_evaluation_count": 0})()
    cursor = {"node": None}
    consumed = []

    def accept_first(_state, _budget, recipe, *_args):
        consumed.append(recipe)
        return True

    monkeypatch.setattr(refinement, "_try_recipe", accept_first)
    monkeypatch.setattr(refinement, "_prepare_recipe_batch", lambda s, b, r, *a: [None] * len(r))
    accepted, exhausted = _scan_family(
        state,
        runtime,
        iter(((3, "first"), (5, "stale-a"), (7, "stale-b"))),
        diagnostics=diagnostics,
        diagnostic_key="regular:node",
        cursor=cursor,
        family="node",
        _batch_size=3,
    )

    assert (accepted, exhausted) == (True, False)
    assert consumed == ["first"]
    assert cursor == {"node": 3}
    assert runtime.candidate_check_count == 1
    assert diagnostics.batch_prepared == {"regular:node": 3}
    assert diagnostics.batch_consumed == {"regular:node": 1}
    assert diagnostics.batch_accepted == {"regular:node": 1}
    assert diagnostics.batch_stale == {"regular:node": 2}
    assert diagnostics.maximum_batch_size == 3


def test_serial_batch_cancellation_does_not_consume_prefetched_descriptions(monkeypatch):
    flag = type("Flag", (), {"cancelled": False})()
    cancellation = type(
        "Cancellation", (), {"is_cancelled": lambda _self: flag.cancelled}
    )()
    diagnostics = NumericRefinementDiagnostics()
    runtime = budget(candidate_limit=100, cancellation=cancellation)
    state = type("State", (), {"complete_candidate_evaluation_count": 0})()

    def cancel_after_first(*_args):
        flag.cancelled = True
        return False

    monkeypatch.setattr(refinement, "_try_recipe", cancel_after_first)
    monkeypatch.setattr(refinement, "_prepare_recipe_batch", lambda s, b, r, *a: [None] * len(r))
    accepted, exhausted = _scan_family(
        state,
        runtime,
        iter(("first", "cancelled-a", "cancelled-b")),
        diagnostics=diagnostics,
        diagnostic_key="regular:node",
        _batch_size=3,
    )

    assert (accepted, exhausted) == (False, False)
    assert runtime.candidate_check_count == 1
    assert runtime.stop_reason is SearchStopReason.USER_CANCELLED
    assert diagnostics.batch_rejected == {"regular:node": 1}
    assert diagnostics.batch_cancelled == {"regular:node": 2}
    assert diagnostics.batch_stopped == {"regular:node": 2}


def test_refinement_generates_critical_lane_without_post_filter(monkeypatch):
    task, program, quality = construction_case(
        weights=("100",) * 6,
        widths=("1000", "990", "980", "970", "960", "950"),
        due_dates=("2026-05-31",) + ("2026-06-30",) * 5,
    )
    state = _state(task, program, quality, range(6), (0, 3, 6), (10, 20), (0, 0))
    base = NumericRefinementIndex.build(state)
    critical = frozenset(_critical_sources(state, base))
    index = base.with_critical_sources(state, critical)
    expected = tuple(_source_recipe_stream(state, 0, "node", index=index))
    expected = tuple(recipe for recipe in expected if index.recipe_is_critical(recipe))
    assert not hasattr(refinement, "_layout")
    monkeypatch.setattr(
        NumericRefinementIndex,
        "recipe_is_critical",
        lambda *args: pytest.fail("lane ownership must be decided during generation"),
    )

    actual = tuple(
        _family_stream(
            state,
            "node",
            {family: None for family in refinement._FAMILIES},
            _delivery_sources(state),
            critical,
            True,
            budget(candidate_limit=100),
            index=index,
        )
    )

    assert actual == expected
    assert all(_recipe_real_sources(state, recipe, index=index) & critical for recipe in actual)


def test_refinement_routes_each_recipe_once_and_polls_cancellation():
    task, program, quality = construction_case(
        weights=("100",) * 6,
        widths=("1000", "990", "980", "970", "960", "950"),
        due_dates=("2026-05-31",) + ("2026-06-30",) * 5,
    )
    state = _state(task, program, quality, range(6), (0, 3, 6), (10, 20), (0, 0))
    base = NumericRefinementIndex.build(state)
    critical = frozenset(_critical_sources(state, base))
    index = base.with_critical_sources(state, critical)
    recipes = []
    for lane in (True, False):
        recipes.extend(
            _family_stream(
                state,
                "node",
                {family: None for family in refinement._FAMILIES},
                _delivery_sources(state),
                critical,
                lane,
                budget(candidate_limit=1000),
                index=index,
            )
        )
    assert len(recipes) == len(set(recipes))

    one_chain = _state(task, program, quality, range(6), (0, 6), (10,), (0,))
    one_index = NumericRefinementIndex.build(one_chain)
    diagnostics = NumericRefinementDiagnostics()
    cut_recipes = tuple(
        _family_stream(
            one_chain,
            "cut",
            {family: None for family in refinement._FAMILIES},
            _delivery_sources(one_chain),
            frozenset(),
            False,
            budget(candidate_limit=1000),
            diagnostics=diagnostics,
            index=one_index,
        )
    )
    assert len(cut_recipes) == len(set(cut_recipes)) == 10
    assert diagnostics.raw_combinations == {"regular:cut": 10}
    assert diagnostics.unique_combinations == {"regular:cut": 10}
    assert "duplicate_filtered" not in diagnostics.snapshot()

    class Cancelled:
        @staticmethod
        def is_cancelled():
            return True

    runtime = budget(cancellation=Cancelled(), candidate_limit=100)
    assert _direct_slots(state, 0, index.chains[1], range(4), runtime) == ()
    assert runtime.stop_reason is SearchStopReason.USER_CANCELLED


def test_block_family_builds_shared_structural_ownership_once(monkeypatch):
    task, program, quality = construction_case(
        weights=("100",) * 6,
        widths=("1000", "990", "980", "970", "960", "950"),
    )
    state = _state(task, program, quality, range(6), (0, 3, 6), (10, 20), (0, 0))
    index = NumericRefinementIndex.build(state)
    sources = _delivery_sources(state)
    original = refinement._structural_ownership
    original_chain_order = refinement._chain_order
    calls = []
    chain_order_calls = []

    def counted(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    def counted_chain_order(*args, **kwargs):
        chain_order_calls.append((args, kwargs))
        return original_chain_order(*args, **kwargs)

    monkeypatch.setattr(refinement, "_structural_ownership", counted)
    monkeypatch.setattr(refinement, "_chain_order", counted_chain_order)
    recipes = tuple(
        _family_stream(
            state,
            "block",
            {family: None for family in refinement._FAMILIES},
            sources,
            frozenset(),
            False,
            budget(candidate_limit=1000),
            index=index,
        )
    )

    assert recipes
    assert len(calls) == 1
    assert calls[0][1] == {"include_intervals": True}
    assert len(chain_order_calls) == 1


def test_block_interval_is_owned_once_when_one_order_has_multiple_pieces():
    task, program, quality = construction_case(
        weights=("100",) * 6,
        widths=("1000", "990", "980", "970", "960", "950"),
    )
    count = task.nodes.weight.size
    first_weight = int(task.nodes.weight[0]) // 2
    piece_weights = (first_weight, int(task.nodes.weight[0]) - first_weight)
    piece_durations = allocate_piece_milliseconds(
        int(task.nodes.weight[0]),
        int(task.nodes.duration_ms[0]),
        piece_weights,
        "test_piece_duration",
    )
    group = NumericSplitGroup(
        0,
        0,
        int(task.nodes.resource[0]),
        int(task.nodes.weight[0]),
        int(task.nodes.duration_ms[0]),
        int(task.nodes.source_period[0]),
        0,
        0,
        0,
        0,
        1,
    )
    pieces = tuple(
        split_piece_node(
            task,
            0,
            group_index=0,
            piece_index=piece_index,
            piece_count=2,
            weight=piece_weight,
            duration_ms=piece_duration,
            accepted_sequence=1,
        )
        for piece_index, (piece_weight, piece_duration) in enumerate(
            zip(piece_weights, piece_durations)
        )
    )
    workspace = extend_resource_workspace(
        task, program, quality, pieces, split_group=group
    )
    state = _state(
        workspace.task,
        workspace.program,
        workspace.quality,
        (count, count + 1, 1, 2, 3, 4, 5),
        (0, 4, 7),
        (10, 20),
        (0, 0),
    )
    sources = tuple(range(6))

    recipes = tuple(
        _source_recipe_stream(
            state,
            0,
            "block",
            ordered_sources=sources,
            owner_sources=sources,
        )
    )

    assert recipes
    assert len(recipes) == len(set(recipes))


def test_refinement_rejects_split_piece_outside_authorized_target_period(monkeypatch):
    task, program, quality = construction_case(
        weights=("100", "100", "100"),
        widths=("1000", "990", "980"),
        source_periods=("P0", "P1", "P0"),
    )
    count = task.nodes.weight.size
    piece_weights = (5000, 5000)
    piece_durations = allocate_piece_milliseconds(
        int(task.nodes.weight[0]),
        int(task.nodes.duration_ms[0]),
        piece_weights,
        "test_piece_duration",
    )
    group = NumericSplitGroup(
        0,
        0,
        int(task.nodes.resource[0]),
        int(task.nodes.weight[0]),
        int(task.nodes.duration_ms[0]),
        0,
        0,
        1,
        1,
        0,
        1,
    )
    pieces = tuple(
        split_piece_node(
            task,
            0,
            group_index=0,
            piece_index=index,
            piece_count=2,
            weight=weight,
            duration_ms=duration,
            accepted_sequence=1,
        )
        for index, (weight, duration) in enumerate(zip(piece_weights, piece_durations))
    )
    workspace = extend_resource_workspace(
        task, program, quality, pieces, split_group=group
    )
    state = _state(
        workspace.task,
        workspace.program,
        workspace.quality,
        (2, count, count + 1, 1),
        (0, 1, 4),
        (10, 20),
        (0, 1),
    )
    runtime = budget(candidate_limit=1)
    assert runtime.consume_candidate_check()
    chains = (
        readonly((2, count), np.int64),
        readonly((count + 1, 1), np.int64),
    )
    overlay = refinement._candidate_overlay(
        state.task, state.plan, chains, (10, 20), (0, 1), group_periods=False
    )
    edit = refinement._edit_for(
        state,
        (NumericSearchAction.NODE_MOVE, 20, 10, 0, 1, 1, -1),
        runtime.candidate_check_count,
    )
    monkeypatch.setattr(
        refinement,
        "summarize_numeric_candidate",
        lambda *args, **kwargs: pytest.fail("invalid split candidate must not be evaluated"),
    )

    assert not refinement._try_overlay_candidate(
        state,
        runtime,
        edit,
        state.task,
        state.program,
        state.quality,
        overlay,
    )
    assert state.complete_candidate_evaluation_count == 0


def test_direct_ownership_preserves_candidate_budget_and_acceptance_order():
    task, program, quality = construction_case(
        weights=("100",) * 12,
        widths=(
            "1000",
            "990",
            "980",
            "970",
            "800",
            "790",
            "780",
            "770",
            "900",
            "890",
            "880",
            "870",
        ),
    )
    state = _state(
        task,
        program,
        quality,
        range(12),
        (0, 4, 8, 12),
        (10, 20, 30),
        (0, 0, 0),
    )
    runtime = budget(candidate_limit=1000)
    diagnostics = NumericRefinementDiagnostics()

    improve_numeric_refinement(state, runtime, diagnostics=diagnostics)

    assert runtime.candidate_check_count == 1000
    assert runtime.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert tuple(move.sequence for move in state.accepted_moves) == (
        6,
        7,
        28,
        58,
        117,
        544,
        625,
        629,
    )
    assert state.evaluation.quality_key.tolist() == [0, 0, 0, 0, 0, 0, 30, 0, 4]
    assert state.plan.node_rows.tolist() == [0, 1, 2, 3, 10, 9, 8, 11, 5, 7, 6, 4]
    assert state.plan.chain_offsets.tolist() == [0, 3, 5, 10, 12]
    assert sum(diagnostics.accepted.values()) == 8
    assert sum(diagnostics.plan_materializations.values()) == 8


def test_serial_batches_preserve_first_improvement_trace_and_result():
    def solve(batch_size):
        task, program, quality = construction_case(
            weights=("100",) * 12,
            widths=(
                "1000",
                "990",
                "980",
                "970",
                "800",
                "790",
                "780",
                "770",
                "900",
                "890",
                "880",
                "870",
            ),
        )
        state = _state(
            task,
            program,
            quality,
            range(12),
            (0, 4, 8, 12),
            (10, 20, 30),
            (0, 0, 0),
        )
        runtime = budget(candidate_limit=1000)
        diagnostics = NumericRefinementDiagnostics()
        improve_numeric_refinement(
            state,
            runtime,
            diagnostics=diagnostics,
            _batch_size=batch_size,
        )
        return (
            state.plan.node_rows.tolist(),
            state.plan.chain_offsets.tolist(),
            state.plan.chain_ids.tolist(),
            state.plan.chain_periods.tolist(),
            state.evaluation.quality_key.tolist(),
            state.accepted_moves,
            state.complete_candidate_evaluation_count,
            state.virtual_sequence,
            state.split_sequence,
            runtime.candidate_check_count,
            runtime.stop_reason,
        ), diagnostics

    single, single_diagnostics = solve(1)
    batched, batched_diagnostics = solve(8)

    assert batched == single
    assert single_diagnostics.maximum_batch_size == 1
    assert batched_diagnostics.maximum_batch_size == 8
    assert sum(batched_diagnostics.batch_stale.values()) > 0
    assert batched_diagnostics.numeric_batch_calls > 0
    assert sum(batched_diagnostics.numeric_discarded.values()) > 0
    assert sum(batched_diagnostics.numeric_precomputed.values()) == (
        sum(batched_diagnostics.numeric_consumed.values())
        + sum(batched_diagnostics.numeric_discarded.values())
    )


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
    diagnostics = NumericRefinementDiagnostics()

    improve_numeric_refinement(state, runtime, diagnostics=diagnostics)

    assert ordinary_row not in state.plan.node_rows
    assert separator_row in state.plan.node_rows
    assert state.accepted_moves[0].action is NumericSearchAction.BRIDGE_RECLAMATION
    assert state.virtual_sequence == 2
    assert sum(diagnostics.plan_materializations.values()) == 1
    assert sum(diagnostics.accepted.values()) == 1
    assert diagnostics.layout_call_count == 0


def test_rejected_refinement_overlay_does_not_materialize_formal_plan(monkeypatch):
    task, program, quality = construction_case(
        weights=("100", "100"),
        widths=("1000", "900"),
    )
    state = _state(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    runtime = budget(candidate_limit=1)
    assert runtime.consume_candidate_check()
    diagnostics = NumericRefinementDiagnostics()
    recipe = (
        NumericSearchAction.CHAIN_ORDER_RELOCATION,
        10,
        20,
        -1,
        -1,
        1,
        -1,
    )
    monkeypatch.setattr(
        refinement,
        "_build_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    import apsgo_scheduler.core._numeric_evaluation as evaluation
    def reject_detail_object(*_args, **_kwargs):
        raise AssertionError("rejected production candidate created detail objects")
    for cls in (evaluation.NumericPlanEvaluation, evaluation.NumericRuleResult,
                evaluation.NumericViolation, evaluation.NumericMetric):
        monkeypatch.setattr(cls, "__post_init__", reject_detail_object)

    assert not refinement._try_recipe(
        state,
        runtime,
        recipe,
        2,
        diagnostics,
        "regular:order",
        NumericRefinementIndex.build(state),
    )
    assert state.complete_candidate_evaluation_count == 1
    assert diagnostics.plan_materializations == {}


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
