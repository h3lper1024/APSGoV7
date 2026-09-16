"""Shared candidate computation on borrowed chains and private numeric resources.

Stage control owns enumeration, quota and acceptance. This module has no reverse
dependency on search/refinement and never constructs a formal task or plan.
"""

from dataclasses import dataclass, replace

import numpy as np
from numba import njit

from ._numeric_chain_ops import chain_rows, write_parts, reorder_chains, replace_span
from ._numeric_evaluation import evaluate_numeric_view
from ._numeric_kernel import (
    task_columns, private_task_columns, rule_tables, column_value, _add,
    all_edges_allowed_values, private_edge_node,
)
from ._numeric_resources import (
    prepare_private_bridge, prepare_private_split, prepare_private_separator,
    _append_selected_virtuals,
)
from ._numeric_rules import NumericRuleKind, NumericSplitDecision
from ._numeric_state import (
    NumericCandidateDescriptors, NumericCandidateWorkspace, NumericSearchAction,
    PrivateNodeColumns, PrivateSplitColumns,
    DESCRIPTOR_FIELDS, OK, INVALID, CANCELLED, CAPACITY, readonly,
)
from ._numeric_units import NumericValueError, int64, checked_sum
from .model import MaterialRole, VirtualPurpose

(APPEND, PREPEND, INSERT, REAL_MOVE, ORDER, FILL, SPLIT, INTRA, NODE_MOVE,
 NODE_SWAP, BLOCK_MOVE, BLOCK_SWAP, CUT, RECLAIM) = range(len(NumericSearchAction))
(ACTION, SOURCE, TARGET, NODE, POSITION, START, STOP, OTHER_START, OTHER_STOP,
 REVERSE_SOURCE, REVERSE_TARGET, OWNER, VARIANT) = range(len(DESCRIPTOR_FIELDS))
GROUP, CHAIN, FIRST, LAST, REVERSE = range(5)
_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)
_BRIDGE = tuple(VirtualPurpose).index(VirtualPurpose.EDGE_BRIDGE)


@dataclass(frozen=True, slots=True)
class CandidateCheckPolicy:
    maximum_bridge_nodes: int = 2
    maximum_changed_chain_weight: int = -1
    reject_prohibited_kinds: tuple = ()
    same_period_order: bool = False
    record_order_rows: bool = True

    def __post_init__(self):
        if (type(self.maximum_bridge_nodes) is not int or not 0 <= self.maximum_bridge_nodes <= 2
                or type(self.maximum_changed_chain_weight) is not int
                or self.maximum_changed_chain_weight < -1
                or type(self.same_period_order) is not bool
                or type(self.record_order_rows) is not bool
                or not isinstance(self.reject_prohibited_kinds, tuple)
                or any(not isinstance(k, NumericRuleKind) for k in self.reject_prohibited_kinds)):
            raise NumericValueError("candidate.policy", "explicit original-stage checks required")
        int64(self.maximum_changed_chain_weight, "candidate.maximum_weight")


@dataclass(frozen=True, slots=True, eq=False)
class NumericCandidateAttempt:
    status: int
    prepared: bool
    admissible: bool
    view: object
    summary: object
    affected_rows: np.ndarray
    virtual_sequence: int
    split_sequence: int
    cleaned_variant_exists: bool
    descriptor: np.ndarray
    program: object
    quality_program: object
    policy: CandidateCheckPolicy


@njit
def _chain_index(view, identity):
    for i in range(view.count):
        if view.ids[i] == identity:
            return i
    return -1


