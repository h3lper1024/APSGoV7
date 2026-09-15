"""Candidate-private numeric rows for virtual material and controlled splits."""

from dataclasses import dataclass

from ._numeric_evaluation import NumericQualityProgram
from ._numeric_rules import NumericRuleProgram, evaluate_numeric_edge, evaluate_numeric_rows
from ._numeric_state import (
    NumericDynamicNode,
    NumericSplitGroup,
    NumericTask,
    extend_numeric_task,
)
from ._numeric_units import NumericValueError, checked_product, checked_sum
from .model import MaterialRole, VirtualPurpose

_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)
_PURPOSES = tuple(VirtualPurpose)
_MIN_TEMPERATURE_PRESENT = 2
_MAX_TEMPERATURE_PRESENT = len(("width", "thickness", "min_temperature"))


@dataclass(frozen=True, slots=True)
class NumericResourceExtension:
    task: NumericTask
    program: NumericRuleProgram
    quality: NumericQualityProgram
    rows: tuple[int, ...]


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
    return not any(
        value.prohibited for value in evaluate_numeric_edge(task, program, left, right).violations
    )


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
                score = _smoothness(workspace.task, (left, first_row, second_row, right))
                if best_score is None or score < best_score:
                    best, best_score = (first_prototype, second_prototype), score
    if best is None:
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
        result = evaluate_numeric_rows(candidate.task, candidate.program, (left, row, right))
        prohibited = tuple(value for value in result.violations if value.prohibited)
        score = (
            len(prohibited),
            checked_sum((value.severity for value in prohibited), "separator_severity"),
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
