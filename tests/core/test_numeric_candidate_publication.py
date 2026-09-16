"""Only ordered accepted attempts materialize identities and mutate search state."""

from tests.core import numeric_reference_resources as reference_resources
from tests.core import numeric_reference_search as reference_search
import pytest

from apsgo_scheduler.core import _numeric_search as search
from apsgo_scheduler.core import _numeric_resources as resources
from apsgo_scheduler.core import _numeric_candidate_kernel as candidate
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan
from apsgo_scheduler.core._numeric_rules import NumericRuleKind
from apsgo_scheduler.core._numeric_units import NumericValueError
from tests.core.test_numeric_candidate_kernel import (
    A, descriptor, workspace_for, standard_state, assert_resources,
)
from tests.core.test_numeric_construction import construction_case, budget
from tests.core.test_numeric_search import state_for
from tests.core.test_numeric_private_split import split_case


def compute(state, workspace, values, *, decision=None):
    return search.capture_candidate_result(workspace, state.program, state.quality, values, 0,
        candidate.CandidateCheckPolicy(), virtual_sequence=state.virtual_sequence,
        split_sequence=state.split_sequence, split_decision=decision, previous_evaluation=state.evaluation)


def compare_states(left, right):
    assert left.task.fingerprint == right.task.fingerprint
    assert left.task.ancestor_fingerprints == right.task.ancestor_fingerprints
    assert left.task.node_ids == right.task.node_ids
    assert left.program.fingerprint == right.program.fingerprint
    assert left.quality.fingerprint == right.quality.fingerprint
    assert left.plan.fingerprint == right.plan.fingerprint
    assert tuple(left.evaluation.quality_key) == tuple(right.evaluation.quality_key)
    assert left.accepted_moves == right.accepted_moves
    assert left.virtual_sequence == right.virtual_sequence
    assert left.split_sequence == right.split_sequence
    assert left.complete_candidate_evaluation_count == right.complete_candidate_evaluation_count


@pytest.mark.parametrize("bridge", (False, True))
def test_acceptance_matches_old_plan_fingerprints_trace_and_resource_events(bridge):
    temperatures = (("700", "710"), ("800", "810")) if bridge else None
    task, program, quality = construction_case(temperatures=temperatures)
    old = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    new = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    old_budget, new_budget = budget(candidate_limit=10), budget(candidate_limit=10)
    assert old_budget.consume_candidate_check() and new_budget.consume_candidate_check()
    edit = search.NumericCandidateEdit(task.fingerprint, old.plan.fingerprint, 0, 1,
        A.WHOLE_CHAIN_PREPEND, 10, 20, target_position=0)
    if bridge:
        extension, plan, sequence = reference_search._resource_whole_chain_candidate(old, edit, (0,), (1,), 2)
        accepted = reference_search._try_prepared_candidate(old, old_budget, edit, extension.task,
            extension.program, extension.quality, plan, extension.rows, virtual_sequence=sequence)
    else:
        accepted = reference_search._try_candidate(task, program, quality, old, old_budget, edit)
    assert accepted
    workspace = workspace_for(new)
    result = compute(new, workspace, descriptor(workspace, A.WHOLE_CHAIN_PREPEND))
    assert search.consume_candidate_result(new, new_budget, workspace, result)
    compare_states(new, old)
    assert_resources(workspace, new.task)


@pytest.mark.parametrize("period", ("P0", "P1"))
def test_split_publication_preserves_all_ancestor_event_identities(period):
    workspace, program, quality, decision = split_case("600", period)
    task, plan = workspace.task, workspace.plan
    previous = evaluate_numeric_plan(task, program, quality, plan)
    old = search.NumericSearchState(task, program, quality, plan, previous)
    new = search.NumericSearchState(task, program, quality, plan, previous)
    old_budget, new_budget = budget(candidate_limit=10), budget(candidate_limit=10)
    old_budget.consume_candidate_check()
    new_budget.consume_candidate_check()
    extension, expected, new_id, sequence, split_sequence, affected = reference_search._prepare_numeric_split(old, 0, 0, decision, 2)
    edit = search.NumericCandidateEdit(task.fingerprint, plan.fingerprint, 0, 1,
        A.CONTROLLED_ORDER_SPLIT, 10, new_id, node_row=0)
    assert reference_search._try_prepared_candidate(old, old_budget, edit, extension.task,
        extension.program, extension.quality, expected, affected, virtual_sequence=sequence,
        split_sequence=split_sequence, reject_prohibited_kinds=(NumericRuleKind.VIRTUAL_RATIO,))
    result = compute(new, workspace, descriptor(workspace, A.CONTROLLED_ORDER_SPLIT,
        target=new_id, node_row=0), decision=decision)
    assert search.consume_candidate_result(new, new_budget, workspace, result)
    compare_states(new, old)


