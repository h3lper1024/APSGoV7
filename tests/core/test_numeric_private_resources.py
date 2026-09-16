"""Private numeric bridges preserve the original adapter's choices and fields."""

from tests.core import numeric_reference_resources as reference_resources
from dataclasses import replace, fields
from decimal import Decimal

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_resources as resources
from apsgo_scheduler.core._numeric_state import (
    OK, CAPACITY, CANCELLED, INVALID, MORE_WORK,
    NumericPlan, NumericCandidateWorkspace, NumericNodeColumns,
)
from apsgo_scheduler.core._numeric_rules import NumericRuleProgram, NumericRuleKind
from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
from apsgo_scheduler.core._numeric_kernel import task_columns, rule_tables
from apsgo_scheduler.core._numeric_units import NumericValueError
from tests.core.test_numeric_evaluation import numeric_quality_spec
from tests.core.test_numeric_rules import build, attributes
from tests.app.test_input_normalizer import make_order, make_prototype


def case(widths=("1000", "400"), prototype_widths=("800", "600"), capacity=4):
    rules, task = build(spec=numeric_quality_spec(), orders=tuple(
        make_order(i, width=Decimal(width), rule_attributes=attributes(),
                   min_temperature=Decimal(700 + 100 * i), max_temperature=Decimal(710 + 100 * i))
        for i, width in enumerate(widths)),
        prototypes=tuple(make_prototype(i, width=Decimal(width))
                         for i, width in enumerate(prototype_widths)))
    program = NumericRuleProgram.compile(task, rules)
    quality = NumericQualityProgram.compile(task, program, rules)
    plan = NumericPlan.build(task, (0, 1), (0, 1, 2), (10, 20), (0, 1))
    workspace = NumericCandidateWorkspace.allocate(task, plan, changed_capacity=10,
        chain_capacity=4, node_capacity=capacity, group_capacity=1, event_capacity=4)
    return workspace, program, quality


def assert_tail_equal(workspace, expected):
    count = len(expected.rows)
    for item in fields(NumericNodeColumns):
        np.testing.assert_array_equal(getattr(workspace.nodes, item.name)[:count],
                                      getattr(expected.task.nodes, item.name)[list(expected.rows)],
                                      err_msg=item.name)
    for name in workspace.derived._fields:
        np.testing.assert_array_equal(getattr(workspace.derived, name)[:, :count],
                                      getattr(expected.task, name)[:, list(expected.rows)], err_msg=name)


@pytest.mark.parametrize("widths,prototypes", (
    (("1000", "400"), ("800", "600")),
    (("1000", "600"), ("800", "790", "800")),
    (("1000", "900"), ("950", "950")),
))
@pytest.mark.parametrize("adaptive", (True, False))
def test_private_single_double_and_stable_ties_match_formal_adapter(widths, prototypes, adaptive):
    workspace, program, quality = case(widths, prototypes)
    if not adaptive:
        program = replace(program, rules=tuple(replace(rule, flags=(False, False))
            if rule.kind is NumericRuleKind.TEMPERATURE else rule for rule in program.rules))
        quality = replace(quality, rule_program_fingerprint=program.fingerprint)
    expected = reference_resources.choose_virtual_bridge(workspace.task, program, quality, 0, 1,
                                                max_nodes=2, first_sequence=1)
    assert expected is not None
    status, connected, rows = resources.prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=2, first_sequence=1, chunk_size=1)
    assert status == OK and connected
    assert rows.tolist() == list(expected.rows)
    assert_tail_equal(workspace, expected)
    assert workspace.event_count == 1
    assert workspace.event_node_ends[0] == len(expected.rows)
    assert workspace.event_group_ends[0] == 0
    assert resources.scan_private_bridge.nopython_signatures
    assert resources.append_private_virtuals.nopython_signatures


def test_no_bridge_capacity_cancel_and_invalid_anchors_leave_private_state_unpublished():
    workspace, program, _ = case(capacity=1)
    assert resources.prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=1, first_sequence=1)[:2] == (OK, False)
    assert resources.prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=2, first_sequence=1)[:2] == (CAPACITY, False)
    assert workspace.node_count == workspace.event_count == 0
    checks = []

    def cancel_between_chunks():
        checks.append(1)
        return len(checks) < 3

    assert resources.prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=2, first_sequence=1, chunk_size=1, allows_continue=cancel_between_chunks)[:2] == (CANCELLED, False)
    assert resources.prepare_private_bridge(workspace, program, -1, 1,
        max_nodes=2, first_sequence=1)[:2] == (INVALID, False)
    assert workspace.node_count == workspace.event_count == 0
    workspace.grow_for_retry(changed_capacity=10, chain_capacity=4,
        node_capacity=2, group_capacity=1, event_capacity=4)
    status, connected, rows = resources.prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=2, first_sequence=1)
    assert status == OK and connected and rows.size == 2
    assert workspace.nodes.accepted_sequence[:2].tolist() == [1, 2]


def test_private_rows_can_anchor_a_later_bridge_without_formal_task_extension(monkeypatch):
    workspace, program, _ = case()

    def forbidden(*a, **kw):
        raise AssertionError("formal object/extension path is forbidden")

    monkeypatch.setattr(reference_resources, "extend_resource_workspace", forbidden)
    monkeypatch.setattr(reference_resources, "virtual_node", forbidden)
    status, connected, rows = resources.prepare_private_bridge(workspace, program, 0, 1,
        max_nodes=2, first_sequence=1)
    assert status == OK and connected
    # Last private bridge row already connects directly to the real endpoint.
    assert resources.prepare_private_bridge(workspace, program, int(rows[-1]), 1,
        max_nodes=2, first_sequence=3)[:2] == (OK, True)
    assert workspace.node_count == 2 and workspace.event_count == 1
    assert len(workspace.task.node_ids) == 4


def test_scan_resume_preserves_selected_indices_for_every_chunk_size():
    workspace, program, _ = case()
    results = []
    for size in (1, 2, 64):
        cursor = np.zeros(6, dtype=np.int64)
        calls = 0
        while True:
            result = resources.scan_private_bridge(task_columns(workspace.task), rule_tables(program.rules),
                workspace.nodes, 0, workspace.task.prototype_rows, 0, 1, 2, cursor, size)
            calls += 1
            assert calls < 20
            if result[0] != MORE_WORK:
                break
        assert result[0] == OK
        results.append(cursor.copy())
    for actual in results[1:]:
        np.testing.assert_array_equal(actual, results[0])


def test_sequence_overflow_rejected_before_any_private_write():
    workspace, program, _ = case()
    with pytest.raises(NumericValueError):
        resources.prepare_private_bridge(workspace, program, 0, 1,
            max_nodes=2, first_sequence=(1 << 63) - 1)
    assert workspace.node_count == workspace.event_count == 0
