"""All refinement actions consume the shared private candidate computation."""
from dataclasses import replace

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_refinement as refinement
from apsgo_scheduler.core import _numeric_refinement_scan as scan
from apsgo_scheduler.core import _numeric_search as search
from apsgo_scheduler.core import _numeric_batch as batch
from apsgo_scheduler.core._numeric_state import NumericCandidateWorkspace, readonly
from tests.core.test_numeric_construction import budget
from tests.core.test_numeric_refinement_scan import make_state, recipe
from tests.core.test_numeric_candidate_publication import compare_states


@pytest.mark.parametrize("action,args", (
    (scan.INTRA, (10, 10, 2, 3, 0, -1)),
    (scan.NODE_MOVE, (10, 20, 0, 1, 0, -1)),
    (scan.NODE_SWAP, (10, 20, 0, 1, 0, 1)),
    (scan.BLOCK_MOVE, (10, 20, 0, 2, 0, -1)),
    (scan.BLOCK_SWAP, (10, 20, 0, 2, 0, 2)),
    (scan.CUT, (10, 31, 2, -1, 0, 1)),
    (scan.ORDER, (10, 20, -1, -1, 1, -1)),
))
@pytest.mark.parametrize("batch_size", (1, 8))
def test_refinement_attempts_match_original_charge_state_and_identity(action, args, batch_size):
    old, new = make_state(), make_state()
    value = scan.description(action, *args)
    before, after = budget(candidate_limit=10), budget(candidate_limit=10)
    before.consume_candidate_check()
    expected = refinement._try_recipe(old, before, recipe(value), 2)
    accepted, _ = refinement._scan_family(new, after, iter((value,)), _batch_size=batch_size)
    assert accepted == expected
    assert after.candidate_check_count == before.candidate_check_count
    compare_states(new, old)


def test_production_refinement_rejects_all_old_preparation_paths(monkeypatch):
    state = make_state()
    def forbidden(*args, **kwargs):
        raise AssertionError("old refinement preparation reached")
    for name in ("_prepare_recipe", "_prepare_segment_recipe", "_prepare_repaired_parts",
                 "_prepare_recipe_batch", "_prepare_chain_cut", "_prepare_order",
                 "_prepare_reclaim", "_candidate_overlay", "_try_overlay_candidate"):
        monkeypatch.setattr(refinement, name, forbidden)
    monkeypatch.setattr(refinement.NumericRefinementIndex, "build", forbidden)
    runtime = budget(candidate_limit=100)
    refinement.improve_numeric_refinement(state, runtime)
    assert runtime.candidate_check_count > 0


def test_capacity_retry_uses_same_description_and_logical_quota(monkeypatch):
    value = scan.description(scan.NODE_MOVE, 10, 20, 0, 1, 0, -1)
    normal, tiny = make_state(), make_state()
    first, second = budget(candidate_limit=10), budget(candidate_limit=10)
    refinement._scan_family(normal, first, iter((value,)))
    def allocate(task, plan):
        return NumericCandidateWorkspace.allocate(task, plan,
            changed_capacity=0, chain_capacity=plan.chain_ids.size + 1,
            node_capacity=0, group_capacity=0, event_capacity=0)
    monkeypatch.setattr(batch, "allocate_candidate_workspace", allocate)
    refinement._scan_family(tiny, second, iter((value,)))
    compare_states(tiny, normal)
    assert first.candidate_check_count == second.candidate_check_count


def test_rejected_private_candidate_does_not_construct_formal_objects(monkeypatch):
    state = make_state()
    value = scan.description(scan.ORDER, 10, 20, -1, -1, 1, -1)
    def forbidden(*args, **kwargs):
        raise AssertionError("rejected refinement materialized formal state")
    for name in ("materialize_private_resources", "_build_plan", "materialize_numeric_evaluation"):
        monkeypatch.setattr(search, name, forbidden)
    # Force a non-improving current key without changing candidate computation.
    state.evaluation = replace(state.evaluation, quality_key=readonly(np.full(9, -1, np.int64), np.int64))
    old_plan = state.plan
    accepted, _ = refinement._scan_family(state, budget(candidate_limit=10), iter((value,)))
    assert not accepted and state.plan is old_plan


def test_public_refinement_selects_native_serial_with_identical_consumption(monkeypatch):
    old, new = make_state(), make_state()
    first, second = budget(candidate_limit=100), budget(candidate_limit=100)
    original_pool = refinement.NumericCandidateBatchWorkspace
    original_many = original_pool.prepare_many
    calls, choices = [], []

    def single(*args, **kwargs):
        kwargs["_native_executor"] = None
        return original_pool(*args, **kwargs)

    with monkeypatch.context() as isolated:
        isolated.setattr(refinement, "NumericCandidateBatchWorkspace", single)
        refinement.improve_numeric_refinement(old, first)

    def selected(*args, **kwargs):
        choices.append(kwargs.get("_native_executor"))
        return original_pool(*args, **kwargs)

    def counted(current, *args, **kwargs):
        calls.append(current.native_executor)
        return original_many(current, *args, **kwargs)

    monkeypatch.setattr(refinement, "NumericCandidateBatchWorkspace", selected)
    monkeypatch.setattr(original_pool, "prepare_many", counted)
    refinement.improve_numeric_refinement(new, second)
    assert choices and set(choices) == {"serial"}
    assert calls and set(calls) == {"serial"}
    assert first.candidate_check_count == second.candidate_check_count
    assert old.complete_candidate_evaluation_count == new.complete_candidate_evaluation_count
    compare_states(new, old)