def test_rejected_summary_does_not_materialize_any_formal_state(monkeypatch):
    state = standard_state()
    workspace = workspace_for(state)
    result = compute(state, workspace, descriptor(workspace, A.CHAIN_ORDER_RELOCATION, target_position=1))
    runtime = budget(candidate_limit=10)
    runtime.consume_candidate_check()

    def forbidden(*args, **kwargs):
        raise AssertionError("rejected attempt materialized")

    monkeypatch.setattr(search, "materialize_private_resources", forbidden)
    monkeypatch.setattr(search, "_build_plan", forbidden)
    monkeypatch.setattr(search, "materialize_numeric_evaluation", forbidden)
    before = state.plan
    assert not search.consume_candidate_result(state, runtime, workspace, result)
    assert state.plan is before and state.accepted_move_count == state.virtual_sequence == 0
    assert state.complete_candidate_evaluation_count == runtime.candidate_check_count == 1


@pytest.mark.parametrize("failure", ("detail", "resource", "events"))
def test_publication_failure_keeps_solution_and_extension_cache_unchanged(monkeypatch, failure):
    task, program, quality = construction_case(temperatures=(("700", "710"), ("800", "810")))
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    workspace = workspace_for(state)
    result = compute(state, workspace, descriptor(workspace, A.WHOLE_CHAIN_PREPEND))
    assert result.prepared and result.summary.quality.tolist() < state.evaluation.quality_key.tolist()
    if failure == "detail":
        def fail(*args, **kwargs):
            raise NumericValueError("test", "detail failure")
        monkeypatch.setattr(search, "materialize_numeric_evaluation", fail)
    elif failure == "resource":
        workspace.nodes.width[0] += 1
    else:
        workspace.event_node_ends[0] = 0
    previous = (state.task, state.program, state.quality, state.plan, state.evaluation)
    cache = reference_resources.extend_resource_workspace.cache_info()
    runtime = budget(candidate_limit=10)
    runtime.consume_candidate_check()
    with pytest.raises(NumericValueError):
        search.consume_candidate_result(state, runtime, workspace, result)
    assert previous == (state.task, state.program, state.quality, state.plan, state.evaluation)
    assert state.accepted_moves == () and state.virtual_sequence == state.split_sequence == 0
    assert reference_resources.extend_resource_workspace.cache_info() == cache


def test_first_acceptance_does_not_pull_suffix_or_fire_its_error():
    task, program, quality = construction_case()
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    workspace = workspace_for(state)
    result = compute(state, workspace, descriptor(workspace, A.WHOLE_CHAIN_PREPEND))
    runtime = budget(candidate_limit=10)
    runtime.consume_candidate_check()
    pulled = []

    def attempts():
        pulled.append(0)
        yield workspace, result
        pulled.append(1)
        raise AssertionError("accepted candidate must discard the suffix")

    assert search.consume_candidate_attempts(state, runtime, attempts())
    assert pulled == [0] and runtime.candidate_check_count == 1
    with pytest.raises(NumericValueError, match="stale"):
        search.consume_candidate_result(state, runtime, workspace, result)


