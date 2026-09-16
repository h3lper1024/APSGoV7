"""Flat serial batches preserve every numeric output and private resource boundary."""
from dataclasses import replace
from itertools import permutations

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_refinement as refinement
from apsgo_scheduler.core._numeric_batch import (
    evaluate_numeric_batch, numeric_batch_result, pack_numeric_candidates,
)
from apsgo_scheduler.core._numeric_evaluation import summarize_numeric_candidate
from apsgo_scheduler.core._numeric_kernel import CANCELLED, STALE, evaluate_batch_kernel
from apsgo_scheduler.core._numeric_resources import extend_resource_workspace, virtual_node
from apsgo_scheduler.core._numeric_state import NumericPlanOverlay, readonly
from apsgo_scheduler.core._numeric_units import NumericValueError
from apsgo_scheduler.core.contracts import SearchStopReason
from apsgo_scheduler.core.model import VirtualPurpose
from tests.core.test_numeric_construction import construction_case, budget
from tests.core.test_numeric_refinement import _state


def sample():
    task, rules, quality = construction_case(
        weights=("100", "210", "350"), widths=("1000", "1010", "900"),
        due_dates=("2026-05-31", "2026-06-01", "2026-06-01"),
        duration_hours=("1", "24", "2"),
    )
    return _state(task, rules, quality, (0, 1, 2), (0, 1, 3), (10, 20), (0, 0))


def pack(state, candidates, **kwargs):
    return pack_numeric_candidates(
        state.task, state.program, state.quality, state.plan, state.evaluation,
        candidates, range(1, len(candidates) + 1), [0] * len(candidates), **kwargs,
    )


def compare(state, candidates):
    flat = pack(state, candidates)
    result = evaluate_numeric_batch(flat, state.task, state.program, state.quality, state.plan)
    for i, candidate in enumerate(candidates):
        expected = summarize_numeric_candidate(
            *candidate, state.task, state.program, state.quality, state.plan, state.evaluation,
        )
        actual = numeric_batch_result(flat, result, i)
        for name in expected._fields:
            np.testing.assert_array_equal(getattr(actual, name), getattr(expected, name), err_msg=name)
            assert not getattr(actual, name).flags.writeable
    assert evaluate_batch_kernel.nopython_signatures
    for value in vars_as_arrays(flat):
        assert value.dtype != object and not value.flags.writeable
    return flat, result


def vars_as_arrays(flat):
    from dataclasses import fields
    for field in fields(flat):
        value = getattr(flat, field.name)
        if isinstance(value, np.ndarray):
            yield value
    yield from flat.columns


def test_flat_batch_matches_all_outputs_for_different_shapes_and_late_orders():
    state = sample()
    candidates = []
    for rows in permutations(range(3)):
        for offsets in ((0, 3), (0, 1, 3), (0, 2, 3), (0, 1, 2, 3)):
            chains = [rows[a:b] for a, b in zip(offsets, offsets[1:])]
            overlay = NumericPlanOverlay.build(
                state.task, state.plan, chains, range(10, 10 + len(chains)), [0] * len(chains),
            )
            candidates.append((state.task, state.program, state.quality, overlay))
    for start in range(0, len(candidates), 8):
        flat, _ = compare(state, candidates[start:start + 8])
        assert flat.changed_chain_offsets.size == flat.candidate_offsets.size
    compare(state, candidates[:1])


def test_candidate_private_virtual_rows_are_disjoint_without_reserving_formal_ids():
    state = sample()
    candidates = []
    for left, right in ((0, 1), (1, 2)):
        node = virtual_node(state.task, 0, left, right, purpose=VirtualPurpose.EDGE_BRIDGE, sequence=1)
        extension = extend_resource_workspace(state.task, state.program, state.quality, (node,))
        overlay = NumericPlanOverlay.build(
            extension.task, state.plan, ((0, extension.rows[0], 1, 2),), (10,), (0,),
        )
        candidates.append((extension.task, extension.program, extension.quality, overlay))
    flat, _ = compare(state, candidates)
    assert flat.resource_row_offsets.tolist() == [0, 1, 2]
    assert flat.resource_metadata.shape == (2, 8)
    assert flat.node_rows[1] != flat.node_rows[5]
    assert state.virtual_sequence == state.split_sequence == 0
    assert state.plan.generation == 0


