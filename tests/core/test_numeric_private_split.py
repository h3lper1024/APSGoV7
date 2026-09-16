"""Private splits keep the existing resource fields, event order and exact clock."""

from dataclasses import fields, replace
from decimal import Decimal
from random import Random

import numpy as np
import pytest

from apsgo_scheduler.core import _numeric_resources as resources
from apsgo_scheduler.core._numeric_rules import evaluate_numeric_split
from apsgo_scheduler.core._numeric_rules import NumericRuleProgram
from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec
from apsgo_scheduler.core._numeric_search import _split_piece_weights
from apsgo_scheduler.core._numeric_state import (
    NumericCandidateWorkspace, NumericPlan, NumericNodeColumns, NumericSplitGroups,
    OK, CAPACITY, CANCELLED,
)
from apsgo_scheduler.core._numeric_units import allocate_piece_milliseconds, NumericValueError
from tests.app.test_input_normalizer import make_order
from tests.core.test_numeric_rules import build, attributes
from tests.core.test_numeric_evaluation import numeric_quality_spec


def split_case(weight="600", source_period="P0", capacity=8):
    spec = numeric_quality_spec()
    spec = replace(spec, rules=tuple(replace(rule, parameters={**rule.parameters, 'max_weight': Decimal(2000)})
        if rule.rule_type == 'ChainWeightRangeRule' else rule for rule in spec.rules))
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    rules, task = build(spec=spec, orders=(make_order(0, weight=Decimal(weight), width=Decimal(1000),
        source_period=source_period, rule_attributes=attributes(grade_class="IF钢")),))
    program = NumericRuleProgram.compile(task, rules)
    quality = NumericQualityProgram.compile(task, program, rules)
    plan = NumericPlan.build(task, (0,), (0, 1), (10,), (0,))
    workspace = NumericCandidateWorkspace.allocate(task, plan, changed_capacity=16,
        chain_capacity=3, node_capacity=capacity, group_capacity=2, event_capacity=6)
    decision = evaluate_numeric_split(task, program, 0, 0, 0)
    return workspace, program, quality, decision


def formal_split(workspace, program, quality, decision):
    task = workspace.task
    weights = _split_piece_weights(int(task.nodes.weight[0]), decision)
    durations = allocate_piece_milliseconds(int(task.nodes.weight[0]), int(task.nodes.duration_ms[0]),
                                            weights, "test")
    group = resources.split_group(task, 0, decision, 0, 1)
    nodes = tuple(resources.split_piece_node(task, 0, group_index=0, piece_index=i,
        piece_count=len(weights), weight=w, duration_ms=d, accepted_sequence=1)
        for i, (w, d) in enumerate(zip(weights, durations), 1))
    return resources.extend_resource_workspace(task, program, quality, nodes, split_group=group)


def assert_all_private_fields(workspace, task):
    start = workspace.task.nodes.weight.size
    assert task.nodes.weight.size == start + workspace.node_count
    for item in fields(NumericNodeColumns):
        np.testing.assert_array_equal(getattr(workspace.nodes, item.name)[:workspace.node_count],
                                      getattr(task.nodes, item.name)[start:], err_msg=item.name)
    for name in workspace.derived._fields:
        np.testing.assert_array_equal(getattr(workspace.derived, name)[:, :workspace.node_count],
                                      getattr(task, name)[:, start:], err_msg=name)
    for item in fields(NumericSplitGroups):
        np.testing.assert_array_equal(getattr(workspace.split_groups, item.name)[:workspace.group_count],
                                      getattr(task.split_groups, item.name), err_msg=item.name)


@pytest.mark.parametrize("weight", ("600", "1200"))
@pytest.mark.parametrize("period", ("P0", "P1"))
def test_private_split_and_separator_match_original_fields_and_extension_events(weight, period):
    workspace, program, quality, decision = split_case(weight, period)
    expected = formal_split(workspace, program, quality, decision)
    status, eligible, rows = resources.prepare_private_split(workspace, 0, decision, 0, sequence=1)
    assert status == OK and eligible
    assert list(rows) == list(expected.rows)
    assert_all_private_fields(workspace, expected.task)
    assert workspace.split_groups.target_period[0] == (0 if period == "P0" else 1)
    pieces = list(map(int, rows))
    for sequence, (left, right) in enumerate(zip(pieces, pieces[1:]), 1):
        expected = resources.choose_split_separator(expected.task, expected.program, expected.quality,
            left, right, sequence=sequence, group_index=0)
        assert expected is not None
        status, found, selected = resources.prepare_private_separator(workspace, program,
            left, right, sequence=sequence, group_index=0, chunk_size=1)
        assert status == OK and found
        assert selected.tolist() == list(expected.rows)
        assert_all_private_fields(workspace, expected.task)
    assert workspace.event_node_ends[:workspace.event_count].tolist() == list(range(len(pieces), 2 * len(pieces)))
    assert workspace.event_group_ends[:workspace.event_count].tolist() == [1] * len(pieces)
    assert resources.append_private_split.nopython_signatures
    assert resources.scan_private_separator.nopython_signatures


