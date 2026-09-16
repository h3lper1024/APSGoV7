"""Candidate-private numeric rows for virtual material and controlled splits."""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numba import njit, literal_unroll

from ._numeric_kernel import (
    EdgeNode, edge_node, private_edge_node, all_edges_allowed_values,
    task_columns, rule_tables, _add, _sub, _abs, _mul,
)

from ._numeric_evaluation import NumericQualityProgram
from ._numeric_rules import (
    NumericRuleKind,
    NumericRuleProgram,
    numeric_rows_prohibited_profile,
    numeric_edge_allowed,
)
from ._numeric_state import (
    NumericDynamicNode,
    NumericSplitGroup,
    NumericTask,
    NumericCandidateWorkspace, PrivateNodeColumns, PrivateDerivedColumns,
    OK, INVALID, NUMERIC_ERROR, CANCELLED, CAPACITY, MORE_WORK,
    extend_numeric_task,
)
from ._numeric_units import NumericValueError, checked_product, checked_sum
from .contracts import RuleScope
from .model import MaterialRole, VirtualPurpose

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)
_PURPOSES = tuple(VirtualPurpose)
_MIN_TEMPERATURE_PRESENT = 2
_MAX_TEMPERATURE_PRESENT = len(("width", "thickness", "min_temperature"))
_NODE_FIELDS = PrivateNodeColumns._fields
_DERIVED_FIELDS = PrivateDerivedColumns._fields
_SCAN_PHASE, _SCAN_NEXT, _SCAN_FIRST, _SCAN_SECOND, _SCAN_SCORE, _SCAN_COUNT = range(6)
_SCAN_START, _SCAN_SINGLE, _SCAN_DOUBLE, _SCAN_DONE = range(4)


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
def append_private_virtuals(base, derived, tail, tail_derived, templates, selected,
                            prototypes, offset, left, right, purpose, first_sequence, group):
    """Write only selected templates, in one original resource extension event."""
    minimum = (min(left.minimum, right.minimum) if left.has_minimum and right.has_minimum
               else left.minimum if left.has_minimum else right.minimum if right.has_minimum else 0)
    maximum = (max(left.maximum, right.maximum) if left.has_maximum and right.has_maximum
               else left.maximum if left.has_maximum else right.maximum if right.has_maximum else 0)
    for k in range(selected.size):
        row, prototype = offset + k, selected[k]
        template = prototypes[prototype]
        for name in literal_unroll(_NODE_FIELDS):
            getattr(tail, name)[row] = getattr(base, name)[template]
        for name in literal_unroll(_DERIVED_FIELDS):
            getattr(tail_derived, name)[:, row] = getattr(derived, name)[:, template]
        templates[row] = template
        tail.role[row] = _GENERATED_VIRTUAL
        tail.source[row] = tail.resource[row] = tail.source_period[row] = -1
        tail.prototype[row], tail.purpose[row], tail.split_group[row] = prototype, purpose, group
        tail.piece_index[row], tail.piece_count[row] = -1, 0
        tail.accepted_sequence[row] = first_sequence + k
        tail.min_temperature[row], tail.max_temperature[row] = minimum, maximum
        tail.present[row, _MIN_TEMPERATURE_PRESENT] = left.has_minimum or right.has_minimum
        tail.present[row, _MAX_TEMPERATURE_PRESENT] = left.has_maximum or right.has_maximum


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
    cursor = np.zeros(6, dtype=np.int64)
    empty = np.empty(0, dtype=np.int64)
    while True:
        if allows_continue is not None and not allows_continue():
            return CANCELLED, False, empty
        status = scan_private_bridge(columns, rules, workspace.nodes, workspace.node_count,
            task.prototype_rows, left, right, max_nodes, cursor, chunk_size)
        if status[0] != MORE_WORK:
            break
    if status[0] != OK:
        return int(status[0]), False, empty
    count = int(cursor[_SCAN_COUNT])
    if count <= 0:
        return OK, count == 0, empty
    capacity = workspace.capacity_status(changed_rows=workspace.changed_count, chains=workspace.chain_count,
        nodes=workspace.node_count + count, groups=workspace.group_count, events=workspace.event_count + 1)
    if capacity != OK:
        return capacity, False, empty
    selected = np.array((cursor[_SCAN_FIRST], cursor[_SCAN_SECOND]), dtype=np.int64)[:count]
    first = workspace.node_count
    base = PrivateNodeColumns(*(getattr(task.nodes, name) for name in _NODE_FIELDS))
    derived = PrivateDerivedColumns(*(getattr(task, name) for name in _DERIVED_FIELDS))
    left_values = private_edge_node(columns, workspace.nodes, first, left, status)
    right_values = private_edge_node(columns, workspace.nodes, first, right, status)
    append_private_virtuals(base, derived, workspace.nodes, workspace.derived, workspace.templates,
        selected, task.prototype_rows, first, left_values, right_values,
        _PURPOSES.index(purpose), first_sequence, split_group)
    workspace.node_count += count
    workspace.event_node_ends[workspace.event_count] = workspace.node_count
    workspace.event_group_ends[workspace.event_count] = workspace.group_count
    workspace.event_count += 1
    return OK, True, np.arange(task.nodes.weight.size + first,
                              task.nodes.weight.size + workspace.node_count, dtype=np.int64)