@njit
def _valid_descriptor(d):
    action = d[ACTION]
    if d[VARIANT] not in (0, 1):
        return False
    segments = action in (INTRA, NODE_MOVE, NODE_SWAP, BLOCK_MOVE, BLOCK_SWAP)
    if d[VARIANT] and not segments:
        return False
    if action in (APPEND, PREPEND, INSERT):
        return d[NODE] == -1 and d[POSITION] >= 0 and d[SOURCE] != d[TARGET]
    if d[REVERSE_SOURCE] or d[REVERSE_TARGET]:
        return False
    if action in (FILL, RECLAIM, INTRA):
        if d[SOURCE] != d[TARGET]:
            return False
    elif d[SOURCE] == d[TARGET]:
        return False
    if action in (REAL_MOVE, FILL):
        return d[NODE] >= 0 and d[POSITION] >= 0
    if action == SPLIT:
        return d[NODE] >= 0 and d[POSITION] == -1
    if d[NODE] != -1:
        return False
    if action == ORDER:
        return d[POSITION] >= 0
    if action == CUT:
        return d[START] > 0 and d[STOP] == -1 and d[OTHER_START] >= 0 and d[OTHER_STOP] >= 0 and d[OTHER_START] != d[OTHER_STOP]
    if action == RECLAIM:
        return 0 <= d[START] < d[STOP]
    if action == INTRA:
        return 0 <= d[POSITION] < d[START] and d[STOP] == d[START] + 1
    if action in (NODE_MOVE, BLOCK_MOVE):
        minimum = 1 if action == NODE_MOVE else 2
        return d[START] >= 0 and d[STOP] - d[START] >= minimum and d[POSITION] >= 0
    if action in (NODE_SWAP, BLOCK_SWAP):
        minimum = 1 if action == NODE_SWAP else 2
        return d[START] >= 0 and d[STOP] - d[START] >= minimum and 0 <= d[OTHER_START] < d[OTHER_STOP]
    return False


@njit
def _has_real(columns, rows):
    for row in rows:
        if column_value(columns.role, row) != _VIRTUAL:
            return True
    return False


@njit
def _ordinary_bridge(base, row):
    return base.role[row] == _VIRTUAL and base.purpose[row] == _BRIDGE and base.split_group[row] < 0


@njit
def _parts(view, nodes, descriptor):
    """Small numeric slice recipe; no node sequence is copied here."""
    source = _chain_index(view, descriptor[SOURCE])
    target = _chain_index(view, descriptor[TARGET])
    empty = np.empty((0, 5), dtype=np.int64)
    indices = np.empty(0, dtype=np.int64)
    if source < 0 or target < 0:
        return INVALID, empty, indices
    n, m = view.stops[source] - view.starts[source], view.stops[target] - view.starts[target]
    action, position = descriptor[ACTION], descriptor[POSITION]
    a, b, c, d = descriptor[START], descriptor[STOP], descriptor[OTHER_START], descriptor[OTHER_STOP]
    rs, rt = descriptor[REVERSE_SOURCE], descriptor[REVERSE_TARGET]
    if action in (APPEND, PREPEND, INSERT):
        if source == target:
            return INVALID, empty, indices
        position = m if action == APPEND else 0 if action == PREPEND else position
        if position < 0 or position > m:
            return INVALID, empty, indices
        # Target positions address the already reversed target, as in the old
        # whole-chain candidate. Reversal is represented by source slice bounds.
        left_a, left_b = (m - position, m) if rt else (0, position)
        right_a, right_b = (0, m - position) if rt else (position, m)
        return OK, np.array(((0, target, left_a, left_b, rt),
            (0, source, 0, n, rs), (0, target, right_a, right_b, rt)), dtype=np.int64), np.array((target,), dtype=np.int64)
    if action == REAL_MOVE:
        row = descriptor[NODE]
        if source == target or row < 0 or row >= nodes.role.size or nodes.role[row] == _VIRTUAL:
            return INVALID, empty, indices
        rows = chain_rows(view, source)
        a = -1
        for i in range(rows.size):
            if rows[i] == row:
                a = i
                break
        b, c, d = a + 1, position, position
    elif action == INTRA:
        c = position
        if source != target or not 0 <= c < a < b <= n or b != a + 1:
            return INVALID, empty, indices
        return OK, np.array(((0, source, 0, c, 0), (0, source, a, b, 0),
            (0, source, c, a, 0), (0, source, b, n, 0)), dtype=np.int64), np.array((source,), dtype=np.int64)
    elif action in (NODE_MOVE, BLOCK_MOVE):
        c, d = position, position
    elif action not in (NODE_SWAP, BLOCK_SWAP):
        return INVALID, empty, indices
    if source == target or not 0 <= a < b <= n or not 0 <= c <= d <= m:
        return INVALID, empty, indices
    if action in (NODE_SWAP, BLOCK_SWAP) and c == d:
        return INVALID, empty, indices
    return OK, np.array(((0, source, 0, a, 0), (0, target, c, d, 0),
        (0, source, b, n, 0), (1, target, 0, c, 0), (1, source, a, b, 0),
        (1, target, d, m, 0)), dtype=np.int64), np.array((source, target), dtype=np.int64)


