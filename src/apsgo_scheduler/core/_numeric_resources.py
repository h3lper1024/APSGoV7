"""Candidate-private numeric rows for virtual material and controlled splits."""

from dataclasses import dataclass

import numpy as np
from numba import njit, literal_unroll

from ._numeric_kernel import (
    EdgeNode, edge_node, private_edge_node, all_edges_allowed_values,
    task_columns, rule_tables, _add, _sub, _abs, _mul,
    gather_local_task_columns, evaluate_chain_kernel, MAX,
)
from ._numeric_evaluation import NumericQualityProgram
from ._numeric_rules import NumericRuleProgram, NumericSplitDecision
from ._numeric_state import (
    NumericDynamicNode, NumericSplitGroup, NumericTask, NumericCandidateWorkspace,
    PrivateNodeColumns, PrivateDerivedColumns, OK, INVALID, CANCELLED, CAPACITY,
    MORE_WORK, extend_numeric_task,
)
from ._numeric_units import NumericValueError, checked_sum, int64
from .model import MaterialRole, VirtualPurpose

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)
_PURPOSES = tuple(VirtualPurpose)
_MIN_TEMPERATURE_PRESENT = 2
_MAX_TEMPERATURE_PRESENT = len(("width", "thickness", "min_temperature"))
_NODE_FIELDS = PrivateNodeColumns._fields
_DERIVED_FIELDS = PrivateDerivedColumns._fields
_SCAN_PHASE, _SCAN_NEXT, _SCAN_FIRST, _SCAN_SECOND, _SCAN_SCORE, _SCAN_COUNT = range(6)
_SCAN_START, _SCAN_SINGLE, _SCAN_DOUBLE, _SCAN_DONE = range(4)
_SEP_NEXT, _SEP_BEST, _SEP_COUNT, _SEP_SEVERITY, _SEP_SMOOTH, _SEP_DONE = range(6)


@dataclass(frozen=True, slots=True)
class NumericResourceExtension:
    task: NumericTask
    program: NumericRuleProgram
    quality: NumericQualityProgram
    rows: tuple[int, ...]


@njit
def _virtual_edge_values(t, prototype, left, right):
    template = edge_node(t, prototype)
    minimum = (min(left.minimum, right.minimum) if left.has_minimum and right.has_minimum
               else left.minimum if left.has_minimum else right.minimum if right.has_minimum else 0)
    maximum = (max(left.maximum, right.maximum) if left.has_maximum and right.has_maximum
               else left.maximum if left.has_maximum else right.maximum if right.has_maximum else 0)
    return EdgeNode(template.width, template.thickness, minimum, maximum,
                    _GENERATED_VIRTUAL, template.hot, template.soft,
                    template.has_width, template.has_thickness,
                    left.has_minimum or right.has_minimum, left.has_maximum or right.has_maximum)


