"""Common numeric transport, borrowed chain view and private buffer ownership."""

from dataclasses import fields

import numpy as np
from numba import njit
import pytest

from apsgo_scheduler.core import _numeric_kernel as kernel
from apsgo_scheduler.core._numeric_state import (
    CAPACITY, INVALID, OK, DESCRIPTOR_FIELDS, NumericCandidateDescriptors,
    NumericCandidateWorkspace, NumericNodeColumns, NumericSearchAction, NumericSplitGroups,
    NumericPlan, readonly,
)
from apsgo_scheduler.core._numeric_units import NumericValueError
from apsgo_scheduler.core._numeric_search import NumericSearchAction as SearchAction
from tests.core.test_numeric_construction import construction_case


def workspace():
    task, _, _ = construction_case()
    plan = NumericPlan.build(task, (0, 1), (0, 1, 2), (10, 20), (0, 0))
    return NumericCandidateWorkspace.allocate(task, plan, changed_capacity=4,
        chain_capacity=4, node_capacity=2, group_capacity=1, event_capacity=2)


@njit
def read_rows(view):
    # A real native consumer, not an object-mode or Python-only type promise.
    total = 0
    for chain in range(view.count):
        for position in range(view.starts[chain], view.stops[chain]):
            total += view.changed_rows[position] if view.private[chain] else view.base_rows[position]
    return total


def test_formal_plan_is_a_zero_overlay_native_view_without_copying_base_nodes():
    value = workspace()
    view = value.view()
    assert view.base_rows is value.plan.node_rows
    assert not view.base_rows.flags.writeable
    assert not view.private[:view.count].any()
    assert read_rows(view) == 1
    assert read_rows.nopython_signatures
    assert value.changed_count == value.node_count == value.group_count == value.event_count == 0
    assert value.allocated_bytes > 0


def test_private_fields_follow_authoritative_schema_and_are_disjoint():
    first, second = workspace(), workspace()
    assert first.nodes._fields == tuple(f.name for f in fields(NumericNodeColumns))
    assert first.split_groups._fields == tuple(f.name for f in fields(NumericSplitGroups))
    for name in first.nodes._fields:
        tail, base = getattr(first.nodes, name), getattr(first.task.nodes, name)
        assert tail.dtype == base.dtype
        assert tail.shape[1:] == base.shape[1:]
        assert not np.shares_memory(tail, base)
        assert not np.shares_memory(tail, getattr(second.nodes, name))
        tail[:] = 0
    for name in first.derived._fields:
        tail, base = getattr(first.derived, name), getattr(first.task, name)
        assert tail.shape == (base.shape[0], 2)
        assert tail.dtype == base.dtype
    assert first.task.nodes.weight[0] > 0


def test_capacity_failure_is_nonmutating_and_retry_invalidates_old_views():
    value = workspace()
    previous = value.view()
    value.changed_rows[:2] = (1, 0)
    value.changed_count = 2
    epoch = value.epoch
    assert value.capacity_status(changed_rows=5, chains=2, nodes=1, groups=1, events=1) == CAPACITY
    assert value.capacity_status(changed_rows=-1, chains=2, nodes=1, groups=1, events=1) == INVALID
    assert value.changed_count == 2 and value.epoch == epoch
    value.grow_for_retry(changed_capacity=8, chain_capacity=4, node_capacity=4,
                         group_capacity=2, event_capacity=4)
    assert value.capacity_status(changed_rows=8, chains=4, nodes=4, groups=2, events=4) == OK
    assert value.changed_count == value.node_count == value.group_count == value.event_count == 0
    assert value.view().base_rows is previous.base_rows
    with pytest.raises(NumericValueError, match="expired"):
        value.require_view(previous)
    assert read_rows(value.view()) == 1


def test_reset_invalidates_borrowed_view_and_other_attempt_is_rejected():
    value = workspace()
    view = value.view()
    value.reset()
    with pytest.raises(NumericValueError, match="expired"):
        value.require_view(view)
    with pytest.raises(NumericValueError, match="foreign"):
        value.require_view(workspace().view())
    value.require_view(value.view())
    with pytest.raises(NumericValueError, match="stale"):
        value.require_current(value.task, workspace().plan)


def descriptor(value, **changes):
    record = {name: -1 for name in DESCRIPTOR_FIELDS}
    record.update(action=0, source_chain_id=10, target_chain_id=20,
                  source_reversed=0, target_reversed=0, repair_variant=0)
    record.update(changes)
    return NumericCandidateDescriptors(value.task, value.plan,
        readonly([[record[name] for name in DESCRIPTOR_FIELDS]], np.int64))


def test_descriptor_binds_exact_base_and_reuses_action_and_status_authority():
    value = workspace()
    descriptions = descriptor(value)
    descriptions.require_current(value.task, value.plan)
    with pytest.raises(NumericValueError, match="stale"):
        descriptions.require_current(value.task, workspace().plan)
    assert SearchAction is NumericSearchAction
    assert kernel.OK == OK and kernel.CAPACITY == CAPACITY
    with pytest.raises(NumericValueError, match="read-only"):
        NumericCandidateDescriptors(value.task, value.plan, descriptions.values.copy())


@pytest.mark.parametrize("changes", (
    {"action": -1}, {"action": len(NumericSearchAction)}, {"source_reversed": 2},
    {"owner_source": 2}, {"owner_source": -2}, {"repair_variant": -1},
    {"source_chain_id": -1},
))
def test_descriptor_rejects_invalid_numeric_domains(changes):
    with pytest.raises(NumericValueError):
        descriptor(workspace(), **changes)


def test_no_dynamic_node_or_formal_task_extension_needed_for_workspace(monkeypatch):
    from apsgo_scheduler.core import _numeric_state as state

    def forbidden(*a, **kw):
        raise AssertionError("formal dynamic materialization forbidden")

    monkeypatch.setattr(state, "extend_numeric_task", forbidden)
    monkeypatch.setattr(state.NumericDynamicNode, "__post_init__", forbidden)
    value = workspace()
    assert read_rows(value.view()) == 1