@njit
def _trim_parts(view, nodes, parts, trim):
    result = parts.copy()
    removed = np.empty(view.base_rows.size, dtype=np.int64)
    count = 0
    for p in range(parts.shape[0]):
        rows = chain_rows(view, parts[p, CHAIN])
        left, right = parts[p, FIRST], parts[p, LAST]
        if p and parts[p - 1, GROUP] == parts[p, GROUP]:
            while left < right and _ordinary_bridge(nodes, rows[left]):
                removed[count], count = rows[left], count + 1
                left += 1
        if p + 1 < parts.shape[0] and parts[p + 1, GROUP] == parts[p, GROUP]:
            while right > left and _ordinary_bridge(nodes, rows[right - 1]):
                right -= 1
                removed[count], count = rows[right], count + 1
        if trim:
            result[p, FIRST], result[p, LAST] = left, right
    return result, removed[:count]


@njit
def _parts_have_real(view, columns, parts, groups):
    present = np.zeros(groups, dtype=np.bool_)
    for part in parts:
        rows = chain_rows(view, part[CHAIN])[part[FIRST]:part[LAST]]
        if _has_real(columns, rows):
            present[part[GROUP]] = True
    return np.all(present)


@njit
def _affected_parts(view, parts, removed, base_count, private_count):
    marked = np.zeros(base_count, dtype=np.bool_)
    output = np.empty(view.base_rows.size + removed.size + private_count, dtype=np.int64)
    count = 0
    for part in parts:
        for row in chain_rows(view, part[CHAIN])[part[FIRST]:part[LAST]]:
            if not marked[row]:
                output[count], count, marked[row] = row, count + 1, True
    for row in removed:
        # Removed endpoints do not occur in the cleaned parts.
        if not marked[row]:
            output[count], count, marked[row] = row, count + 1, True
    for row in range(base_count, base_count + private_count):
        output[count], count = row, count + 1
    return output[:count]


@njit
def _assigned_period(columns, rows):
    period = -1
    for row in rows:
        if column_value(columns.role, row) != _VIRTUAL:
            value = column_value(columns.period, row)
            period = value if period < 0 else min(period, value)
    return period


@njit
def _finalize_map(view, columns, changed_ids, maximum, group_periods):
    status = np.array((OK, -1, -1, -1), dtype=np.int64)
    for i in range(view.count):
        if view.ids[i] in changed_ids:
            rows = chain_rows(view, i)
            period = _assigned_period(columns, rows)
            if period < 0:
                return INVALID, False
            view.periods[i] = period
            if maximum >= 0:
                weight = 0
                for row in rows:
                    weight = _add(weight, column_value(columns.weight, row), status)
                if status[0] != OK:
                    return status[0], False
                if weight > maximum:
                    return OK, False
    if group_periods:
        # Stable insertion sort of metadata only; no chain/node reconstruction.
        order = np.arange(view.count, dtype=np.int64)
        for i in range(1, order.size):
            j, item = i, order[i]
            while j and view.periods[order[j - 1]] > view.periods[item]:
                order[j] = order[j - 1]
                j -= 1
            order[j] = item
        return reorder_chains(view, order), True
    for i in range(1, view.count):
        if view.periods[i - 1] > view.periods[i]:
            return OK, False
    return OK, True


