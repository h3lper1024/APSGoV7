"""Whole-candidate native batches preserve individual attempts and private writes."""

from dataclasses import fields

import numpy as np
import pytest
from numba import get_num_threads, set_num_threads

from apsgo_scheduler.core import _numeric_candidate_kernel as k
from apsgo_scheduler.core import _numeric_batch as batches
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan
from apsgo_scheduler.core._numeric_state import NumericNodeColumns, OK, CANCELLED
from tests.core.test_numeric_candidate_kernel import descriptor, A, standard_state, workspace_for, bridge_state
from tests.core.test_numeric_private_resources import case


def compare_attempts(actual, expected):
    assert len(actual) == len(expected)
    for (workspace, result), (old_workspace, old) in zip(actual, expected):
        assert (result.status, result.prepared, result.admissible, result.virtual_sequence,
                result.split_sequence, result.cleaned_variant_exists) == (
                old.status, old.prepared, old.admissible, old.virtual_sequence,
                old.split_sequence, old.cleaned_variant_exists)
        np.testing.assert_array_equal(result.affected_rows, old.affected_rows)
        for name in ("chain_count", "changed_count", "node_count", "group_count", "event_count"):
            assert getattr(workspace, name) == getattr(old_workspace, name)
        for i in range(workspace.chain_count):
            np.testing.assert_array_equal(k.chain_rows(result.view, i), k.chain_rows(old.view, i))
        for name in ("ids", "periods"):
            np.testing.assert_array_equal(getattr(workspace, name)[:workspace.chain_count],
                getattr(old_workspace, name)[:old_workspace.chain_count])
        for f in fields(NumericNodeColumns):
            np.testing.assert_array_equal(getattr(workspace.nodes, f.name)[:workspace.node_count],
                getattr(old_workspace.nodes, f.name)[:old_workspace.node_count], err_msg=f.name)
        for name in workspace.derived._fields:
            np.testing.assert_array_equal(getattr(workspace.derived, name)[:, :workspace.node_count],
                getattr(old_workspace.derived, name)[:, :old_workspace.node_count], err_msg=name)
        for name in ("event_node_ends", "event_group_ends"):
            np.testing.assert_array_equal(getattr(workspace, name)[:workspace.event_count],
                getattr(old_workspace, name)[:old_workspace.event_count])
        if old.summary is None:
            assert result.summary is None
        else:
            for name in old.summary._fields:
                np.testing.assert_array_equal(getattr(result.summary, name), getattr(old.summary, name), err_msg=name)


@pytest.mark.parametrize("executor", ("serial", "parallel"))
def test_native_batch_complete_bridge_results_match_single_calls(executor):
    workspace, program, quality = case()
    task, plan = workspace.task, workspace.plan
    evaluation = evaluate_numeric_plan(task, program, quality, plan)
    entries = [(descriptor(workspace, action).values[0], k.CandidateCheckPolicy(), (0,))
               for action in (A.WHOLE_CHAIN_PREPEND, A.WHOLE_CHAIN_APPEND)]
    expected_pool = batches.NumericCandidateBatchWorkspace(task, program, quality, plan, evaluation)
    expected = [list(expected_pool.attempts(d, p, variants, virtual_sequence=0,
        split_sequence=0, allows_continue=lambda: True)) for d, p, variants in entries]
    pool = batches.NumericCandidateBatchWorkspace(task, program, quality, plan, evaluation,
                                                 _native_executor=executor)
    previous = get_num_threads()
    try:
        set_num_threads(min(2, previous))
        actual = pool.prepare_many(entries, virtual_sequence=0, split_sequence=0, allows_continue=lambda: True)
    finally:
        set_num_threads(previous)
    for a, b in zip(actual, expected):
        compare_attempts(a, b)
    assert not np.shares_memory(actual[0][0][0].nodes.width, actual[1][0][0].nodes.width)
    kernel = k.native_candidate_batch_parallel_step if executor == "parallel" else k.native_candidate_batch_serial_step
    assert kernel.nopython_signatures


@pytest.mark.parametrize("executor", ("serial", "parallel"))
def test_native_batch_skips_absent_cleaned_variant_and_keeps_original(executor):
    state = standard_state()
    workspace = workspace_for(state)
    d = descriptor(workspace, A.DELIVERY_INTRA_MOVE, target=10,
        target_position=0, source_start=1, source_stop=2).values[0]
    entries = [(d, k.CandidateCheckPolicy(), (1, 0))]
    pool = batches.NumericCandidateBatchWorkspace(state.task, state.program, state.quality,
        state.plan, state.evaluation, _native_executor=executor)
    actual = pool.prepare_many(entries, virtual_sequence=0, split_sequence=0, allows_continue=lambda: True)
    assert len(actual[0]) == 1 and actual[0][0][1].descriptor[k.VARIANT] == 0
    assert actual[0][0][1].status == OK and actual[0][0][1].prepared
    old = batches.NumericCandidateBatchWorkspace(state.task, state.program, state.quality, state.plan, state.evaluation)
    expected = list(old.attempts(d, k.CandidateCheckPolicy(), (1, 0), virtual_sequence=0,
        split_sequence=0, allows_continue=lambda: True))
    compare_attempts(actual[0], expected)


