"""Common attempts preserve stage preparation, private resources and evaluation."""

from dataclasses import fields

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_candidate_kernel as candidate
from apsgo_scheduler.core import _numeric_search as search
from apsgo_scheduler.core import _numeric_refinement as refinement
from apsgo_scheduler.core import _numeric_resources as resources
from apsgo_scheduler.core._numeric_chain_ops import chain_rows
from apsgo_scheduler.core._numeric_evaluation import evaluate_numeric_plan, evaluate_numeric_overlay_candidate
from apsgo_scheduler.core._numeric_rules import NumericRuleKind
from apsgo_scheduler.core._numeric_state import (
    NumericCandidateWorkspace, NumericCandidateDescriptors, NumericPlan,
    NumericNodeColumns, NumericSplitGroups, DESCRIPTOR_FIELDS, readonly,
    OK, INVALID, CAPACITY, CANCELLED,
)
from apsgo_scheduler.core.model import VirtualPurpose
from apsgo_scheduler.core._numeric_units import NumericValueError
from tests.core.test_numeric_construction import construction_case
from tests.core.test_numeric_search import state_for
from tests.core.test_numeric_private_resources import case
from tests.core.test_numeric_private_split import split_case
from tests.core.test_numeric_view_evaluation import assert_same

A = search.NumericSearchAction


def workspace_for(state, *, capacity=128):
    return NumericCandidateWorkspace.allocate(state.task, state.plan, changed_capacity=capacity,
        chain_capacity=state.plan.chain_ids.size + 2, node_capacity=32, group_capacity=2, event_capacity=32)


def descriptor(workspace, action, source=10, target=20, **values):
    row = np.full(len(DESCRIPTOR_FIELDS), -1, dtype=np.int64)
    defaults = dict(action=tuple(A).index(action), source_chain_id=source, target_chain_id=target,
                    source_reversed=0, target_reversed=0, repair_variant=0)
    if action in (A.WHOLE_CHAIN_APPEND, A.WHOLE_CHAIN_PREPEND, A.WHOLE_CHAIN_INSERTION):
        defaults["target_position"] = 0
    defaults.update(values)
    values = defaults
    for key, value in values.items():
        row[DESCRIPTOR_FIELDS.index(key)] = value
    return NumericCandidateDescriptors(workspace.task, workspace.plan, readonly(row[None, :], np.int64))


def assert_layout(workspace, plan):
    count = workspace.chain_count
    np.testing.assert_array_equal(workspace.ids[:count], plan.chain_ids)
    np.testing.assert_array_equal(workspace.periods[:count], plan.chain_periods)
    expected = plan.chains if hasattr(plan, "chains") else tuple(
        plan.node_rows[plan.chain_offsets[i]:plan.chain_offsets[i + 1]] for i in range(plan.chain_ids.size))
    for index, rows in enumerate(expected):
        np.testing.assert_array_equal(chain_rows(workspace.view(), index), rows)


def assert_resources(workspace, task):
    start = workspace.task.nodes.weight.size
    assert task.nodes.weight.size == start + workspace.node_count
    for f in fields(NumericNodeColumns):
        np.testing.assert_array_equal(getattr(workspace.nodes, f.name)[:workspace.node_count],
            getattr(task.nodes, f.name)[start:], err_msg=f.name)
    for name in workspace.derived._fields:
        np.testing.assert_array_equal(getattr(workspace.derived, name)[:, :workspace.node_count],
            getattr(task, name)[:, start:], err_msg=name)
    base_groups = workspace.task.split_groups.parent_row.size
    for f in fields(NumericSplitGroups):
        np.testing.assert_array_equal(getattr(workspace.split_groups, f.name)[:workspace.group_count],
            getattr(task.split_groups, f.name)[base_groups:], err_msg=f.name)


def standard_state():
    task, program, quality = construction_case(weights=("100",) * 6, widths=("1000",) * 6)
    return state_for(task, program, quality, range(6), (0, 3, 6), (10, 20), (0, 0))


@pytest.mark.parametrize("action,position", ((A.WHOLE_CHAIN_APPEND, 3),
    (A.WHOLE_CHAIN_PREPEND, 0), (A.WHOLE_CHAIN_INSERTION, 1)))