def test_private_split_metadata_and_completion_are_preserved():
    from apsgo_scheduler.core._numeric_resources import split_piece_node
    from apsgo_scheduler.core._numeric_state import NumericSplitGroup
    from apsgo_scheduler.core._numeric_units import allocate_piece_milliseconds
    state = sample()
    task = state.task
    weight, duration = int(task.nodes.weight[0]), int(task.nodes.duration_ms[0])
    weights = (weight // 2, weight - weight // 2)
    durations = allocate_piece_milliseconds(weight, duration, weights, "split")
    group = NumericSplitGroup(0, 0, int(task.nodes.resource[0]), weight, duration,
                              int(task.nodes.source_period[0]), 0, 0, 0, 0, 1)
    pieces = tuple(split_piece_node(
        task, 0, group_index=0, piece_index=i, piece_count=2, weight=w,
        duration_ms=d, accepted_sequence=1,
    ) for i, (w, d) in enumerate(zip(weights, durations)))
    extension = extend_resource_workspace(task, state.program, state.quality, pieces, split_group=group)
    candidates = []
    for rows in ((extension.rows[0], 1, extension.rows[1], 2),
                 (extension.rows[1], extension.rows[0], 1, 2)):
        overlay = NumericPlanOverlay.build(extension.task, state.plan, (rows,), (10,), (0,))
        candidates.append((extension.task, extension.program, extension.quality, overlay))
    flat, _ = compare(state, candidates)
    assert flat.resource_row_offsets.tolist() == [0, 2, 4]
    assert flat.resource_metadata[:, -1].tolist() == [0] * 4
    assert state.split_sequence == 0


def test_batch_rejects_invalid_rows_and_does_not_create_detail_objects(monkeypatch):
    from apsgo_scheduler.core import _numeric_evaluation as evaluation
    from apsgo_scheduler.core._numeric_kernel import INVALID
    state = sample()
    overlay = NumericPlanOverlay.build(state.task, state.plan, ((0, 1, 2),), (10,), (0,))
    flat = pack(state, [(state.task, state.program, state.quality, overlay)])
    def fail(*args, **kwargs):
        pytest.fail("batch summary constructed a detail object")
    for cls in (evaluation.NumericPlanEvaluation, evaluation.NumericRuleResult,
                evaluation.NumericViolation, evaluation.NumericMetric):
        monkeypatch.setattr(cls, "__post_init__", fail)
    result = evaluate_numeric_batch(flat, state.task, state.program, state.quality, state.plan)
    assert result.status[0, 0] == 0
    invalid = replace(flat, node_rows=readonly((-1, 1, 2), np.int64))
    result = evaluate_numeric_batch(invalid, state.task, state.program, state.quality, state.plan)
    assert result.status[0, 0] == INVALID
    with pytest.raises(NumericValueError, match="invalid numeric"):
        evaluation._check_kernel_status(numeric_batch_result(invalid, result, 0))


def test_capacity_stale_and_cancellation_are_explicit():
    state = sample()
    overlay = NumericPlanOverlay.build(state.task, state.plan, ((0,), (1, 2)), (10, 20), (0, 0))
    candidates = [(state.task, state.program, state.quality, overlay)]
    with pytest.raises(NumericValueError, match="capacity"):
        pack(state, candidates, max_bytes=1)
    with pytest.raises(NumericValueError, match="capacity"):
        pack(state, candidates * 65)
    flat = pack(state, candidates)
    with pytest.raises(NumericValueError, match="stale"):
        evaluate_numeric_batch(flat, state.task, state.program, state.quality,
                               replace(state.plan, generation=state.plan.generation + 1))
    out = evaluate_numeric_batch(flat, state.task, state.program, state.quality, state.plan, cancelled=True)
    assert out.status[:, 0].tolist() == [CANCELLED]
    stale = replace(flat, candidate_generation=readonly([1], np.int64))
    out = evaluate_numeric_batch(stale, state.task, state.program, state.quality, state.plan)
    assert out.status[:, 0].tolist() == [STALE]


def test_batch_refuses_misaligned_sequence_and_unrelated_task():
    state = sample()
    overlay = NumericPlanOverlay.build(state.task, state.plan, ((0, 1, 2),), (10,), (0,))
    candidate = (state.task, state.program, state.quality, overlay)
    with pytest.raises(NumericValueError, match="ordered positive"):
        pack_numeric_candidates(state.task, state.program, state.quality, state.plan,
                                state.evaluation, [candidate, candidate], [2, 1], [0, 0])
    flat = pack(state, [candidate])
    with pytest.raises(NumericValueError, match="stale"):
        evaluate_numeric_batch(flat, replace(state.task, fingerprint="different"),
                               state.program, state.quality, state.plan)


def test_second_variant_still_attempts_quota_and_error_only_when_consumed(monkeypatch):
    state = sample()
    recipe = (refinement.NumericSearchAction.NODE_MOVE, 10, 20, 0, 1, 0, -1)

    def variants(*args):
        yield None
        raise NumericValueError("second_variant", "deferred failure")

    monkeypatch.setattr(refinement, "_prepare_recipe", variants)
    for size in (1, 8):
        runtime = budget(candidate_limit=1)
        # Size 1 has not prefetched StopIteration; size 8 has. Neither accepts.
        assert refinement._scan_family(state, runtime, iter([recipe]), _batch_size=size) == (False, size > 1)
        assert runtime.candidate_check_count == 1
        assert runtime.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
        with pytest.raises(NumericValueError, match="second_variant"):
            refinement._scan_family(state, budget(candidate_limit=2), iter([recipe]), _batch_size=size)


def test_first_accept_discards_later_errors_without_allocating_formal_ids(monkeypatch):
    state = sample()
    recipe = (refinement.NumericSearchAction.CHAIN_ORDER_RELOCATION, 10, 20, -1, -1, 1, -1)
    original = refinement._prepare_recipe

    def variants(*args):
        yield from original(*args)
        raise NumericValueError("unused", "must not propagate after first acceptance")

    monkeypatch.setattr(refinement, "_prepare_recipe", variants)
    monkeypatch.setattr(refinement, "_try_overlay_candidate", lambda *a, **kw: True)
    runtime = budget(candidate_limit=4)
    assert refinement._scan_family(state, runtime, iter([recipe]), _batch_size=8) == (True, False)
    assert runtime.candidate_check_count == 1
    assert state.virtual_sequence == 0


def test_cancellation_after_precompute_consumes_nothing(monkeypatch):
    state = sample()
    flag = type("Flag", (), {"cancelled": False, "is_cancelled": lambda self: self.cancelled})()
    runtime = budget(candidate_limit=10, cancellation=flag)
    diagnostics = refinement.NumericRefinementDiagnostics()
    recipe = (refinement.NumericSearchAction.CHAIN_ORDER_RELOCATION, 10, 20, -1, -1, 1, -1)
    original = refinement.evaluate_numeric_batch

    def evaluate(*args):
        output = original(*args)
        flag.cancelled = True
        return output

    monkeypatch.setattr(refinement, "evaluate_numeric_batch", evaluate)
    assert refinement._scan_family(state, runtime, iter([recipe]), diagnostics=diagnostics) == (False, False)
    assert runtime.stop_reason is SearchStopReason.USER_CANCELLED
    assert runtime.candidate_check_count == state.complete_candidate_evaluation_count == 0
    assert sum(diagnostics.numeric_precomputed.values()) == sum(diagnostics.numeric_discarded.values()) == 1


def test_capacity_reduces_batch_then_uses_same_single_kernel(monkeypatch):
    state = sample()
    recipe = (refinement.NumericSearchAction.CHAIN_ORDER_RELOCATION, 10, 20, -1, -1, 1, -1)
    calls = []
    original = refinement.pack_numeric_candidates

    def small_capacity(*args, **kwargs):
        calls.append(len(args[5]))
        return original(*args, **kwargs, max_bytes=1)

    monkeypatch.setattr(refinement, "pack_numeric_candidates", small_capacity)
    diagnostics = refinement.NumericRefinementDiagnostics()
    runtime = budget(candidate_limit=2)
    refinement._scan_family(state, runtime, iter([recipe, recipe]), diagnostics=diagnostics)
    assert calls == [2, 1, 1]
    assert runtime.candidate_check_count == 2
    assert state.complete_candidate_evaluation_count == 2
    assert diagnostics.numeric_batch_calls == 0