@njit
def _split_periods_match(view, base_nodes, tail, base_groups, groups):
    base_count = base_nodes.weight.size
    for i in range(view.count):
        for row in chain_rows(view, i):
            group = base_nodes.split_group[row] if row < base_count else tail.split_group[row - base_count]
            if group >= 0:
                target = base_groups.target_period[group] if group < base_groups.target_period.size else groups.target_period[group - base_groups.target_period.size]
                if target != view.periods[i]:
                    return False
    return True


def _columns(workspace):
    return private_task_columns(task_columns(workspace.task), workspace.nodes,
                                workspace.derived, workspace.node_count)


def _base_nodes(task):
    return PrivateNodeColumns(*(getattr(task.nodes, name) for name in PrivateNodeColumns._fields))


def _edge(workspace, program, left, right):
    status = np.array((OK, -1, -1, -1), dtype=np.int64)
    columns = task_columns(workspace.task)
    a = private_edge_node(columns, workspace.nodes, workspace.node_count, int(left), status)
    b = private_edge_node(columns, workspace.nodes, workspace.node_count, int(right), status)
    allowed = all_edges_allowed_values(a, b, rule_tables(program.rules), status)
    return int(status[0]), bool(allowed)


def _append_rows(workspace, rows):
    parts = np.array(((-1, 0, rows.size, 0),), dtype=np.int64)
    status, end = write_parts(workspace.view(), parts, rows, workspace.changed_count)
    if status == OK:
        workspace.changed_count = int(end)
    return int(status)


def _repair_parts(workspace, program, base_view, parts, indices, policy, sequence, allows_continue):
    """Control only a few splice boundaries; scans and copies run on arrays."""
    start = workspace.changed_count
    group = -1
    for part in parts:
        if allows_continue is not None and not allows_continue():
            return CANCELLED, False, sequence
        next_group = int(part[GROUP])
        if next_group != group:
            if group >= 0:
                target = int(indices[group])
                workspace.starts[target], workspace.stops[target], workspace.private[target] = start, workspace.changed_count, True
            start, group = workspace.changed_count, next_group
        rows = chain_rows(base_view, int(part[CHAIN]))[int(part[FIRST]):int(part[LAST])]
        if part[REVERSE]:
            rows = rows[::-1]
        if not rows.size:
            continue
        if workspace.changed_count > start:
            status, connected, bridge = prepare_private_bridge(workspace, program,
                int(workspace.changed_rows[workspace.changed_count - 1]), int(rows[0]),
                max_nodes=policy.maximum_bridge_nodes, first_sequence=sequence + 1,
                allows_continue=allows_continue)
            if status != OK or not connected:
                return status, False, sequence
            status = _append_rows(workspace, bridge)
            if status != OK:
                return status, False, sequence
            sequence += bridge.size
        status = _append_rows(workspace, rows)
        if status != OK:
            return status, False, sequence
    if group >= 0:
        target = int(indices[group])
        workspace.starts[target], workspace.stops[target], workspace.private[target] = start, workspace.changed_count, True
    return OK, True, int(sequence)


def _base_view(workspace):
    # Editing the candidate metadata must not change later reads of source parts.
    return workspace.view()._replace(starts=workspace.starts.copy(), stops=workspace.stops.copy(),
        private=workspace.private.copy(), ids=workspace.ids.copy(), periods=workspace.periods.copy())


def _move_order(workspace, source, position):
    order = np.arange(workspace.chain_count, dtype=np.int64)
    if source < position:
        order[source:position] = order[source + 1:position + 1]
    elif source > position:
        order[position + 1:source + 1] = order[position:source]
    order[position] = source
    return int(reorder_chains(workspace.view(), order))