def _temperature(task, rows, column):
    present_column = (
        _MIN_TEMPERATURE_PRESENT if column == "min_temperature" else _MAX_TEMPERATURE_PRESENT
    )
    values = [
        int(getattr(task.nodes, column)[row])
        for row in rows
        if bool(task.nodes.present[row, present_column])
    ]
    return (min(values) if column == "min_temperature" else max(values), bool(values))


def virtual_node(
    task,
    prototype_index,
    left_anchor,
    right_anchor,
    *,
    purpose,
    sequence,
    node_id=None,
    split_group=-1,
):
    if (
        not isinstance(task, NumericTask)
        or type(prototype_index) is not int
        or not 0 <= prototype_index < len(task.prototype_ids)
        or type(left_anchor) is not int
        or type(right_anchor) is not int
        or not 0 <= left_anchor < task.nodes.weight.size
        or not 0 <= right_anchor < task.nodes.weight.size
        or not isinstance(purpose, VirtualPurpose)
        or type(sequence) is not int
        or sequence <= 0
        or type(split_group) is not int
    ):
        raise NumericValueError(
            "virtual", "valid task, prototype, anchors, purpose and sequence required"
        )
    template = int(task.prototype_rows[prototype_index])
    minimum, has_minimum = _temperature(task, (left_anchor, right_anchor), "min_temperature")
    maximum, has_maximum = _temperature(task, (left_anchor, right_anchor), "max_temperature")
    return NumericDynamicNode(
        node_id or f"virtual-{sequence:06d}",
        template,
        int(task.nodes.weight[template]),
        int(task.nodes.duration_ms[template]),
        _GENERATED_VIRTUAL,
        -1,
        -1,
        -1,
        prototype_index,
        _PURPOSES.index(purpose),
        split_group,
        -1,
        0,
        sequence,
        minimum,
        maximum,
        has_minimum,
        has_maximum,
    )


def split_piece_node(
    task,
    parent_row,
    *,
    group_index,
    piece_index,
    piece_count,
    weight,
    duration_ms,
    accepted_sequence,
):
    if not isinstance(task, NumericTask) or not 0 <= parent_row < task.nodes.weight.size:
        raise NumericValueError("split_piece", "parent row is outside the numeric task")
    return NumericDynamicNode(
        f"split-{accepted_sequence:06d}:piece:{piece_index:04d}",
        parent_row,
        weight,
        duration_ms,
        int(task.nodes.role[parent_row]),
        int(task.nodes.source[parent_row]),
        int(task.nodes.resource[parent_row]),
        int(task.nodes.source_period[parent_row]),
        -1,
        -1,
        group_index,
        piece_index,
        piece_count,
        accepted_sequence,
        int(task.nodes.min_temperature[parent_row]),
        int(task.nodes.max_temperature[parent_row]),
        bool(task.nodes.present[parent_row, _MIN_TEMPERATURE_PRESENT]),
        bool(task.nodes.present[parent_row, _MAX_TEMPERATURE_PRESENT]),
    )


