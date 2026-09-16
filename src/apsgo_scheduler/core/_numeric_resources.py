"""Candidate-private numeric rows for virtual material and controlled splits."""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from ._numeric_evaluation import NumericQualityProgram
from ._numeric_rules import (
    NumericRuleKind,
    NumericRuleProgram,
    evaluate_numeric_rows,
    numeric_edge_allowed,
)
from ._numeric_state import (
    NumericDynamicNode,
    NumericSplitGroup,
    NumericTask,
    extend_numeric_task,
)
from ._numeric_units import NumericValueError, checked_product, checked_sum
from .contracts import RuleScope
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
    """Evaluate edges containing a static generated-virtual prototype in bulk."""
    nodes = task.nodes
    left = np.asarray(left_rows, dtype=np.int64)[:, None]
    right = np.asarray(right_rows, dtype=np.int64)[None, :]
    allowed = np.ones((left.size, right.size), dtype=np.bool_)
    for rule in program.rules:
        if rule.scope is not RuleScope.EDGE:
            continue
        if rule.kind is NumericRuleKind.SYNTHETIC_WIDTH:
            allowed &= nodes.present[left, 0] & nodes.present[right, 0]
            allowed &= np.maximum(0, nodes.width[right] - nodes.width[left]) <= rule.values[0]
        elif rule.kind is NumericRuleKind.SOFT_HARD:
            if not rule.flags[0]:
                allowed.fill(False)
        elif rule.kind is NumericRuleKind.TEMPERATURE:
            continue
        elif rule.kind is NumericRuleKind.THICKNESS:
            present = nodes.present[left, 1] & nodes.present[right, 1]
            first, second = nodes.thickness[left], nodes.thickness[right]
            basis = np.maximum(first, second) if rule.values[0] else np.minimum(first, second)
            numerator = np.full(basis.shape, rule.values[1], dtype=np.int64)
            denominator = np.full(basis.shape, rule.values[2], dtype=np.int64)
            unmatched = np.ones(basis.shape, dtype=np.bool_)
            for band in rule.bands:
                matches = unmatched.copy()
                if band.has_minimum:
                    matches &= basis >= band.minimum if band.include_minimum else basis > band.minimum
                if band.has_maximum:
                    matches &= basis <= band.maximum if band.include_maximum else basis < band.maximum
                numerator[matches] = band.tolerance_numerator
                denominator[matches] = band.tolerance_denominator
                unmatched[matches] = False
            difference = np.abs(first - second)
            allowed &= ~present | (difference * denominator <= numerator)
        elif rule.kind is NumericRuleKind.WIDTH:
            present = nodes.present[left, 0] & nodes.present[right, 0]
            difference = np.abs(nodes.width[right] - nodes.width[left])
            allowed &= present & (difference <= rule.values[1])
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