def _local_resource_edit(workspace, program, descriptor, source, policy, sequence):
    rows = chain_rows(workspace.view(), source)
    empty = np.empty(0, dtype=np.int64)
    if descriptor[ACTION] == FILL:
        position, prototype = int(descriptor[POSITION]), int(descriptor[NODE])
        if not 0 <= position <= rows.size or not 0 <= prototype < workspace.task.prototype_rows.size:
            return INVALID, False, sequence, empty
        left, right = rows[max(0, position - 1)], rows[min(rows.size - 1, position)]
        next_sequence = checked_sum((sequence, 1), "candidate.virtual_sequence")
        status, found, added = _append_selected_virtuals(workspace,
            np.array((prototype,), dtype=np.int64), int(left), int(right),
            VirtualPurpose.WEIGHT_FILL, next_sequence, -1)
        if status != OK or not found:
            return status, False, sequence, empty
        if position:
            status, allowed = _edge(workspace, program, left, added[0])
            if status != OK or not allowed:
                return status, False, sequence, empty
        if position < rows.size:
            status, allowed = _edge(workspace, program, added[0], right)
            if status != OK or not allowed:
                return status, False, sequence, empty
        status, end = replace_span(workspace.view(), source, position, position, added, workspace.changed_count)
        if status == OK:
            workspace.changed_count = int(end)
        return int(status), status == OK, next_sequence, added
    start, stop = int(descriptor[START]), int(descriptor[STOP])
    if not 0 <= start < stop <= rows.size:
        return INVALID, False, sequence, empty
    removed = rows[start:stop]
    if not _ordinary_run(_base_nodes(workspace.task), removed):
        return OK, False, sequence, empty
    if start and stop < rows.size:
        status, allowed = _edge(workspace, program, rows[start - 1], rows[stop])
        if status != OK or not allowed:
            return status, False, sequence, empty
    status, end = replace_span(workspace.view(), source, start, stop, empty, workspace.changed_count)
    if status == OK:
        workspace.changed_count = int(end)
    return int(status), status == OK, sequence, removed


@njit
def _ordinary_run(nodes, rows):
    for row in rows:
        if not _ordinary_bridge(nodes, row):
            return False
    return True


@njit
def _cut_map(view, columns, source, cut, new_id, prefix_position, suffix_position):
    if (view.count >= view.ids.size or cut <= 0
            or cut >= view.stops[source] - view.starts[source]):
        return CAPACITY if view.count >= view.ids.size else INVALID, False
    count = view.count + 1
    if (not 0 <= prefix_position < count or not 0 <= suffix_position < count
            or prefix_position == suffix_position or new_id < 0 or _chain_index(view, new_id) >= 0):
        return INVALID, False
    rows = chain_rows(view, source)
    prefix_period, suffix_period = _assigned_period(columns, rows[:cut]), _assigned_period(columns, rows[cut:])
    if prefix_period < 0 or suffix_period < 0:
        return OK, False
    starts, stops, private = view.starts.copy(), view.stops.copy(), view.private.copy()
    ids, periods = view.ids.copy(), view.periods.copy()
    remaining = 0
    for position in range(count):
        if position == prefix_position or position == suffix_position:
            view.private[position] = private[source]
            if position == prefix_position:
                view.starts[position], view.stops[position] = starts[source], starts[source] + cut
                view.ids[position], view.periods[position] = ids[source], prefix_period
            else:
                view.starts[position], view.stops[position] = starts[source] + cut, stops[source]
                view.ids[position], view.periods[position] = new_id, suffix_period
        else:
            if remaining == source:
                remaining += 1
            view.starts[position], view.stops[position] = starts[remaining], stops[remaining]
            view.private[position], view.ids[position], view.periods[position] = private[remaining], ids[remaining], periods[remaining]
            remaining += 1
    return OK, True


def _cut_candidate(workspace, descriptor, source):
    affected = chain_rows(workspace.view(), source)
    status, found = _cut_map(workspace.view(), task_columns(workspace.task), source,
        int(descriptor[START]), int(descriptor[TARGET]), int(descriptor[OTHER_START]), int(descriptor[OTHER_STOP]))
    if status == OK and found:
        workspace.chain_count += 1
    return int(status), bool(found), affected


@njit
def _split_sides(view, nodes, source, parent):
    rows = chain_rows(view, source)
    position = -1
    for i in range(rows.size):
        if rows[i] == parent:
            position = i
            break
    if position < 0:
        return INVALID, -1, -1
    left, right = position, position + 1
    while left and _ordinary_bridge(nodes, rows[left - 1]):
        left -= 1
    while right < rows.size and _ordinary_bridge(nodes, rows[right]):
        right += 1
    if (left and nodes.role[rows[left - 1]] == _VIRTUAL
            or right < rows.size and nodes.role[rows[right]] == _VIRTUAL):
        return OK, -1, -1
    return OK, left, right