@pytest.mark.parametrize("limit", (1, 2))
def test_second_variant_charges_only_at_original_order_and_defers_error(limit):
    state = standard_state()
    workspace = workspace_for(state)
    result = compute(state, workspace, descriptor(workspace, A.CHAIN_ORDER_RELOCATION, target_position=1))
    later = workspace_for(state)
    error = search.capture_candidate_result(later, state.program, state.quality,
        descriptor(later, A.CONTROLLED_ORDER_SPLIT, target=21, node_row=0), 0,
        candidate.CandidateCheckPolicy())
    assert isinstance(error, search.NumericDeferredCandidateFailure)
    runtime = budget(candidate_limit=limit)
    runtime.consume_candidate_check()
    attempts = ((workspace, result), (later, error))
    if limit == 1:
        assert not search.consume_candidate_attempts(state, runtime, attempts)
    else:
        with pytest.raises(NumericValueError, match="authorized"):
            search.consume_candidate_attempts(state, runtime, attempts)
    assert runtime.candidate_check_count == limit
    assert state.complete_candidate_evaluation_count == 1 and state.accepted_move_count == 0


def test_cancellation_after_computation_rejects_without_formal_publication():
    class Token:
        cancelled = False

        def is_cancelled(self):
            return self.cancelled

    token = Token()
    task, program, quality = construction_case()
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    workspace = workspace_for(state)
    result = compute(state, workspace, descriptor(workspace, A.WHOLE_CHAIN_PREPEND))
    runtime = budget(cancellation=token, candidate_limit=10)
    runtime.consume_candidate_check()
    token.cancelled = True
    assert not search.consume_candidate_result(state, runtime, workspace, result)
    assert state.task is task and state.plan is workspace.plan and state.accepted_move_count == 0


def test_first_search_order_keeps_its_original_empty_row_trace():
    task, program, quality = construction_case(due_dates=("2026-06-30", "2026-05-31"))
    old = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    new = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    old_budget, new_budget = budget(candidate_limit=10), budget(candidate_limit=10)
    old_budget.consume_candidate_check()
    new_budget.consume_candidate_check()
    edit = search.NumericCandidateEdit(task.fingerprint, old.plan.fingerprint, 0, 1,
        A.CHAIN_ORDER_RELOCATION, 20, 10, target_position=0)
    assert reference_search._try_candidate(task, program, quality, old, old_budget, edit)
    workspace = workspace_for(new)
    result = candidate.compute_candidate_attempt(workspace, program, quality,
        descriptor(workspace, A.CHAIN_ORDER_RELOCATION, source=20, target=10, target_position=0),
        0, candidate.CandidateCheckPolicy(same_period_order=True, record_order_rows=False))
    assert search.consume_candidate_result(new, new_budget, workspace, result)
    compare_states(new, old)
    assert new.accepted_moves[0].affected_rows == ()


def test_fill_publication_does_not_leak_prototype_index_into_legacy_move_trace():
    task, program, quality = construction_case(weights=("195",), widths=("1000",))
    state = state_for(task, program, quality, (0,), (0, 1), (10,), (0,))
    workspace = workspace_for(state)
    result = candidate.compute_candidate_attempt(workspace, program, quality,
        descriptor(workspace, A.VIRTUAL_WEIGHT_FILL, target=10, node_row=0, target_position=0),
        0, candidate.CandidateCheckPolicy(reject_prohibited_kinds=(NumericRuleKind.VIRTUAL_RATIO,)))
    runtime = budget(candidate_limit=10)
    runtime.consume_candidate_check()
    assert search.consume_candidate_result(state, runtime, workspace, result)
    assert state.virtual_sequence == 1 and state.task.node_ids[-1] == "virtual-000001"
    assert state.accepted_moves[0].action is A.VIRTUAL_WEIGHT_FILL
    assert state.accepted_moves[0].affected_rows == (task.nodes.weight.size,)


def test_private_failure_leaves_source_conservation_to_the_formal_publish_guard():
    task, program, quality = construction_case()
    state = state_for(task, program, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    workspace = workspace_for(state)
    result = compute(state, workspace, descriptor(workspace, A.WHOLE_CHAIN_PREPEND))
    workspace.changed_rows[workspace.starts[0]] = workspace.changed_rows[workspace.stops[0] - 1]
    runtime = budget(candidate_limit=10)
    runtime.consume_candidate_check()
    with pytest.raises(NumericValueError):
        search.consume_candidate_result(state, runtime, workspace, result)
    assert state.task is task and state.plan is workspace.plan and state.accepted_moves == ()