@pytest.mark.parametrize("reverse", (False, True))
def test_whole_chain_private_attempt_matches_existing_candidate(action, position, reverse):
    state = standard_state()
    workspace = workspace_for(state)
    values = descriptor(workspace, action, target_position=position)
    raw = values.values.copy()
    raw[0, candidate.REVERSE_SOURCE] = raw[0, candidate.REVERSE_TARGET] = reverse
    values = NumericCandidateDescriptors(state.task, state.plan, readonly(raw, np.int64))
    edit = search.NumericCandidateEdit(state.task.fingerprint, state.plan.fingerprint, 0, 1,
        action, 10, 20, target_position=position, source_reversed=reverse, target_reversed=reverse)
    expected = search.apply_numeric_candidate(state.task, state.plan, edit)
    result = candidate.compute_candidate_attempt(workspace, state.program, state.quality,
        values, 0, candidate.CandidateCheckPolicy(), previous_evaluation=state.evaluation)
    assert result.status == OK and result.prepared and result.admissible
    assert_layout(workspace, expected)
    assert_same(result.summary, evaluate_numeric_plan(state.task, state.program, state.quality, expected).kernel_result)
    assert workspace.node_count == workspace.event_count == 0


def test_real_node_direct_only_and_order_match_existing_candidates():
    state = standard_state()
    for action, kwargs in ((A.REAL_NODE_RELOCATION, dict(node_row=1, target_position=1)),
                           (A.CHAIN_ORDER_RELOCATION, dict(target_position=1))):
        workspace = workspace_for(state)
        edit = search.NumericCandidateEdit(state.task.fingerprint, state.plan.fingerprint, 0, 1,
                                            action, 10, 20, **kwargs)
        expected = search.apply_numeric_candidate(state.task, state.plan, edit)
        result = candidate.compute_candidate_attempt(workspace, state.program, state.quality,
            descriptor(workspace, action, **kwargs), 0, candidate.CandidateCheckPolicy(same_period_order=True))
        assert result.status == OK and result.prepared
        assert_layout(workspace, expected)
        assert_same(result.summary, evaluate_numeric_plan(state.task, state.program, state.quality, expected).kernel_result)


@pytest.mark.parametrize("action,start,stop,other_start,other_stop", (
    (A.DELIVERY_INTRA_MOVE, 1, 2, 0, 0), (A.NODE_MOVE, 1, 2, 1, 1),
    (A.BLOCK_MOVE, 1, 3, 1, 1), (A.NODE_EXCHANGE, 1, 2, 1, 2),
    (A.BLOCK_EXCHANGE, 1, 3, 0, 2), (A.CHAIN_CUT, 1, -1, 0, 1)))
def test_refinement_preparation_uses_shared_array_operations(action, start, stop, other_start, other_stop):
    state = standard_state()
    target = 10 if action is A.DELIVERY_INTRA_MOVE else 21 if action is A.CHAIN_CUT else 20
    recipe = (action, 10, target, start, stop, other_start, other_stop)
    expected = list(refinement._prepare_recipe(state, recipe, 2))[-1]
    assert expected is not None
    workspace = workspace_for(state)
    values = descriptor(workspace, action, target=target, source_start=start, source_stop=stop,
        target_start=other_start, target_stop=other_stop, target_position=other_start)
    result = candidate.compute_candidate_attempt(workspace, state.program, state.quality,
        values, 0, candidate.CandidateCheckPolicy())
    assert result.status == OK and result.prepared
    assert_layout(workspace, expected.overlay)
    evaluation = evaluate_numeric_overlay_candidate(expected.task, expected.program, expected.quality,
        expected.overlay, state.task, state.program, state.quality, state.plan, state.evaluation)
    assert_same(result.summary, evaluation.kernel_result)
    np.testing.assert_array_equal(result.affected_rows, expected.affected_rows)


def test_private_double_bridge_keeps_resource_fields_and_order():
    workspace, program, quality = case()
    state = search.NumericSearchState(workspace.task, program, quality, workspace.plan,
        evaluate_numeric_plan(workspace.task, program, quality, workspace.plan))
    edit = search.NumericCandidateEdit(state.task.fingerprint, state.plan.fingerprint, 0, 1,
        A.WHOLE_CHAIN_PREPEND, 10, 20, target_position=0)
    expected, plan, sequence = search._resource_whole_chain_candidate(state, edit, (0,), (1,), 2)
    result = candidate.compute_candidate_attempt(workspace, program, quality,
        descriptor(workspace, A.WHOLE_CHAIN_PREPEND), 0, candidate.CandidateCheckPolicy())
    assert result.status == OK and result.prepared and result.virtual_sequence == sequence == 2
    assert_layout(workspace, plan)
    assert_resources(workspace, expected.task)
    assert workspace.event_node_ends[:workspace.event_count].tolist() == [2]
    assert_same(result.summary, evaluate_numeric_plan(expected.task, expected.program, expected.quality, plan).kernel_result)