def _split_candidate(workspace, program, descriptor, source, decision, policy,
                     sequence, split_sequence, allows_continue):
    base, parent = _base_view(workspace), int(descriptor[NODE])
    original = chain_rows(base, source)
    empty = np.empty(0, dtype=np.int64)
    status, left, right = _split_sides(base, _base_nodes(workspace.task), source, parent)
    if status != OK or left < 0:
        return int(status), False, sequence, split_sequence, empty
    removed = original[left:right]
    parts = np.array(((0, source, 0, left, 0), (0, source, right, original.size, 0)), dtype=np.int64)
    status, found, sequence = _repair_parts(workspace, program, base, parts,
        np.array((source,), dtype=np.int64), policy, sequence, allows_continue)
    if status != OK or not found:
        return status, False, sequence, split_sequence, empty
    remaining = workspace.stops[source] > workspace.starts[source]
    if remaining:
        period = _assigned_period(_columns(workspace), chain_rows(workspace.view(), source))
        if period < 0:
            return INVALID, False, sequence, split_sequence, empty
        workspace.periods[source] = period
    split_sequence = checked_sum((split_sequence, 1), "candidate.split_sequence")
    group_index = workspace.task.split_groups.parent_row.size + workspace.group_count
    status, found, pieces = prepare_private_split(workspace, parent, decision,
        int(base.periods[source]), sequence=split_sequence)
    if status != OK or not found:
        return status, False, sequence, split_sequence, empty
    start, separator_weight = workspace.changed_count, 0
    status = _append_rows(workspace, pieces[:1])
    if status != OK:
        return status, False, sequence, split_sequence, empty
    for i in range(1, pieces.size):
        next_sequence = checked_sum((sequence, 1), "candidate.virtual_sequence")
        status, found, separator = prepare_private_separator(workspace, program,
            int(pieces[i - 1]), int(pieces[i]), sequence=next_sequence, group_index=group_index,
            allows_continue=allows_continue)
        if status != OK or not found:
            return status, False, sequence, split_sequence, empty
        separator_weight = checked_sum((separator_weight,
            int(workspace.nodes.weight[int(separator[0]) - workspace.task.nodes.weight.size])), "candidate.separator_weight")
        if separator_weight > decision.maximum_separator_weight:
            return OK, False, sequence, split_sequence, empty
        sequence = next_sequence
        status = _append_rows(workspace, np.array((separator[0], pieces[i]), dtype=np.int64))
        if status != OK:
            return status, False, sequence, split_sequence, empty
    new_id = checked_sum((int(base.ids[:base.count].max()), 1), "candidate.chain_id")
    if descriptor[TARGET] != new_id:
        return INVALID, False, sequence, split_sequence, empty
    if remaining:
        if workspace.chain_count == workspace.ids.size:
            return CAPACITY, False, sequence, split_sequence, empty
        new_index = workspace.chain_count
        workspace.chain_count += 1
    else:
        _move_order(workspace, source, workspace.chain_count - 1)
        new_index = workspace.chain_count - 1
    workspace.starts[new_index], workspace.stops[new_index] = start, workspace.changed_count
    workspace.private[new_index], workspace.ids[new_index], workspace.periods[new_index] = True, new_id, decision.target_period
    added = np.arange(workspace.task.nodes.weight.size,
                      workspace.task.nodes.weight.size + workspace.node_count, dtype=np.int64)
    return OK, True, sequence, split_sequence, np.concatenate((removed, added))