def test_native_split_rounding_matches_wide_integer_half_up_without_intermediate_overflow():
    random = Random(531)
    maximum = (1 << 63) - 1
    status = np.array((OK, -1, -1, -1), dtype=np.int64)
    samples = [(1, 1, 2), (9, 1, 2), (maximum, maximum - 1, maximum),
               (maximum, maximum, maximum), (maximum, 1, 2)]
    for _ in range(300):
        a, d = random.randrange(1, maximum), random.randrange(1, maximum)
        samples.append((a, random.randrange(d + 1), d))
    for a, b, d in samples:
        quotient, remainder = divmod(a * b, d)
        assert resources.round_product_ratio(a, b, d, status) == quotient + int(remainder >= d - remainder)
        assert status[0] == OK
    for weight, duration, limit in ((120003, 5, 50000), (maximum, maximum, maximum // 2)):
        result, weights, durations = resources.split_piece_arrays(weight, duration, 1, limit, 3)
        assert result[0] == OK
        assert tuple(durations) == allocate_piece_milliseconds(weight, duration, tuple(map(int, weights)), "test")
        assert sum(map(int, weights)) == weight
        assert sum(map(int, durations)) == duration


def test_split_failures_and_separator_cancellation_do_not_advance_valid_lengths(monkeypatch):
    workspace, program, quality, decision = split_case(capacity=1)
    with monkeypatch.context() as limited:
        def forbidden_allocation(*args):
            raise AssertionError("piece allocation before capacity check")
        limited.setattr(resources, "split_piece_arrays", forbidden_allocation)
        assert resources.prepare_private_split(workspace, 0, decision, 0, sequence=1)[:2] == (CAPACITY, False)
    assert (workspace.node_count, workspace.group_count, workspace.event_count) == (0, 0, 0)
    workspace.grow_for_retry(changed_capacity=16, chain_capacity=3, node_capacity=8,
                             group_capacity=2, event_capacity=6)
    status, found, rows = resources.prepare_private_split(workspace, 0, decision, 0, sequence=1)
    assert status == OK and found
    before = (workspace.node_count, workspace.group_count, workspace.event_count)
    assert resources.prepare_private_separator(workspace, program, int(rows[0]), int(rows[1]),
        sequence=1, group_index=0, allows_continue=lambda: False)[:2] == (CANCELLED, False)
    with pytest.raises(NumericValueError):
        resources.prepare_private_split(workspace, 0, decision, 0, sequence=2)
    assert (workspace.node_count, workspace.group_count, workspace.event_count) == before
    workspace.reset()
    assert workspace.node_count == workspace.event_count == workspace.group_count == 0


def test_rejected_piece_remainder_returns_no_resource_and_originals_stay_shared():
    workspace, program, quality, decision = split_case(weight="500.001")
    original_weight = workspace.task.nodes.weight.copy()
    assert decision.eligible
    assert resources.prepare_private_split(workspace, 0, decision, 0, sequence=1)[:2] == (OK, False)
    assert workspace.node_count == workspace.group_count == workspace.event_count == 0
    np.testing.assert_array_equal(workspace.task.nodes.weight, original_weight)


def test_split_and_separator_work_with_formal_objects_and_extension_disabled(monkeypatch):
    workspace, program, quality, decision = split_case()

    def forbidden(*args, **kwargs):
        raise AssertionError("formal resource path used")

    for name in ("split_piece_node", "split_group", "virtual_node", "extend_resource_workspace"):
        monkeypatch.setattr(resources, name, forbidden)
    status, found, rows = resources.prepare_private_split(workspace, 0, decision, 0, sequence=1)
    assert status == OK and found
    assert resources.prepare_private_separator(workspace, program, int(rows[0]), int(rows[1]),
        sequence=1, group_index=0)[:2] == (OK, True)
    assert workspace.node_count == 3 and workspace.event_count == 2