@pytest.mark.parametrize("period", ("P0", "P1"))
def test_split_is_one_private_candidate_with_original_group_and_separator_events(period):
    workspace, program, quality, decision = split_case("1200", period)
    state = search.NumericSearchState(workspace.task, program, quality, workspace.plan,
        evaluate_numeric_plan(workspace.task, program, quality, workspace.plan))
    expected, plan, _, sequence, split_sequence, affected = search._prepare_numeric_split(state, 0, 0, decision, 2)
    result = candidate.compute_candidate_attempt(workspace, program, quality,
        descriptor(workspace, A.CONTROLLED_ORDER_SPLIT, target=11, node_row=0), 0,
        candidate.CandidateCheckPolicy(), split_decision=decision)
    assert result.status == OK and result.prepared
    assert (result.virtual_sequence, result.split_sequence) == (sequence, split_sequence)
    assert_layout(workspace, plan)
    assert_resources(workspace, expected.task)
    np.testing.assert_array_equal(result.affected_rows, affected)
    assert_same(result.summary, evaluate_numeric_plan(expected.task, expected.program, expected.quality, plan).kernel_result)


def test_no_solution_cancel_capacity_and_policy_do_not_mutate_the_base():
    workspace, program, quality = case(capacity=1)
    before = (workspace.task.fingerprint, workspace.plan.fingerprint, workspace.plan.node_rows.copy())
    values = descriptor(workspace, A.WHOLE_CHAIN_PREPEND)
    result = candidate.compute_candidate_attempt(workspace, program, quality, values, 0, candidate.CandidateCheckPolicy())
    assert result.status == CAPACITY and not result.prepared
    result = candidate.compute_candidate_attempt(workspace, program, quality, values, 0,
        candidate.CandidateCheckPolicy(), allows_continue=lambda: False)
    assert result.status == CANCELLED and workspace.node_count == workspace.changed_count == 0
    result = candidate.compute_candidate_attempt(workspace, program, quality, values, 0,
        candidate.CandidateCheckPolicy(maximum_bridge_nodes=0))
    assert result.status == OK and not result.prepared
    assert before[:2] == (workspace.task.fingerprint, workspace.plan.fingerprint)
    np.testing.assert_array_equal(workspace.plan.node_rows, before[2])


def test_rejected_candidate_does_not_call_stage_or_formal_preparation(monkeypatch):
    state = standard_state()
    workspace = workspace_for(state)
    values = descriptor(workspace, A.WHOLE_CHAIN_PREPEND)

    def forbidden(*args, **kwargs):
        raise AssertionError("formal candidate or stage preparation called")

    monkeypatch.setattr(NumericPlan, "build", forbidden)
    monkeypatch.setattr(search, "_layout", forbidden)
    monkeypatch.setattr(search, "_resource_whole_chain_candidate", forbidden)
    monkeypatch.setattr(refinement, "_prepare_recipe", forbidden)
    monkeypatch.setattr(resources, "extend_resource_workspace", forbidden)
    result = candidate.compute_candidate_attempt(workspace, state.program, state.quality,
        values, 0, candidate.CandidateCheckPolicy(maximum_changed_chain_weight=1))
    assert result.status == OK and not result.prepared


@pytest.mark.parametrize("position", (0, 1, 2))
def test_explicit_weight_fill_keeps_selected_prototype_and_original_edges(position):
    task, program, quality = construction_case(widths=("1000", "1000"))
    state = state_for(task, program, quality, (0, 1), (0, 2), (10,), (0,))
    workspace = workspace_for(state)
    node = resources.virtual_node(task, 0, max(0, position - 1), min(1, position),
                                   purpose=VirtualPurpose.WEIGHT_FILL, sequence=1)
    expected = resources.extend_resource_workspace(task, program, quality, (node,))
    row = expected.rows[0]
    order = (0, 1)
    order = order[:position] + (row,) + order[position:]
    plan = NumericPlan.build(expected.task, order, (0, len(order)), (10,), (0,))
    result = candidate.compute_candidate_attempt(workspace, program, quality,
        descriptor(workspace, A.VIRTUAL_WEIGHT_FILL, target=10, node_row=0, target_position=position),
        0, candidate.CandidateCheckPolicy())
    assert result.prepared and result.virtual_sequence == 1
    assert_layout(workspace, plan)
    assert_resources(workspace, expected.task)
    assert_same(result.summary, evaluate_numeric_plan(expected.task, expected.program, expected.quality, plan).kernel_result)