def prepare_candidate_attempt(workspace, program, quality, descriptors, index, policy, *,
                              virtual_sequence=0, split_sequence=0, split_decision=None,
                              allows_continue=None):
    """Prepare one declared repair variant. Never charge quota or publish state.

    Variant 0 is original, 1 trims ordinary inner bridges (only if any exist).
    The caller owns the original cleaned-before-original attempt/charging order.
    FILL uses node_row for the selected prototype index; CUT uses target_start/
    target_stop for the prefix/suffix final positions and target_id for the new ID.
    Split control may pause here at its original preparation-before-charge point.
    All successful preparations must finish through compute_candidate_attempt.
    """
    if (not isinstance(workspace, NumericCandidateWorkspace)
            or not isinstance(descriptors, NumericCandidateDescriptors)
            or not isinstance(policy, CandidateCheckPolicy)
            or type(index) is not int or not 0 <= index < descriptors.values.shape[0]):
        raise NumericValueError("candidate", "matching numeric workspace, descriptor and policy required")
    descriptors.require_current(workspace.task, workspace.plan)
    if program.task_fingerprint != workspace.task.fingerprint or quality.rule_program_fingerprint != program.fingerprint:
        raise NumericValueError("candidate", "matching rule and quality programs required")
    for name, value in (("virtual_sequence", virtual_sequence), ("split_sequence", split_sequence)):
        if type(value) is not int or value < 0:
            raise NumericValueError(name, "nonnegative resource sequence required")
        int64(value, name)
    workspace.reset()
    descriptor = descriptors.values[index]
    action = int(descriptor[ACTION])
    base = _base_view(workspace)
    source, target = _chain_index(base, descriptor[SOURCE]), _chain_index(base, descriptor[TARGET])
    affected = np.empty(0, dtype=np.int64)
    cleaned = False

    def result(status=OK, prepared=False, admissible=False, summary=None):
        return NumericCandidateAttempt(int(status), prepared, admissible, workspace.view(), summary,
            readonly(affected, np.int64), int(virtual_sequence), int(split_sequence), cleaned,
            descriptor, program, quality, policy)

    if allows_continue is not None and not allows_continue():
        return result(CANCELLED)
    if source < 0 or not _valid_descriptor(descriptor):
        return result(INVALID)
    if action == SPLIT:
        if not isinstance(split_decision, NumericSplitDecision):
            raise NumericValueError("candidate.split", "explicit authorized split decision required")
        status, found, virtual_sequence, split_sequence, affected = _split_candidate(
            workspace, program, descriptor, source, split_decision, policy,
            virtual_sequence, split_sequence, allows_continue)
        if status != OK or not found:
            return result(status)
        changed_ids = np.empty(0, dtype=np.int64)  # Split helper sets authorized target period.
        group_periods = True
    elif action == CUT:
        status, found, affected = _cut_candidate(workspace, descriptor, source)
        if status != OK or not found:
            return result(status)
        changed_ids, group_periods = np.empty(0, dtype=np.int64), False
    elif action == ORDER:
        position = int(descriptor[POSITION])
        if target < 0 or not 0 <= position < workspace.chain_count or base.ids[position] != descriptor[TARGET]:
            return result(INVALID)
        if policy.same_period_order and base.periods[source] != base.periods[target]:
            return result()
        affected = chain_rows(base, source) if policy.record_order_rows else affected
        status = _move_order(workspace, source, position)
        if status != OK:
            return result(status)
        changed_ids, group_periods = np.empty(0, dtype=np.int64), False
    elif action in (FILL, RECLAIM):
        status, found, virtual_sequence, affected = _local_resource_edit(
            workspace, program, descriptor, source, policy, virtual_sequence)
        if status != OK or not found:
            return result(status)
        changed_ids, group_periods = np.array((descriptor[SOURCE],), dtype=np.int64), True
    else:
        status, parts, indices = _parts(base, _base_nodes(workspace.task), descriptor)
        if status != OK:
            return result(status)
        if action in (INTRA, NODE_MOVE, NODE_SWAP, BLOCK_MOVE, BLOCK_SWAP):
            if not _parts_have_real(base, task_columns(workspace.task), parts, indices.size):
                return result()
            if action != INTRA:
                source_rows = chain_rows(base, source)[descriptor[START]:descriptor[STOP]]
                if not _has_real(task_columns(workspace.task), source_rows):
                    return result()
                if action in (NODE_SWAP, BLOCK_SWAP) and not _has_real(task_columns(workspace.task),
                        chain_rows(base, target)[descriptor[OTHER_START]:descriptor[OTHER_STOP]]):
                    return result()
            parts, removed = _trim_parts(base, _base_nodes(workspace.task), parts, descriptor[VARIANT] == 1)
            cleaned = bool(removed.size)
            if descriptor[VARIANT] == 1 and not cleaned:
                return result()
            if descriptor[VARIANT] == 0:
                removed = removed[:0]
        else:
            if descriptor[VARIANT] != 0:
                return result(INVALID)
            removed = affected
        actual_policy = policy if action != REAL_MOVE else CandidateCheckPolicy(0,
            policy.maximum_changed_chain_weight, policy.reject_prohibited_kinds, policy.same_period_order)
        status, found, virtual_sequence = _repair_parts(workspace, program, base, parts,
            indices, actual_policy, virtual_sequence, allows_continue)
        if status != OK or not found:
            return result(status)
        if action in (APPEND, PREPEND, INSERT):
            # Original policy appends the merged target after all retained chains,
            # before stable period grouping, rather than keeping its old slot.
            status = _move_order(workspace, target, workspace.chain_count - 1)
            source = _chain_index(workspace.view(), descriptor[SOURCE])
            status = _move_order(workspace, source, workspace.chain_count - 1)
            workspace.chain_count -= 1
            affected = np.arange(workspace.task.nodes.weight.size,
                workspace.task.nodes.weight.size + workspace.node_count, dtype=np.int64)
        elif action == REAL_MOVE:
            affected = np.array((descriptor[NODE],), dtype=np.int64)
        else:
            affected = _affected_parts(base, parts, removed, workspace.task.nodes.weight.size, workspace.node_count)
        changed_ids = base.ids[indices]
        group_periods = True
    status, found = _finalize_map(workspace.view(), _columns(workspace), changed_ids,
        policy.maximum_changed_chain_weight, group_periods)
    if status != OK or not found:
        return result(status)
    return result(OK, True)