@pytest.mark.parametrize("executor", ("serial", "parallel"))
def test_native_batch_keeps_both_real_repair_variants_in_original_order(executor):
    state = bridge_state()
    workspace = workspace_for(state)
    d = descriptor(workspace, A.NODE_MOVE, source_start=2, source_stop=3, target_position=0).values[0]
    policy = k.CandidateCheckPolicy()
    old = batches.NumericCandidateBatchWorkspace(state.task, state.program, state.quality, state.plan, state.evaluation)
    expected = list(old.attempts(d, policy, (1, 0), virtual_sequence=2,
        split_sequence=0, allows_continue=lambda: True))
    pool = batches.NumericCandidateBatchWorkspace(state.task, state.program, state.quality,
        state.plan, state.evaluation, _native_executor=executor)
    actual = pool.prepare_many([(d, policy, (1, 0))], virtual_sequence=2,
        split_sequence=0, allows_continue=lambda: True)
    assert len(actual[0]) == len(expected) == 2
    compare_attempts(actual[0], expected)


@pytest.mark.parametrize("executor", ("serial", "parallel"))
def test_native_batch_capacity_retry_and_cancellation_never_publish(monkeypatch, executor):
    workspace, program, quality = case()
    task, plan = workspace.task, workspace.plan
    evaluation = evaluate_numeric_plan(task, program, quality, plan)
    values = descriptor(workspace, A.WHOLE_CHAIN_PREPEND).values[0]
    allocate = batches.allocate_candidate_workspace

    def tiny(task, plan):
        result = allocate(task, plan)
        return type(result).allocate(task, plan, changed_capacity=1, chain_capacity=plan.chain_ids.size,
            node_capacity=1, group_capacity=0, event_capacity=1)

    monkeypatch.setattr(batches, "allocate_candidate_workspace", tiny)
    pool = batches.NumericCandidateBatchWorkspace(task, program, quality, plan, evaluation,
        _native_executor=executor)
    actual = pool.prepare_many([(values, k.CandidateCheckPolicy(), (0,))],
        virtual_sequence=0, split_sequence=0, allows_continue=lambda: True)
    assert actual[0][0][1].prepared and actual[0][0][1].virtual_sequence == 2
    assert pool.workspaces[0].nodes.weight.size >= 2
    assert task is pool.context.task and plan is pool.context.plan
    pool.release()
    checks = []

    def cancel():
        checks.append(1)
        return len(checks) < 2

    stopped = pool.prepare_many([(values, k.CandidateCheckPolicy(), (0,))],
        virtual_sequence=0, split_sequence=0, allows_continue=cancel)
    assert stopped[0][0][1].status == CANCELLED and not stopped[0][0][1].prepared
    assert plan.generation == pool.context.plan.generation


@pytest.mark.parametrize("executor", ("serial", "parallel"))
def test_native_batch_later_invalid_descriptor_is_deferred(executor):
    state = standard_state()
    workspace = workspace_for(state)
    good = descriptor(workspace, A.WHOLE_CHAIN_PREPEND).values[0]
    bad = good.copy()
    bad[k.OWNER] = state.task.originals.weight.size
    pool = batches.NumericCandidateBatchWorkspace(state.task, state.program, state.quality,
        state.plan, state.evaluation, _native_executor=executor)
    actual = pool.prepare_many([(good, k.CandidateCheckPolicy(), (0,)),
                               (bad, k.CandidateCheckPolicy(), (0,))],
        virtual_sequence=0, split_sequence=0, allows_continue=lambda: True)
    assert actual[0][0][1].prepared
    assert isinstance(actual[1][0][1], k.NumericDeferredCandidateFailure)


@pytest.mark.parametrize("executor", ("serial", "parallel"))
def test_native_error_in_first_repair_suppresses_its_dependent_variant(executor):
    state = bridge_state()
    workspace = workspace_for(state)
    d = descriptor(workspace, A.NODE_MOVE, source_start=2, source_stop=3, target_position=0).values[0]
    pool = batches.NumericCandidateBatchWorkspace(state.task, state.program, state.quality,
        state.plan, state.evaluation, _native_executor=executor)
    actual = pool.prepare_many([(d, k.CandidateCheckPolicy(), (1, 0))],
        virtual_sequence=(1 << 63) - 1, split_sequence=0, allows_continue=lambda: True)
    assert len(actual[0]) == 1
    assert isinstance(actual[0][0][1], k.NumericDeferredCandidateFailure)
    assert "private_bridge.sequence" in str(actual[0][0][1].error)
