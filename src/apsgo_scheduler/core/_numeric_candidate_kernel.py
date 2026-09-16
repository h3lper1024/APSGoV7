"""Shared candidate computation on borrowed chains and private numeric resources.

Stage control owns enumeration, quota and acceptance. This module has no reverse
dependency on search/refinement and never constructs a formal task or plan.
"""

from dataclasses import dataclass, replace
from collections import namedtuple

import numpy as np
from numba import njit, prange

from ._numeric_chain_ops import chain_rows, write_parts, reorder_chains, replace_span
from ._numeric_evaluation import evaluate_numeric_view, NumericEvaluationContext, _check_kernel_status
from ._numeric_kernel import (
    task_columns, private_task_columns, rule_tables, column_value, _add,
    all_edges_allowed_values, private_edge_node,
    allocate_result, evaluate_view_kernel, KernelResult,
)
from ._numeric_resources import (
    prepare_private_split, prepare_private_separator,
    private_resource_columns, private_bridge_step, append_virtual_selection_native,
)
from ._numeric_rules import NumericRuleKind, NumericSplitDecision
from ._numeric_state import (
    NumericCandidateDescriptors, NumericCandidateWorkspace, NumericSearchAction,
    PrivateNodeColumns, PrivateSplitColumns, NumericChainView,
    DESCRIPTOR_FIELDS, OK, INVALID, CANCELLED, CAPACITY, MORE_WORK, readonly,
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
_FILL_PURPOSE = tuple(VirtualPurpose).index(VirtualPurpose.WEIGHT_FILL)
(_ERROR_NONE, _ERROR_BRIDGE_SEQUENCE, _ERROR_FILL_SEQUENCE,
 _ERROR_ACTIVE_CHAINS, _ERROR_TIMING) = range(5)


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
    native_state: object = None


@dataclass(frozen=True, slots=True)
class NumericDeferredCandidateFailure:
    view: object
    error: NumericValueError


def capture_candidate_result(workspace, program, quality, descriptors, index, policy, **options):
    """Speculative errors are inert until their original ordered consumption."""
    try:
        return compute_candidate_attempt(workspace, program, quality, descriptors, index, policy, **options)
    except NumericValueError as error:
        return NumericDeferredCandidateFailure(workspace.view(), error)


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




def _append_rows(workspace, rows):
    parts = np.array(((-1, 0, rows.size, 0),), dtype=np.int64)
    status, end = write_parts(workspace.view(), parts, rows, workspace.changed_count)
    if status == OK:
        workspace.changed_count = int(end)
    return int(status)


_RP_PART, _RP_GROUP, _RP_START, _RP_SEQUENCE, _RP_CHANGED, _RP_NODES, _RP_EVENTS, _RP_ERROR = range(8)


@njit
def repair_parts_step(view, base_view, parts, indices, columns, rules, base,
        base_derived, tail, derived, templates, prototypes, event_node_ends,
        event_group_ends, group_count, maximum_bridge_nodes, progress, cursor,
        work_limit):
    """One splice or bounded bridge scan. All writes stay in the caller's slot."""
    p, group, start, sequence, used, nodes, events = progress[:7]
    if p == parts.shape[0]:
        if group >= 0:
            target = indices[group]
            view.starts[target], view.stops[target], view.private[target] = start, used, True
        return OK, True
    part = parts[p]
    next_group = part[GROUP]
    if next_group != group:
        if group >= 0:
            target = indices[group]
            view.starts[target], view.stops[target], view.private[target] = start, used, True
        start, group = used, next_group
        progress[_RP_START], progress[_RP_GROUP] = start, group
    rows = chain_rows(base_view, part[CHAIN])[part[FIRST]:part[LAST]]
    if part[REVERSE]:
        rows = rows[::-1]
    if rows.size:
        if used > start:
            check = np.array((OK, -1, -1, -1), np.int64)
            first_sequence = _add(sequence, 1, check)
            _add(first_sequence, maximum_bridge_nodes, check)
            if check[0] != OK:
                progress[_RP_ERROR] = _ERROR_BRIDGE_SEQUENCE
                return check[0], False
            status, connected, bridge, nodes, events = private_bridge_step(
                columns, rules, base, base_derived, tail, derived, templates,
                prototypes, event_node_ends, event_group_ends, nodes, group_count,
                events, view.changed_rows[used - 1], rows[0], maximum_bridge_nodes,
                first_sequence, _BRIDGE, -1, cursor, work_limit)
            progress[_RP_NODES], progress[_RP_EVENTS] = nodes, events
            if status != OK or not connected:
                return status, False
            status, used = write_parts(view, np.array(((-1, 0, bridge.size, 0),), np.int64), bridge, used)
            if status != OK:
                return status, False
            sequence += bridge.size
            progress[_RP_CHANGED], progress[_RP_SEQUENCE] = used, sequence
        status, used = write_parts(view, np.array(((-1, 0, rows.size, 0),), np.int64), rows, used)
        if status != OK:
            return status, False
        progress[_RP_CHANGED] = used
    progress[_RP_PART] = p + 1
    cursor[:] = 0
    return MORE_WORK, False


def _repair_parts(workspace, program, base_view, parts, indices, policy, sequence, allows_continue):
    progress = np.array((0, -1, workspace.changed_count, sequence,
        workspace.changed_count, workspace.node_count, workspace.event_count, 0), np.int64)
    cursor = np.zeros(6, np.int64)
    base, derived = private_resource_columns(workspace.task)
    columns, rules = task_columns(workspace.task), rule_tables(program.rules)
    while True:
        if allows_continue is not None and not allows_continue():
            return CANCELLED, False, int(progress[_RP_SEQUENCE])
        status, found = repair_parts_step(workspace.view(), base_view, parts, indices,
            columns, rules, base, derived, workspace.nodes, workspace.derived,
            workspace.templates, workspace.task.prototype_rows, workspace.event_node_ends,
            workspace.event_group_ends, workspace.group_count, policy.maximum_bridge_nodes,
            progress, cursor, 64)
        workspace.changed_count = int(progress[_RP_CHANGED])
        workspace.node_count, workspace.event_count = int(progress[_RP_NODES]), int(progress[_RP_EVENTS])
        if progress[_RP_ERROR]:
            checked_sum((int(progress[_RP_SEQUENCE]) + 1, policy.maximum_bridge_nodes), "private_bridge.sequence")
        if status != MORE_WORK:
            return int(status), bool(found), int(progress[_RP_SEQUENCE])


def _base_view(workspace):
    # Editing the candidate metadata must not change later reads of source parts.
    return workspace.view()._replace(starts=workspace.starts.copy(), stops=workspace.stops.copy(),
        private=workspace.private.copy(), ids=workspace.ids.copy(), periods=workspace.periods.copy())


@njit
def _move_order_native(view, source, position):
    order = np.arange(view.count, dtype=np.int64)
    if source < position:
        order[source:position] = order[source + 1:position + 1]
    elif source > position:
        order[position + 1:source + 1] = order[position:source]
    order[position] = source
    return reorder_chains(view, order)


def _move_order(workspace, source, position):
    return int(_move_order_native(workspace.view(), source, position))




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


NativeCandidateInput = namedtuple("NativeCandidateInput", (
    "columns rules nodes derived groups prototypes base_view objectives reuse period_count has_timing"
))
NativeCandidateFrame = namedtuple("NativeCandidateFrame", (
    "chains nodes derived groups templates event_node_ends event_group_ends control "
    "parts indices removed affected progress cursor output"
))
NativeCandidatePolicy = namedtuple("NativeCandidatePolicy", "bridge maximum same_period record_rows reject")
(_NC_STATUS, _NC_PHASE, _NC_CHAINS, _NC_NODES, _NC_EVENTS, _NC_CHANGED,
 _NC_SEQUENCE, _NC_CLEANED, _NC_PREPARED, _NC_ADMISSIBLE, _NC_PARTS,
 _NC_INDICES, _NC_REMOVED, _NC_AFFECTED, _NC_ERROR, _NC_SOURCE, _NC_TARGET,
 _NC_EVALUATED, _NC_ENDS) = range(19)
_NC_BEGIN, _NC_REPAIR, _NC_FINALIZE, _NC_DONE = range(4)
_NC_EVALUATE = 4


def native_candidate_input(context):
    """Borrow immutable generation inputs; no candidate-specific task copy."""
    return NativeCandidateInput(context.columns, context.rules, context.base_nodes,
        context.base_derived, context.base_groups, context.task.prototype_rows,
        context.base_view, context.objectives, context.reuse, len(context.task.period_ids), context.task.start_ms is not None)


def native_candidate_policy(program, policy):
    reject = np.array([rule.kind in policy.reject_prohibited_kinds for rule in program.rules], np.bool_)
    reject.setflags(write=False)
    return NativeCandidatePolicy(policy.maximum_bridge_nodes, policy.maximum_changed_chain_weight,
        policy.same_period_order, policy.record_order_rows, reject)


def native_candidate_frame(workspace, data, sequence):
    """Only array/scalar tuples cross the compiled boundary; counts are call-local."""
    if workspace.native_frame is not None:
        frame = workspace.native_frame._replace(chains=workspace.view())
        frame.control[:] = 0
        frame.control[_NC_CHAINS], frame.control[_NC_SEQUENCE] = workspace.chain_count, sequence
        frame.progress[:], frame.cursor[:] = 0, 0
        workspace.native_frame = frame
        return frame
    control = np.zeros(19, np.int64)
    control[_NC_CHAINS], control[_NC_SEQUENCE] = workspace.chain_count, sequence
    result = NativeCandidateFrame(workspace.view(), workspace.nodes, workspace.derived,
        workspace.split_groups, workspace.templates, workspace.event_node_ends,
        workspace.event_group_ends, control, np.empty((6, 5), np.int64),
        np.empty(2, np.int64), np.empty(workspace.plan.node_rows.size, np.int64),
        np.empty(2 * workspace.plan.node_rows.size + workspace.templates.size, np.int64),
        np.zeros(8, np.int64), np.zeros(6, np.int64),
        allocate_result(workspace.ids.size, data.rules.meta.shape[0],
            workspace.plan.node_rows.size + workspace.templates.size, data.columns.original_weight.size))
    workspace.native_frame = result
    return result


@njit
def _frame_view(frame, active=False):
    v = frame.chains
    changed = v.changed_rows[:frame.control[_NC_CHANGED]] if active else v.changed_rows
    return NumericChainView(v.base_rows, changed, v.starts, v.stops, v.private,
        v.ids, v.periods, frame.control[_NC_CHAINS], v.epoch)


@njit
def _frame_edge(data, frame, left, right):
    status = np.array((OK, -1, -1, -1), np.int64)
    a = private_edge_node(data.columns, frame.nodes, frame.control[_NC_NODES], left, status)
    b = private_edge_node(data.columns, frame.nodes, frame.control[_NC_NODES], right, status)
    allowed = all_edges_allowed_values(a, b, data.rules, status)
    return status[0], allowed


@njit
def _frame_affected(frame, rows):
    if rows.size > frame.affected.size:
        return CAPACITY
    frame.affected[:rows.size] = rows
    frame.control[_NC_AFFECTED] = rows.size
    return OK


@njit
def _native_local_resource(data, f, d, source):
    c, view = f.control, _frame_view(f)
    rows = chain_rows(view, source)
    empty = rows[:0]
    if d[ACTION] == FILL:
        position, prototype = d[POSITION], d[NODE]
        if not 0 <= position <= rows.size or not 0 <= prototype < data.prototypes.size:
            return INVALID, False
        left, right = rows[max(0, position - 1)], rows[min(rows.size - 1, position)]
        status = np.array((OK, -1, -1, -1), np.int64)
        sequence = _add(c[_NC_SEQUENCE], 1, status)
        if status[0] != OK:
            c[_NC_ERROR] = _ERROR_FILL_SEQUENCE
            return status[0], False
        code, found, added, nodes, events = append_virtual_selection_native(
            data.columns, data.nodes, data.derived, f.nodes, f.derived, f.templates,
            data.prototypes, f.event_node_ends, f.event_group_ends, c[_NC_NODES], 0,
            c[_NC_EVENTS], np.array((prototype,), np.int64), left, right,
            _FILL_PURPOSE, sequence, -1)
        c[_NC_NODES], c[_NC_EVENTS] = nodes, events
        if code != OK or not found:
            return code, False
        if position:
            code, found = _frame_edge(data, f, left, added[0])
            if code != OK or not found:
                return code, False
        if position < rows.size:
            code, found = _frame_edge(data, f, added[0], right)
            if code != OK or not found:
                return code, False
        code, end = replace_span(view, source, position, position, added, c[_NC_CHANGED])
        if code == OK:
            c[_NC_CHANGED] = end
        c[_NC_SEQUENCE] = sequence
        if code == OK:
            code = _frame_affected(f, added)
        return code, code == OK
    start, stop = d[START], d[STOP]
    if not 0 <= start < stop <= rows.size:
        return INVALID, False
    removed = rows[start:stop]
    if not _ordinary_run(data.nodes, removed):
        return OK, False
    if start and stop < rows.size:
        code, found = _frame_edge(data, f, rows[start - 1], rows[stop])
        if code != OK or not found:
            return code, False
    code, end = replace_span(view, source, start, stop, empty, c[_NC_CHANGED])
    if code == OK:
        c[_NC_CHANGED] = end
        code = _frame_affected(f, removed)
    return code, code == OK


@njit
def _native_candidate_begin(data, f, d, policy):
    c, base, action = f.control, data.base_view, d[ACTION]
    source, target = _chain_index(base, d[SOURCE]), _chain_index(base, d[TARGET])
    c[_NC_SOURCE], c[_NC_TARGET] = source, target
    if source < 0 or not _valid_descriptor(d) or action == SPLIT:
        return INVALID, False
    if action == CUT:
        code, found = _cut_map(_frame_view(f), data.columns, source, d[START], d[TARGET], d[OTHER_START], d[OTHER_STOP])
        if code == OK and found:
            c[_NC_CHAINS] += 1
            code = _frame_affected(f, chain_rows(base, source))
        return code, found
    if action == ORDER:
        position = d[POSITION]
        if target < 0 or not 0 <= position < c[_NC_CHAINS] or base.ids[position] != d[TARGET]:
            return INVALID, False
        if policy.same_period and base.periods[source] != base.periods[target]:
            return OK, False
        code = _frame_affected(f, chain_rows(base, source)) if policy.record_rows else OK
        if code == OK:
            code = _move_order_native(_frame_view(f), source, position)
        return code, code == OK
    if action in (FILL, RECLAIM):
        f.indices[0], c[_NC_INDICES] = source, 1
        return _native_local_resource(data, f, d, source)
    code, parts, indices = _parts(base, data.nodes, d)
    if code != OK:
        return code, False
    removed = f.removed[:0]
    if action in (INTRA, NODE_MOVE, NODE_SWAP, BLOCK_MOVE, BLOCK_SWAP):
        if not _parts_have_real(base, data.columns, parts, indices.size):
            return OK, False
        if action != INTRA:
            if not _has_real(data.columns, chain_rows(base, source)[d[START]:d[STOP]]):
                return OK, False
            if action in (NODE_SWAP, BLOCK_SWAP) and not _has_real(data.columns,
                    chain_rows(base, target)[d[OTHER_START]:d[OTHER_STOP]]):
                return OK, False
        parts, removed = _trim_parts(base, data.nodes, parts, d[VARIANT] == 1)
        c[_NC_CLEANED] = int(removed.size > 0)
        if d[VARIANT] == 1 and not c[_NC_CLEANED]:
            return OK, False
        if d[VARIANT] == 0:
            removed = removed[:0]
    if parts.shape[0] > f.parts.shape[0] or indices.size > f.indices.size or removed.size > f.removed.size:
        return CAPACITY, False
    c[_NC_PARTS], c[_NC_INDICES], c[_NC_REMOVED] = parts.shape[0], indices.size, removed.size
    f.parts[:parts.shape[0]], f.indices[:indices.size], f.removed[:removed.size] = parts, indices, removed
    f.progress[:] = 0
    f.progress[_RP_GROUP], f.progress[_RP_SEQUENCE] = -1, c[_NC_SEQUENCE]
    c[_NC_PHASE] = _NC_REPAIR
    return MORE_WORK, False


@njit
def native_candidate_prepare_step(data, f, d, policy):
    """The common non-split preparation, resumable without Python row processing."""
    c, action = f.control, d[ACTION]
    if c[_NC_PHASE] == _NC_DONE:
        return
    code, found = OK, False
    if c[_NC_PHASE] == _NC_BEGIN:
        code, found = _native_candidate_begin(data, f, d, policy)
        if code == MORE_WORK:
            c[_NC_STATUS] = MORE_WORK
            return
        if code == OK and found:
            c[_NC_PHASE] = _NC_FINALIZE
    elif c[_NC_PHASE] == _NC_REPAIR:
        maximum = 0 if action == REAL_MOVE else policy.bridge
        code, found = repair_parts_step(_frame_view(f), data.base_view,
            f.parts[:c[_NC_PARTS]], f.indices[:c[_NC_INDICES]], data.columns, data.rules,
            data.nodes, data.derived, f.nodes, f.derived, f.templates, data.prototypes,
            f.event_node_ends, f.event_group_ends, 0, maximum, f.progress, f.cursor, 64)
        c[_NC_CHANGED], c[_NC_NODES], c[_NC_EVENTS], c[_NC_SEQUENCE] = (
            f.progress[_RP_CHANGED], f.progress[_RP_NODES], f.progress[_RP_EVENTS], f.progress[_RP_SEQUENCE])
        c[_NC_ERROR] = f.progress[_RP_ERROR]
        if code == MORE_WORK:
            c[_NC_STATUS] = MORE_WORK
            return
        if code == OK and found:
            if action in (APPEND, PREPEND, INSERT):
                _move_order_native(_frame_view(f), c[_NC_TARGET], c[_NC_CHAINS] - 1)
                source = _chain_index(_frame_view(f), d[SOURCE])
                _move_order_native(_frame_view(f), source, c[_NC_CHAINS] - 1)
                c[_NC_CHAINS] -= 1
                affected = np.arange(data.nodes.weight.size, data.nodes.weight.size + c[_NC_NODES])
            elif action == REAL_MOVE:
                affected = np.array((d[NODE],), np.int64)
            else:
                affected = _affected_parts(data.base_view, f.parts[:c[_NC_PARTS]],
                    f.removed[:c[_NC_REMOVED]], data.nodes.weight.size, c[_NC_NODES])
            code = _frame_affected(f, affected)
            if code == OK:
                c[_NC_PHASE] = _NC_FINALIZE
    if c[_NC_PHASE] == _NC_FINALIZE:
        columns = private_task_columns(data.columns, f.nodes, f.derived, c[_NC_NODES])
        changed = data.base_view.ids[f.indices[:c[_NC_INDICES]]]
        code, found = _finalize_map(_frame_view(f), columns, changed, policy.maximum, action not in (CUT, ORDER))
        c[_NC_PREPARED] = int(code == OK and found)
    c[_NC_STATUS], c[_NC_PHASE] = code, _NC_DONE


def _sync_native_frame(workspace, frame, policy):
    c = frame.control
    workspace.chain_count, workspace.node_count = int(c[_NC_CHAINS]), int(c[_NC_NODES])
    workspace.event_count, workspace.changed_count = int(c[_NC_EVENTS]), int(c[_NC_CHANGED])
    if c[_NC_ERROR] == _ERROR_BRIDGE_SEQUENCE:
        checked_sum((int(c[_NC_SEQUENCE]) + 1, policy.maximum_bridge_nodes), "private_bridge.sequence")
    elif c[_NC_ERROR] == _ERROR_FILL_SEQUENCE:
        checked_sum((int(c[_NC_SEQUENCE]), 1), "candidate.virtual_sequence")


@njit
def native_candidate_complete_step(data, f, d, policy):
    """Shared native preparation-through-summary step; workers cannot publish."""
    c = f.control
    if c[_NC_PHASE] == _NC_EVALUATE:
        c[_NC_PHASE], c[_NC_STATUS] = _NC_DONE, OK
    else:
        resumed_preparation = c[_NC_PHASE] == _NC_DONE
        native_candidate_prepare_step(data, f, d, policy)
        if not resumed_preparation and c[_NC_PHASE] == _NC_DONE and c[_NC_PREPARED]:
            # Keep the original cancellation boundary before complete evaluation.
            c[_NC_PHASE], c[_NC_STATUS] = _NC_EVALUATE, MORE_WORK
            return
    if c[_NC_PHASE] != _NC_DONE or not c[_NC_PREPARED] or c[_NC_EVALUATED]:
        return
    if not data.has_timing:
        c[_NC_ERROR] = _ERROR_TIMING
        return
    view = _frame_view(f, True)
    if not _split_periods_match(view, data.nodes, f.nodes, data.groups, f.groups):
        c[_NC_PREPARED] = 0
        return
    if view.count <= 0:
        c[_NC_ERROR] = _ERROR_ACTIVE_CHAINS
        return
    for i in range(view.count):
        if view.ids[i] < 0 or not 0 <= view.periods[i] < data.period_count:
            c[_NC_ERROR] = _ERROR_ACTIVE_CHAINS
            return
        for j in range(i):
            if view.ids[i] == view.ids[j]:
                c[_NC_ERROR] = _ERROR_ACTIVE_CHAINS
                return
    columns = private_task_columns(data.columns, f.nodes, f.derived, c[_NC_NODES])
    result = evaluate_view_kernel(columns, data.rules, view, data.objectives, False, 0, 0, False, data.reuse)
    # Only numeric outputs cross a batch barrier. Every slot owns these buffers.
    out = f.output
    out.status[:] = result.status
    if result.ends.size > out.ends.size:
        c[_NC_STATUS], c[_NC_PREPARED] = CAPACITY, 0
        return
    c[_NC_ENDS] = result.ends.size
    out.quality[:] = result.quality
    out.facts[:view.count] = result.facts
    out.scores[:view.count + 1] = result.scores
    out.hits[:view.count + 1] = result.hits
    out.totals[:] = result.totals
    out.ends[:result.ends.size] = result.ends
    out.completion[:], out.late[:], out.waits[:] = result.completion, result.late, result.waits
    out.counts[:], out.event_counts[:view.count] = result.counts, result.event_counts
    c[_NC_EVALUATED], c[_NC_ADMISSIBLE] = 1, 1
    for r in range(policy.reject.size):
        if policy.reject[r]:
            for i in range(view.count + 1):
                if result.hits[i, r]:
                    c[_NC_ADMISSIBLE] = 0


def native_candidate_summary(frame):
    c, out = frame.control, frame.output
    count, nodes = int(c[_NC_CHAINS]), int(c[_NC_ENDS])
    result = KernelResult(out.status[:], out.quality[:], out.facts[:count],
        out.scores[:count + 1], out.hits[:count + 1], out.totals[:], out.ends[:nodes],
        out.completion[:], out.late[:], out.waits[:], out.violations[:], out.metrics[:],
        out.counts[:], out.event_counts[:count])
    _check_kernel_status(result)
    for array in result:
        array.setflags(write=False)
    return result


def finish_native_candidate(workspace, frame, descriptor, program, quality, policy, split_sequence):
    """One result/error boundary shared by single and native-batch execution."""
    _sync_native_frame(workspace, frame, policy)
    c = frame.control
    if c[_NC_ERROR] == _ERROR_ACTIVE_CHAINS:
        raise NumericValueError("evaluation_view", "active chains and task periods must match")
    if c[_NC_ERROR] == _ERROR_TIMING:
        raise NumericValueError("delivery", "task has no production start and duration input")
    summary = native_candidate_summary(frame) if c[_NC_EVALUATED] else None
    return NumericCandidateAttempt(int(c[_NC_STATUS]), bool(c[_NC_PREPARED]),
        bool(c[_NC_ADMISSIBLE]), workspace.view(), summary,
        readonly(frame.affected[:c[_NC_AFFECTED]], np.int64), int(c[_NC_SEQUENCE]),
        split_sequence, bool(c[_NC_CLEANED]), descriptor, program, quality, policy)


@njit
def _advance_native_group(inputs, frames, descriptions, policies, starts, active, group):
    """At most one bounded step per group; dependent repair variants stay ordered."""
    first, stop = starts[group], starts[group + 1]
    for i in range(first, stop):
        if not active[i]:
            continue
        c = frames[i].control
        if c[_NC_PHASE] == _NC_DONE:
            if c[_NC_STATUS] == CAPACITY:
                return
            if c[_NC_ERROR] or c[_NC_EVALUATED] and frames[i].output.status[0] != OK:
                active[i + 1:stop] = False
                return
            continue
        native_candidate_complete_step(inputs[0], frames[i], descriptions[i], policies[i])
        return


@njit(nogil=True)
def native_candidate_batch_serial_step(inputs, frames, descriptions, policies, starts, active):
    for group in range(starts.size - 1):
        _advance_native_group(inputs, frames, descriptions, policies, starts, active, group)


@njit(nogil=True, parallel=True)
def native_candidate_batch_parallel_step(inputs, frames, descriptions, policies, starts, active):
    for group in prange(starts.size - 1):
        _advance_native_group(inputs, frames, descriptions, policies, starts, active, group)


def prepare_candidate_attempt(workspace, program, quality, descriptors, index, policy, *,
                              virtual_sequence=0, split_sequence=0, split_decision=None,
                              allows_continue=None, evaluation_context=None, _complete=False):
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
    if action != SPLIT:
        context = evaluation_context or NumericEvaluationContext(workspace.task, program, quality, workspace.plan, None)
        context.require_current(workspace, program, quality, context.previous_evaluation)
        data = native_candidate_input(context)
        native_policy = native_candidate_policy(program, policy)
        frame = native_candidate_frame(workspace, data, virtual_sequence)
        step = native_candidate_complete_step if _complete else native_candidate_prepare_step
        while frame.control[_NC_PHASE] != _NC_DONE:
            if allows_continue is not None and not allows_continue():
                frame.control[_NC_STATUS], frame.control[_NC_PREPARED] = CANCELLED, 0
                break
            step(data, frame, descriptor, native_policy)
        _sync_native_frame(workspace, frame, policy)
        c = frame.control
        return NumericCandidateAttempt(int(c[_NC_STATUS]), bool(c[_NC_PREPARED]), False,
            workspace.view(), None, readonly(frame.affected[:c[_NC_AFFECTED]], np.int64),
            int(c[_NC_SEQUENCE]), split_sequence, bool(c[_NC_CLEANED]), descriptor, program, quality, policy,
            (data, frame, native_policy))
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
    status, found = _finalize_map(workspace.view(), _columns(workspace), changed_ids,
        policy.maximum_changed_chain_weight, group_periods)
    if status != OK or not found:
        return result(status)
    return result(OK, True)


def compute_candidate_attempt(workspace, program, quality, descriptors, index, policy, *,
                              virtual_sequence=0, split_sequence=0, split_decision=None,
                              previous_evaluation=None, allows_continue=None, preparation=None,
                              evaluation_context=None):
    """Unique complete attempt; optionally resume at the original split quota boundary."""
    if preparation is None:
        if not isinstance(workspace, NumericCandidateWorkspace):
            raise NumericValueError("candidate", "matching numeric workspace required")
        if evaluation_context is None:
            evaluation_context = NumericEvaluationContext(workspace.task, program, quality,
                workspace.plan, previous_evaluation)
        evaluation_context.require_current(workspace, program, quality, previous_evaluation)
        preparation = prepare_candidate_attempt(workspace, program, quality, descriptors, index, policy,
            virtual_sequence=virtual_sequence, split_sequence=split_sequence,
            split_decision=split_decision, allows_continue=allows_continue, evaluation_context=evaluation_context,
            _complete=True)
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
    if preparation.native_state is not None:
        data, frame, native_policy = preparation.native_state
        if evaluation_context is None:
            evaluation_context = NumericEvaluationContext(workspace.task, program, quality,
                workspace.plan, previous_evaluation)
        evaluation_context.require_current(workspace, program, quality, previous_evaluation)
        data = native_candidate_input(evaluation_context)
        if not frame.control[_NC_EVALUATED]:
            if allows_continue is not None and not allows_continue():
                return replace(preparation, status=CANCELLED, prepared=False, native_state=None)
            native_candidate_complete_step(data, frame, preparation.descriptor, native_policy)
        return finish_native_candidate(workspace, frame, preparation.descriptor,
            program, quality, policy, preparation.split_sequence)
    base_groups = PrivateSplitColumns(*(getattr(workspace.task.split_groups, name)
                                         for name in PrivateSplitColumns._fields))
    if not _split_periods_match(workspace.view(), _base_nodes(workspace.task), workspace.nodes,
                                base_groups, workspace.split_groups):
        return replace(preparation, prepared=False)
    if allows_continue is not None and not allows_continue():
        return replace(preparation, status=CANCELLED, prepared=False)
    summary = evaluate_numeric_view(workspace, program, quality, previous_evaluation=previous_evaluation,
                                    context=evaluation_context)
    admissible = not any(summary.hits[:, rule.index].any() for rule in program.rules
                         if rule.kind in policy.reject_prohibited_kinds)
    return replace(preparation, admissible=admissible, summary=summary)