def compute_candidate_attempt(workspace, program, quality, descriptors, index, policy, *,
                              virtual_sequence=0, split_sequence=0, split_decision=None,
                              previous_evaluation=None, allows_continue=None, preparation=None):
    """Unique complete attempt; optionally resume at the original split quota boundary."""
    if preparation is None:
        preparation = prepare_candidate_attempt(workspace, program, quality, descriptors, index, policy,
            virtual_sequence=virtual_sequence, split_sequence=split_sequence,
            split_decision=split_decision, allows_continue=allows_continue)
    else:
        if (not isinstance(preparation, NumericCandidateAttempt)
                or type(index) is not int or not 0 <= index < descriptors.values.shape[0]):
            raise NumericValueError("candidate.resume", "matching unfinished preparation required")
        descriptors.require_current(workspace.task, workspace.plan)
        workspace.require_view(preparation.view)
        if (preparation.program is not program or preparation.quality_program is not quality
                or preparation.policy != policy or preparation.summary is not None
                or not np.array_equal(preparation.descriptor, descriptors.values[index])):
            raise NumericValueError("candidate.resume", "matching unfinished preparation required")
    if preparation.status != OK or not preparation.prepared:
        return preparation
    base_groups = PrivateSplitColumns(*(getattr(workspace.task.split_groups, name)
                                         for name in PrivateSplitColumns._fields))
    if not _split_periods_match(workspace.view(), _base_nodes(workspace.task), workspace.nodes,
                                base_groups, workspace.split_groups):
        return replace(preparation, prepared=False)
    if allows_continue is not None and not allows_continue():
        return replace(preparation, status=CANCELLED, prepared=False)
    summary = evaluate_numeric_view(workspace, program, quality, previous_evaluation=previous_evaluation)
    admissible = not any(summary.hits[:, rule.index].any() for rule in program.rules
                         if rule.kind in policy.reject_prohibited_kinds)
    return replace(preparation, admissible=admissible, summary=summary)