def bridge_state():
    task, program, quality = construction_case(weights=("100",) * 4, widths=("1000",) * 4)
    nodes = tuple(resources.virtual_node(task, 0, 0, 1, purpose=VirtualPurpose.EDGE_BRIDGE,
                                         sequence=i + 1) for i in range(2))
    extension = resources.extend_resource_workspace(task, program, quality, nodes)
    a, b = extension.rows
    state = state_for(extension.task, extension.program, extension.quality,
        (0, a, 1, b, 2, 3), (0, 5, 6), (10, 20), (0, 0))
    state.virtual_sequence = 2
    return state


@pytest.mark.parametrize("variant", (0, 1))
def test_cleaned_and_original_repairs_keep_removed_order_and_only_requested_variant(variant):
    state = bridge_state()
    recipe = (A.NODE_MOVE, 10, 20, 2, 3, 0, 0)
    old = list(refinement._prepare_recipe(state, recipe, 2))
    assert len(old) == 2 and all(item is not None for item in old)
    expected = old[0 if variant else 1]
    workspace = workspace_for(state)
    result = candidate.compute_candidate_attempt(workspace, state.program, state.quality,
        descriptor(workspace, A.NODE_MOVE, source_start=2, source_stop=3,
            target_position=0, repair_variant=variant), 0, candidate.CandidateCheckPolicy(), virtual_sequence=2)
    assert result.prepared and result.cleaned_variant_exists
    assert_layout(workspace, expected.overlay)
    np.testing.assert_array_equal(result.affected_rows, expected.affected_rows)
    assert_resources(workspace, expected.task)


def test_bridge_reclaim_keeps_protected_resources_and_direct_only_semantics():
    state = bridge_state()
    recipe = (A.BRIDGE_RECLAMATION, 10, 10, 1, 2, -1, -1)
    expected = refinement._prepare_reclaim(state, recipe)
    workspace = workspace_for(state)
    result = candidate.compute_candidate_attempt(workspace, state.program, state.quality,
        descriptor(workspace, A.BRIDGE_RECLAMATION, target=10, source_start=1, source_stop=2),
        0, candidate.CandidateCheckPolicy(), virtual_sequence=2)
    assert result.prepared and result.virtual_sequence == 2
    assert_layout(workspace, expected.overlay)
    np.testing.assert_array_equal(result.affected_rows, expected.affected_rows)
    assert workspace.node_count == 0


def test_phase_prohibition_policy_does_not_turn_every_candidate_into_zero_violation_gate():
    # Six identical high-surface nodes in one chain violate the enabled run rule.
    from tests.core.test_numeric_rules import build, attributes
    from tests.core.test_numeric_evaluation import numeric_quality_spec
    from tests.app.test_input_normalizer import make_order
    from apsgo_scheduler.core._numeric_rules import NumericRuleProgram
    from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
    rules, task = build(spec=numeric_quality_spec(), orders=tuple(
        make_order(i, source_period="P0", rule_attributes=attributes(surface_grade="FC")) for i in range(6)))
    program = NumericRuleProgram.compile(task, rules)
    quality = NumericQualityProgram.compile(task, program, rules)
    state = state_for(task, program, quality, range(6), (0, 3, 6), (10, 20), (0, 0))
    workspace = workspace_for(state)
    values = descriptor(workspace, A.WHOLE_CHAIN_PREPEND)
    first = candidate.compute_candidate_attempt(workspace, program, quality, values, 0,
        candidate.CandidateCheckPolicy())
    assert first.prepared and first.admissible and first.summary.quality[0] > 0
    strict = candidate.compute_candidate_attempt(workspace, program, quality, values, 0,
        candidate.CandidateCheckPolicy(reject_prohibited_kinds=tuple(NumericRuleKind)))
    assert strict.prepared and not strict.admissible
    assert_same(first.summary, strict.summary)