@njit
def _pair_smoothness(left, right, scales, status):
    common = max(scales[0], scales[1])
    if common % scales[0] or common % scales[1]:
        status[0] = INVALID
        return 0
    width = _mul(_abs(_sub(left.width, right.width, status), status), common // scales[0], status)
    thickness = _mul(_abs(_sub(left.thickness, right.thickness, status), status),
                     common // scales[1], status)
    return _add(width, _mul(thickness, 100, status), status)


@njit
def scan_private_bridge(t, rules, tail, private_count, prototypes, left_row, right_row,
                        maximum_nodes, cursor, work_limit):
    """Resume ordered direct/single/double scanning; no resource or ID is allocated."""
    status = np.array((OK, -1, -1, -1), dtype=np.int64)
    if maximum_nodes < 0 or maximum_nodes > 2 or work_limit <= 0:
        status[0] = INVALID
        return status
    left = private_edge_node(t, tail, private_count, left_row, status)
    right = private_edge_node(t, tail, private_count, right_row, status)
    if status[0] != OK:
        return status
    if cursor[_SCAN_PHASE] == _SCAN_START:
        cursor[_SCAN_COUNT] = -1
        cursor[_SCAN_FIRST] = cursor[_SCAN_SECOND] = -1
        if all_edges_allowed_values(left, right, rules, status):
            cursor[_SCAN_COUNT] = 0
            cursor[_SCAN_PHASE] = _SCAN_DONE
        elif maximum_nodes == 0 or prototypes.size == 0:
            cursor[_SCAN_PHASE] = _SCAN_DONE
        else:
            cursor[_SCAN_PHASE], cursor[_SCAN_NEXT] = _SCAN_SINGLE, 0
    if status[0] != OK:
        return status
    work = 0
    while cursor[_SCAN_PHASE] != _SCAN_DONE and work < work_limit:
        phase = cursor[_SCAN_PHASE]
        count = prototypes.size
        limit = count if phase == _SCAN_SINGLE else count * count
        if cursor[_SCAN_NEXT] >= limit:
            if cursor[_SCAN_FIRST] >= 0:
                cursor[_SCAN_COUNT] = 1 if phase == _SCAN_SINGLE else 2
                cursor[_SCAN_PHASE] = _SCAN_DONE
            elif phase == _SCAN_SINGLE and maximum_nodes == 2:
                cursor[_SCAN_PHASE], cursor[_SCAN_NEXT] = _SCAN_DOUBLE, 0
            else:
                cursor[_SCAN_PHASE] = _SCAN_DONE
            continue
        position = cursor[_SCAN_NEXT]
        cursor[_SCAN_NEXT] += 1
        work += 1
        first = position if phase == _SCAN_SINGLE else position // count
        second = -1 if phase == _SCAN_SINGLE else position % count
        a = _virtual_edge_values(t, prototypes[first], left, right)
        allowed = all_edges_allowed_values(left, a, rules, status)
        score = 0
        if phase == _SCAN_SINGLE:
            if allowed:
                allowed = all_edges_allowed_values(a, right, rules, status)
            if allowed:
                score = _add(_pair_smoothness(left, a, t.scales, status),
                             _pair_smoothness(a, right, t.scales, status), status)
        else:
            b = _virtual_edge_values(t, prototypes[second], left, right)
            if allowed:
                allowed = all_edges_allowed_values(a, b, rules, status)
            if allowed:
                allowed = all_edges_allowed_values(b, right, rules, status)
            if allowed:
                middle = _pair_smoothness(a, b, t.scales, status)
                score = _add(_add(_pair_smoothness(left, a, t.scales, status), middle, status),
                             _add(middle, _pair_smoothness(b, right, t.scales, status), status), status)
        if status[0] != OK:
            return status
        if allowed and (cursor[_SCAN_FIRST] < 0 or score < cursor[_SCAN_SCORE]):
            cursor[_SCAN_FIRST], cursor[_SCAN_SECOND], cursor[_SCAN_SCORE] = first, second, score
    if cursor[_SCAN_PHASE] != _SCAN_DONE:
        status[0] = MORE_WORK
    return status


@njit
def copy_resource_templates(base, derived, tail, tail_derived, templates, selected, offset):
    for k in range(selected.size):
        row, template = offset + k, selected[k]
        for name in literal_unroll(_NODE_FIELDS):
            getattr(tail, name)[row] = getattr(base, name)[template]
        for name in literal_unroll(_DERIVED_FIELDS):
            getattr(tail_derived, name)[:, row] = getattr(derived, name)[:, template]
        templates[row] = template


@njit
def append_private_virtuals(base, derived, tail, tail_derived, templates, selected,
                            prototypes, offset, left, right, purpose, first_sequence, group):
    """Write only selected templates, in one original resource extension event."""
    copy_resource_templates(base, derived, tail, tail_derived, templates, prototypes[selected], offset)
    minimum = (min(left.minimum, right.minimum) if left.has_minimum and right.has_minimum
               else left.minimum if left.has_minimum else right.minimum if right.has_minimum else 0)
    maximum = (max(left.maximum, right.maximum) if left.has_maximum and right.has_maximum
               else left.maximum if left.has_maximum else right.maximum if right.has_maximum else 0)
    for k in range(selected.size):
        row, prototype = offset + k, selected[k]
        tail.role[row] = _GENERATED_VIRTUAL
        tail.source[row] = tail.resource[row] = tail.source_period[row] = -1
        tail.prototype[row], tail.purpose[row], tail.split_group[row] = prototype, purpose, group
        tail.piece_index[row], tail.piece_count[row] = -1, 0
        tail.accepted_sequence[row] = first_sequence + k
        tail.min_temperature[row], tail.max_temperature[row] = minimum, maximum
        tail.present[row, _MIN_TEMPERATURE_PRESENT] = left.has_minimum or right.has_minimum
        tail.present[row, _MAX_TEMPERATURE_PRESENT] = left.has_maximum or right.has_maximum


@njit
def append_virtual_selection_native(columns, base, base_derived, tail, derived,
        templates, prototypes, event_node_ends, event_group_ends, node_count,
        group_count, event_count, selected, left, right, purpose, first_sequence, split_group):
    """One selected resource event; scans and callers share this exact write path."""
    empty = np.empty(0, np.int64)
    count = selected.size
    if node_count + count > templates.size or event_count >= event_node_ends.size:
        return CAPACITY, False, empty, node_count, event_count
    status = np.array((OK, -1, -1, -1), np.int64)
    left_values = private_edge_node(columns, tail, node_count, left, status)
    right_values = private_edge_node(columns, tail, node_count, right, status)
    if status[0] != OK:
        return status[0], False, empty, node_count, event_count
    append_private_virtuals(base, base_derived, tail, derived, templates, selected,
        prototypes, node_count, left_values, right_values, purpose, first_sequence, split_group)
    end = node_count + count
    event_node_ends[event_count], event_group_ends[event_count] = end, group_count
    return OK, True, np.arange(base.weight.size + node_count, base.weight.size + end), end, event_count + 1


@njit
def private_bridge_step(columns, rules, base, base_derived, tail, derived, templates,
        prototypes, event_node_ends, event_group_ends, node_count, group_count,
        event_count, left, right, max_nodes, first_sequence, purpose, split_group,
        cursor, work_limit):
    """One bounded scan slice plus the sole selected-resource write on completion."""
    status = scan_private_bridge(columns, rules, tail, node_count, prototypes,
        left, right, max_nodes, cursor, work_limit)
    if status[0] != OK:
        return status[0], False, np.empty(0, np.int64), node_count, event_count
    count = cursor[_SCAN_COUNT]
    if count <= 0:
        return OK, count == 0, np.empty(0, np.int64), node_count, event_count
    selected = np.array((cursor[_SCAN_FIRST], cursor[_SCAN_SECOND]), np.int64)[:count]
    return append_virtual_selection_native(columns, base, base_derived, tail, derived,
        templates, prototypes, event_node_ends, event_group_ends, node_count,
        group_count, event_count, selected, left, right, purpose, first_sequence, split_group)


def private_resource_columns(task):
    return (PrivateNodeColumns(*(getattr(task.nodes, name) for name in _NODE_FIELDS)),
            PrivateDerivedColumns(*(getattr(task, name) for name in _DERIVED_FIELDS)))


def prepare_private_bridge(workspace, program, left, right, *, max_nodes,
                           first_sequence, purpose=VirtualPurpose.EDGE_BRIDGE,
                           split_group=-1, chunk_size=64, allows_continue=None):
    """Return status, connectable, private rows; never extend a formal task.

    Empty rows with connectable=True mean direct; False means no bridge or a
    stopped attempt. Capacity failures leave every valid private length intact.
    """
    if (not isinstance(workspace, NumericCandidateWorkspace)
            or not isinstance(program, NumericRuleProgram)
            or program.task_fingerprint != workspace.task.fingerprint
            or type(chunk_size) is not int or not 1 <= chunk_size <= 256
            or type(max_nodes) is not int or not 0 <= max_nodes <= 2
            or type(first_sequence) is not int or first_sequence <= 0
            or not isinstance(purpose, VirtualPurpose)
            or type(split_group) is not int or split_group < -1
            or type(left) is not int or type(right) is not int):
        raise NumericValueError("private_bridge", "valid workspace, rules, anchors and options required")
    # Sequence arithmetic is checked before writing int64 columns.
    checked_sum((first_sequence, max_nodes), "private_bridge.sequence")
    task = workspace.task
    columns, rules = task_columns(task), rule_tables(program.rules)
    base, derived = private_resource_columns(task)
    cursor = np.zeros(6, dtype=np.int64)
    empty = np.empty(0, dtype=np.int64)
    while True:
        if allows_continue is not None and not allows_continue():
            return CANCELLED, False, empty
        status, connected, rows, nodes, events = private_bridge_step(columns, rules,
            base, derived, workspace.nodes, workspace.derived, workspace.templates,
            task.prototype_rows, workspace.event_node_ends, workspace.event_group_ends,
            workspace.node_count, workspace.group_count, workspace.event_count,
            left, right, max_nodes, first_sequence, _PURPOSES.index(purpose), split_group,
            cursor, chunk_size)
        if status != MORE_WORK:
            workspace.node_count, workspace.event_count = int(nodes), int(events)
            return int(status), bool(connected), rows


def _append_selected_virtuals(workspace, selected, left, right, purpose, first_sequence, split_group):
    task, count = workspace.task, selected.size
    capacity = workspace.capacity_status(changed_rows=workspace.changed_count, chains=workspace.chain_count,
        nodes=workspace.node_count + int(count), groups=workspace.group_count, events=workspace.event_count + 1)
    if capacity != OK:
        return capacity, False, np.empty(0, dtype=np.int64)
    columns = task_columns(task)
    base, derived = private_resource_columns(task)
    status, connected, rows, nodes, events = append_virtual_selection_native(columns,
        base, derived, workspace.nodes, workspace.derived, workspace.templates,
        task.prototype_rows, workspace.event_node_ends, workspace.event_group_ends,
        workspace.node_count, workspace.group_count, workspace.event_count, selected,
        left, right, _PURPOSES.index(purpose), first_sequence, split_group)
    workspace.node_count, workspace.event_count = int(nodes), int(events)
    return int(status), bool(connected), rows


@njit
def round_product_ratio(a, b, denominator, status):
    """Exact half-up of a*b/d, with 0 <= b <= d, even if a*b exceeds int64."""
    if a < 0 or b < 0 or denominator <= 0 or b > denominator:
        status[0] = INVALID
        return 0
    if b == 0:
        return 0
    if a <= MAX // b:
        product = a * b
        quotient, remainder = product // denominator, product % denominator
    else:
        # Long multiplication of the fractional remainder. Neither doubling
        # nor adding remainders overflows because each operation reduces first.
        whole, fraction = a // denominator, a % denominator
        quotient, remainder = 0, 0
        bit = 1
        while bit <= b // 2:
            bit *= 2
        while bit:
            quotient *= 2
            if remainder >= denominator - remainder:
                remainder -= denominator - remainder
                quotient += 1
            else:
                remainder += remainder
            if b & bit:
                if remainder >= denominator - fraction:
                    remainder -= denominator - fraction
                    quotient += 1
                else:
                    remainder += fraction
            bit //= 2
        quotient += whole * b
    return _add(quotient, int(remainder >= denominator - remainder), status)


@njit
def split_piece_count(weight, duration, minimum, maximum, maximum_separators):
    if weight <= 0 or duration <= 0 or minimum <= 0 or maximum < minimum or maximum_separators < 0:
        return INVALID, 0
    count = (weight - 1) // maximum + 1
    remainder = weight - maximum * (count - 1)
    if count < 2 or count - 1 > maximum_separators or remainder < minimum:
        return OK, 0
    return OK, count


@njit
def split_piece_arrays(weight, duration, minimum, maximum, maximum_separators):
    status = np.array((OK, -1, -1, -1), dtype=np.int64)
    status[0], count = split_piece_count(weight, duration, minimum, maximum, maximum_separators)
    if status[0] != OK or count == 0:
        empty = np.empty(0, dtype=np.int64)
        return status, empty, empty
    remainder = weight - maximum * (count - 1)
    weights, durations = np.empty(count, dtype=np.int64), np.empty(count, dtype=np.int64)
    cumulative, previous = 0, 0
    for i in range(count):
        piece = maximum if i < count - 1 else remainder
        cumulative += piece
        allocated = round_product_ratio(duration, cumulative, weight, status)
        weights[i], durations[i] = piece, allocated - previous
        previous = allocated
    return status, weights, durations


@njit
def append_private_split(base, derived, tail, tail_derived, templates, parent, offset,
                         weights, durations, group_index, sequence):
    selected = np.full(weights.size, parent, dtype=np.int64)
    copy_resource_templates(base, derived, tail, tail_derived, templates, selected, offset)
    for i in range(weights.size):
        row = offset + i
        tail.weight[row], tail.duration_ms[row] = weights[i], durations[i]
        tail.prototype[row] = tail.purpose[row] = -1
        tail.split_group[row] = group_index
        tail.piece_index[row], tail.piece_count[row] = i + 1, weights.size
        tail.accepted_sequence[row] = sequence


def prepare_private_split(workspace, parent, decision, origin_period, *, sequence):
    """Derive a previously authorized partition; do not choose an action or publish."""
    if not isinstance(workspace, NumericCandidateWorkspace) or not isinstance(decision, NumericSplitDecision):
        raise NumericValueError("private_split", "numeric workspace and split decision required")
    task = workspace.task
    if (type(parent) is not int or not 0 <= parent < task.nodes.weight.size
            or type(origin_period) is not int or not 0 <= origin_period < len(task.period_ids)
            or type(sequence) is not int or sequence <= 0):
        raise NumericValueError("private_split", "valid parent, origin and sequence required")
    int64(sequence, "private_split.sequence")
    empty = np.empty(0, dtype=np.int64)
    if not decision.eligible:
        return OK, False, empty
    if (int(task.nodes.split_group[parent]) >= 0 or task.nodes.source[parent] < 0
            or decision.target_period != int(task.nodes.source_period[parent])
            or decision.target_period < origin_period
            or decision.mode != int(decision.target_period != origin_period)
            or np.any(workspace.split_groups.parent_row[:workspace.group_count] == parent)):
        raise NumericValueError("private_split", "authorized unsplit original and target period required")
    status, count = split_piece_count(int(task.nodes.weight[parent]),
        int(task.nodes.duration_ms[parent]), decision.minimum_piece_weight,
        decision.maximum_piece_weight, decision.maximum_separator_node_count)
    count = int(count)
    if status != OK or count == 0:
        return int(status), False, empty
    capacity = workspace.capacity_status(changed_rows=workspace.changed_count, chains=workspace.chain_count,
        nodes=workspace.node_count + count, groups=workspace.group_count + 1, events=workspace.event_count + 1)
    if capacity != OK:
        return capacity, False, empty
    status, weights, durations = split_piece_arrays(int(task.nodes.weight[parent]),
        int(task.nodes.duration_ms[parent]), decision.minimum_piece_weight,
        decision.maximum_piece_weight, decision.maximum_separator_node_count)
    if status[0] != OK:
        return int(status[0]), False, empty
    first, local_group = workspace.node_count, workspace.group_count
    group_index = task.split_groups.parent_row.size + local_group
    base = PrivateNodeColumns(*(getattr(task.nodes, name) for name in _NODE_FIELDS))
    derived = PrivateDerivedColumns(*(getattr(task, name) for name in _DERIVED_FIELDS))
    append_private_split(base, derived, workspace.nodes, workspace.derived, workspace.templates,
                         parent, first, weights, durations, group_index, sequence)
    group_values = (parent, int(task.nodes.source[parent]), int(task.nodes.resource[parent]),
        int(task.nodes.weight[parent]), int(task.nodes.duration_ms[parent]),
        int(task.nodes.source_period[parent]), origin_period, decision.target_period,
        decision.mode, decision.rule_index, sequence)
    for column, value in zip(workspace.split_groups, group_values):
        column[local_group] = value
    workspace.node_count += count
    workspace.group_count += 1
    workspace.event_node_ends[workspace.event_count] = workspace.node_count
    workspace.event_group_ends[workspace.event_count] = workspace.group_count
    workspace.event_count += 1
    return OK, True, np.arange(task.nodes.weight.size + first,
                              task.nodes.weight.size + workspace.node_count, dtype=np.int64)


@njit
def scan_private_separator(t, rules, tail, derived, private_count, prototypes,
                           left_row, right_row, cursor, work_limit):
    status = np.array((OK, -1, -1, -1), dtype=np.int64)
    left = private_edge_node(t, tail, private_count, left_row, status)
    right = private_edge_node(t, tail, private_count, right_row, status)
    if status[0] != OK:
        return status
    local_rows = np.arange(3, dtype=np.int64)
    stop = min(prototypes.size, cursor[_SEP_NEXT] + work_limit)
    while cursor[_SEP_NEXT] < stop:
        prototype = cursor[_SEP_NEXT]
        cursor[_SEP_NEXT] += 1
        middle = _virtual_edge_values(t, prototypes[prototype], left, right)
        allowed = (all_edges_allowed_values(left, middle, rules, status)
                   and all_edges_allowed_values(middle, right, rules, status))
        if status[0] != OK:
            return status
        if not allowed:
            continue
        # The ranking uses the same full chain rules as before, on three rows.
        # Only this tiny subject is projected; no task extension/fingerprint.
        local = gather_local_task_columns(t, tail, derived,
            np.array((left_row, prototypes[prototype], right_row), dtype=np.int64))
        local.minimum[1], local.maximum[1] = middle.minimum, middle.maximum
        local.present[1, _MIN_TEMPERATURE_PRESENT] = middle.has_minimum
        local.present[1, _MAX_TEMPERATURE_PRESENT] = middle.has_maximum
        result = evaluate_chain_kernel(local, rules, local_rows)
        if result.status[0] != OK:
            return result.status
        count, severity = result.scores[0, 0], result.scores[0, 1]
        smooth = _add(_pair_smoothness(left, middle, t.scales, status),
                      _pair_smoothness(middle, right, t.scales, status), status)
        if status[0] != OK:
            return status
        if (cursor[_SEP_BEST] < 0 or count < cursor[_SEP_COUNT]
                or count == cursor[_SEP_COUNT] and severity < cursor[_SEP_SEVERITY]
                or count == cursor[_SEP_COUNT] and severity == cursor[_SEP_SEVERITY] and smooth < cursor[_SEP_SMOOTH]):
            cursor[_SEP_BEST], cursor[_SEP_COUNT] = prototype, count
            cursor[_SEP_SEVERITY], cursor[_SEP_SMOOTH] = severity, smooth
    if cursor[_SEP_NEXT] == prototypes.size:
        cursor[_SEP_DONE] = 1
    else:
        status[0] = MORE_WORK
    return status


def prepare_private_separator(workspace, program, left, right, *, sequence, group_index,
                              chunk_size=64, allows_continue=None):
    if (not isinstance(workspace, NumericCandidateWorkspace) or not isinstance(program, NumericRuleProgram)
            or program.task_fingerprint != workspace.task.fingerprint
            or type(left) is not int or type(right) is not int
            or type(sequence) is not int or sequence <= 0
            or type(group_index) is not int or not 0 <= group_index < workspace.task.split_groups.parent_row.size + workspace.group_count
            or type(chunk_size) is not int or not 1 <= chunk_size <= 256):
        raise NumericValueError("private_separator", "matching workspace, rules and valid separator inputs required")
    int64(sequence, "private_separator.sequence")
    task = workspace.task
    columns, rules = task_columns(task), rule_tables(program.rules)
    cursor = np.zeros(6, dtype=np.int64)
    cursor[_SEP_BEST] = -1
    empty = np.empty(0, dtype=np.int64)
    while True:
        if allows_continue is not None and not allows_continue():
            return CANCELLED, False, empty
        status = scan_private_separator(columns, rules, workspace.nodes, workspace.derived,
            workspace.node_count, task.prototype_rows, left, right, cursor, chunk_size)
        if status[0] != MORE_WORK:
            break
    if status[0] != OK or cursor[_SEP_BEST] < 0:
        return int(status[0]), False, empty
    return _append_selected_virtuals(workspace, np.array((cursor[_SEP_BEST],), dtype=np.int64),
        left, right, VirtualPurpose.SPLIT_SEPARATOR, sequence, group_index)


def materialize_private_resources(workspace, program, quality):
    """Acceptance boundary: replay original extension events, never cache failures."""
    if (not isinstance(workspace, NumericCandidateWorkspace)
            or not isinstance(program, NumericRuleProgram)
            or not isinstance(quality, NumericQualityProgram)
            or program.task_fingerprint != workspace.task.fingerprint
            or quality.task_fingerprint != workspace.task.fingerprint
            or quality.rule_program_fingerprint != program.fingerprint):
        raise NumericValueError("publish.resources", "matching private resource context required")
    if workspace.capacity_status(changed_rows=workspace.changed_count, chains=workspace.chain_count,
            nodes=workspace.node_count, groups=workspace.group_count, events=workspace.event_count) != OK:
        raise NumericValueError("publish.resources", "private resource lengths exceed capacity")
    node_start = group_start = 0
    for event in range(workspace.event_count):
        node_end, group_end = int(workspace.event_node_ends[event]), int(workspace.event_group_ends[event])
        if (not node_start < node_end <= workspace.node_count
                or not group_start <= group_end <= min(group_start + 1, workspace.group_count)):
            raise NumericValueError("publish.resources", "ordered nonempty extension events required")
        node_start, group_start = node_end, group_end
    if (node_start, group_start) != (workspace.node_count, workspace.group_count):
        raise NumericValueError("publish.resources", "extension events must cover all private resources")
    task, node_start, group_start = workspace.task, 0, 0
    columns = workspace.nodes
    for event in range(workspace.event_count):
        node_end, group_end = int(workspace.event_node_ends[event]), int(workspace.event_group_ends[event])
        group = None if group_end == group_start else NumericSplitGroup(
            *(int(column[group_start]) for column in workspace.split_groups))
        nodes = []
        for row in range(node_start, node_end):
            sequence, piece = int(columns.accepted_sequence[row]), int(columns.piece_index[row])
            identity = (f"virtual-{sequence:06d}" if columns.role[row] == _GENERATED_VIRTUAL
                        else f"split-{sequence:06d}:piece:{piece:04d}")
            nodes.append(NumericDynamicNode(identity, int(workspace.templates[row]),
                int(columns.weight[row]), int(columns.duration_ms[row]), int(columns.role[row]),
                int(columns.source[row]), int(columns.resource[row]), int(columns.source_period[row]),
                int(columns.prototype[row]), int(columns.purpose[row]), int(columns.split_group[row]),
                piece, int(columns.piece_count[row]), sequence,
                int(columns.min_temperature[row]), int(columns.max_temperature[row]),
                bool(columns.present[row, _MIN_TEMPERATURE_PRESENT]), bool(columns.present[row, _MAX_TEMPERATURE_PRESENT])))
        # No LRU candidate-task insertion before atomic state publication.
        task = extend_numeric_task(task, tuple(nodes), split_group=group)
        program = program.rebind(task)
        quality = quality.rebind(task, program)
        node_start, group_start = node_end, group_end
    start = workspace.task.nodes.weight.size
    for name in _NODE_FIELDS:
        if not np.array_equal(getattr(task.nodes, name)[start:], getattr(columns, name)[:workspace.node_count]):
            raise NumericValueError("publish.resources", f"private and formal node fields differ: {name}")
    for name in _DERIVED_FIELDS:
        if not np.array_equal(getattr(task, name)[:, start:], getattr(workspace.derived, name)[:, :workspace.node_count]):
            raise NumericValueError("publish.resources", f"private and formal derived fields differ: {name}")
    return NumericResourceExtension(task, program, quality, tuple(range(start, task.nodes.weight.size)))