@lru_cache(maxsize=128)
def extend_resource_workspace(task, program, quality, nodes, *, split_group=None):
    candidate = extend_numeric_task(task, nodes, split_group=split_group)
    candidate_program = program.rebind(candidate)
    candidate_quality = quality.rebind(candidate, candidate_program)
    first = task.nodes.weight.size
    return NumericResourceExtension(
        candidate,
        candidate_program,
        candidate_quality,
        tuple(range(first, candidate.nodes.weight.size)),
    )


def _allowed(task, program, left, right):
    return numeric_edge_allowed(task, program, left, right)


def _smoothness(task, rows):
    common = max(task.units.width, task.units.thickness)
    if common % task.units.width or common % task.units.thickness:
        raise NumericValueError(
            "virtual_smoothness", "physical scales must share a decimal multiple"
        )
    total = 0
    for left, middle, right in zip(rows, rows[1:], rows[2:]):
        width = abs(int(task.nodes.width[left]) - int(task.nodes.width[middle])) + abs(
            int(task.nodes.width[middle]) - int(task.nodes.width[right])
        )
        thickness = abs(int(task.nodes.thickness[left]) - int(task.nodes.thickness[middle])) + abs(
            int(task.nodes.thickness[middle]) - int(task.nodes.thickness[right])
        )
        total = checked_sum(
            (
                total,
                checked_product(width, common // task.units.width, "virtual_smoothness"),
                checked_product(
                    checked_product(
                        thickness, common // task.units.thickness, "virtual_smoothness"
                    ),
                    100,
                    "virtual_smoothness",
                ),
            ),
            "virtual_smoothness",
        )
    return total


def _static_bridge_supported(program):
    edge_kinds = {
        NumericRuleKind.SYNTHETIC_WIDTH,
        NumericRuleKind.SOFT_HARD,
        NumericRuleKind.TEMPERATURE,
        NumericRuleKind.THICKNESS,
        NumericRuleKind.WIDTH,
    }
    for rule in program.rules:
        if rule.scope is not RuleScope.EDGE or rule.kind not in edge_kinds:
            continue
        if rule.kind is NumericRuleKind.TEMPERATURE and not (rule.flags[0] or rule.flags[1]):
            return False
        if rule.kind is NumericRuleKind.THICKNESS and any(band.relative for band in rule.bands):
            return False
    return True


def _prototype_edge_mask(task, program, left_rows, right_rows):
    """The prototype scan uses the same native edge formulas as full evaluation."""
    from ._numeric_kernel import task_columns, rule_tables, edge_matrix_kernel, OK
    allowed, status = edge_matrix_kernel(task_columns(task), rule_tables(program.rules),
        np.asarray(left_rows, dtype=np.int64), np.asarray(right_rows, dtype=np.int64))
    if status[0] != OK:
        raise NumericValueError(f"rules[{int(status[1])}]", "integer is outside signed int64")
    return allowed


def _choose_static_bridge(task, program, left, right, max_nodes):
    prototypes = tuple(int(row) for row in task.prototype_rows)
    from_left = _prototype_edge_mask(task, program, (left,), prototypes)[0]
    to_right = _prototype_edge_mask(task, program, prototypes, (right,))[:, 0]
    best = None
    best_score = None
    for prototype, row in enumerate(prototypes):
        if from_left[prototype] and to_right[prototype]:
            score = _smoothness(task, (left, row, right))
            if best_score is None or score < best_score:
                best, best_score = (prototype,), score
    if best is not None or max_nodes < 2:
        return best
    between = _prototype_edge_mask(task, program, prototypes, prototypes)
    for first_prototype, first_row in enumerate(prototypes):
        if not from_left[first_prototype]:
            continue
        for second_prototype, second_row in enumerate(prototypes):
            if between[first_prototype, second_prototype] and to_right[second_prototype]:
                score = _smoothness(task, (left, first_row, second_row, right))
                if best_score is None or score < best_score:
                    best, best_score = (first_prototype, second_prototype), score
    return best


def choose_virtual_bridge(
    task,
    program,
    quality,
    left,
    right,
    *,
    max_nodes,
    first_sequence,
):
    """Preserve direct, ordered single, then ordered best double bridge semantics."""
    if _allowed(task, program, left, right):
        return None
    if type(max_nodes) is not int or not 0 <= max_nodes <= 2:
        raise NumericValueError("max_nodes", "zero, one or two bridge rows required")
    if not max_nodes or not task.prototype_ids:
        return None
    if _static_bridge_supported(program):
        best = _choose_static_bridge(task, program, left, right, max_nodes) or ()
    else:
        count = len(task.prototype_ids)
        temporary = tuple(
            virtual_node(
                task,
                prototype,
                left,
                right,
                purpose=VirtualPurpose.EDGE_BRIDGE,
                sequence=first_sequence + copy,
                node_id=f"candidate-virtual:{first_sequence}:{copy}:{prototype}",
            )
            for copy in range(2 if max_nodes > 1 else 1)
            for prototype in range(count)
        )
        workspace = extend_resource_workspace(task, program, quality, temporary)
        first_rows = workspace.rows[:count]
        best = None
        best_score = None
        for prototype, row in enumerate(first_rows):
            if _allowed(workspace.task, workspace.program, left, row) and _allowed(
                workspace.task, workspace.program, row, right
            ):
                score = _smoothness(workspace.task, (left, row, right))
                if best_score is None or score < best_score:
                    best, best_score = (prototype,), score
        if best is None and max_nodes > 1:
            second_rows = workspace.rows[count:]
            for first_prototype, first_row in enumerate(first_rows):
                if not _allowed(workspace.task, workspace.program, left, first_row):
                    continue
                for second_prototype, second_row in enumerate(second_rows):
                    if not _allowed(workspace.task, workspace.program, first_row, second_row):
                        continue
                    if not _allowed(workspace.task, workspace.program, second_row, right):
                        continue
                    score = _smoothness(
                        workspace.task, (left, first_row, second_row, right)
                    )
                    if best_score is None or score < best_score:
                        best, best_score = (first_prototype, second_prototype), score
        best = () if best is None else best
    if not best:
        return None
    selected = tuple(
        virtual_node(
            task,
            prototype,
            left,
            right,
            purpose=VirtualPurpose.EDGE_BRIDGE,
            sequence=first_sequence + offset,
        )
        for offset, prototype in enumerate(best)
    )
    return extend_resource_workspace(task, program, quality, selected)


def choose_split_separator(task, program, quality, left, right, *, sequence, group_index):
    best = None
    best_score = None
    for prototype in range(len(task.prototype_ids)):
        node = virtual_node(
            task,
            prototype,
            left,
            right,
            purpose=VirtualPurpose.SPLIT_SEPARATOR,
            sequence=sequence,
            node_id=f"candidate-separator:{sequence}:{prototype}",
            split_group=group_index,
        )
        candidate = extend_resource_workspace(task, program, quality, (node,))
        row = candidate.rows[0]
        if not _allowed(candidate.task, candidate.program, left, row) or not _allowed(
            candidate.task, candidate.program, row, right
        ):
            continue
        profile = numeric_rows_prohibited_profile(
            candidate.task, candidate.program, (left, row, right)
        )
        score = (
            *profile,
            _smoothness(candidate.task, (left, row, right)),
        )
        if best_score is None or score < best_score:
            best, best_score = prototype, score
    if best is None:
        return None
    node = virtual_node(
        task,
        best,
        left,
        right,
        purpose=VirtualPurpose.SPLIT_SEPARATOR,
        sequence=sequence,
        split_group=group_index,
    )
    return extend_resource_workspace(task, program, quality, (node,))


def split_group(task, parent_row, decision, origin_period, accepted_sequence):
    return NumericSplitGroup(
        parent_row,
        int(task.nodes.source[parent_row]),
        int(task.nodes.resource[parent_row]),
        int(task.nodes.weight[parent_row]),
        int(task.nodes.duration_ms[parent_row]),
        int(task.nodes.source_period[parent_row]),
        origin_period,
        decision.target_period,
        decision.mode,
        decision.rule_index,
        accepted_sequence,
    )