def test_capacity_retry_and_cancelled_later_splice_keep_the_same_candidate():
    workspace, program, quality = case(capacity=1)
    values = descriptor(workspace, A.WHOLE_CHAIN_PREPEND)
    policy = candidate.CandidateCheckPolicy()
    first = candidate.compute_candidate_attempt(workspace, program, quality, values, 0, policy)
    assert first.status == CAPACITY
    workspace.grow_for_retry(changed_capacity=20, chain_capacity=4, node_capacity=4,
                              group_capacity=1, event_capacity=4)
    retry = candidate.compute_candidate_attempt(workspace, program, quality, values, 0, policy)
    assert retry.prepared and retry.virtual_sequence == 2
    assert retry.affected_rows.size == 2
    assert workspace.event_count == 1
    # A result is borrowed until the next attempt, not a reusable detached plan.
    old_view = retry.view
    stopped = candidate.compute_candidate_attempt(workspace, program, quality, values, 0, policy,
                                                    allows_continue=lambda: False)
    assert stopped.status == CANCELLED
    with pytest.raises(NumericValueError, match="expired"):
        workspace.require_view(old_view)


@pytest.mark.parametrize("chunk", (1, 2, 64))
def test_native_splice_scan_resume_keeps_selection_fields_and_resource_events(monkeypatch, chunk):
    workspace, program, quality = case()
    values = descriptor(workspace, A.WHOLE_CHAIN_PREPEND)
    expected = resources.choose_virtual_bridge(workspace.task, program, quality, 0, 1,
        max_nodes=2, first_sequence=1)
    native = candidate.repair_parts_step
    calls = []

    def bounded(*args):
        calls.append(1)
        return native(*args[:-1], chunk)

    def forbidden(*args, **kwargs):
        raise AssertionError("splice must not return to Python bridge orchestration")

    monkeypatch.setattr(candidate, "repair_parts_step", bounded)
    monkeypatch.setattr(resources, "prepare_private_bridge", forbidden)
    result = candidate.compute_candidate_attempt(workspace, program, quality, values, 0,
        candidate.CandidateCheckPolicy())
    assert result.prepared and result.virtual_sequence == 2
    assert_resources(workspace, expected.task)
    assert workspace.event_count == 1 and workspace.event_node_ends[0] == 2
    assert workspace.event_group_ends[0] == 0
    assert len(calls) >= 4 and native.nopython_signatures
    assert resources.private_bridge_step.nopython_signatures


def test_native_splice_sequence_overflow_retains_original_boundary_error():
    workspace, program, quality = case()
    values = descriptor(workspace, A.WHOLE_CHAIN_PREPEND)
    with pytest.raises(NumericValueError, match="private_bridge.sequence"):
        candidate.compute_candidate_attempt(workspace, program, quality, values, 0,
            candidate.CandidateCheckPolicy(), virtual_sequence=(1 << 63) - 2)
    assert workspace.node_count == workspace.event_count == 0


def test_descriptor_staleness_and_action_specific_domains_are_checked_before_editing():
    state = standard_state()
    workspace = workspace_for(state)
    invalid = descriptor(workspace, A.REAL_NODE_RELOCATION, node_row=1, target_position=1,
                           source_reversed=1)
    result = candidate.compute_candidate_attempt(workspace, state.program, state.quality,
        invalid, 0, candidate.CandidateCheckPolicy())
    assert result.status == INVALID and workspace.changed_count == 0
    other_plan = NumericPlan.build(state.task, range(6), (0, 3, 6), (10, 20), (0, 0))
    stale = NumericCandidateDescriptors(state.task, other_plan, invalid.values)
    with pytest.raises(NumericValueError, match="stale"):
        candidate.compute_candidate_attempt(workspace, state.program, state.quality,
            stale, 0, candidate.CandidateCheckPolicy())


@pytest.mark.parametrize("source,target", ((10, 20), (30, 10), (20, 30)))
def test_merge_keeps_original_retained_order_before_stable_period_grouping(source, target):
    task, program, quality = construction_case(weights=("100",) * 3, widths=("1000",) * 3,
                                               source_periods=("P0", "P1", "P1"))
    state = state_for(task, program, quality, range(3), (0, 1, 2, 3), (10, 20, 30), (0, 1, 1))
    workspace = workspace_for(state)
    edit = search.NumericCandidateEdit(task.fingerprint, state.plan.fingerprint, 0, 1,
        A.WHOLE_CHAIN_PREPEND, source, target, target_position=0)
    expected = search.apply_numeric_candidate(task, state.plan, edit)
    result = candidate.compute_candidate_attempt(workspace, program, quality,
        descriptor(workspace, A.WHOLE_CHAIN_PREPEND, source=source, target=target), 0,
        candidate.CandidateCheckPolicy())
    assert result.prepared
    assert_layout(workspace, expected)
