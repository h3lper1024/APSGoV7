"""Authoritative native integer rules and complete candidate evaluation.

All hot functions take native arrays/scalars. Python prepares immutable input
tables; summary and detail evaluation run the same primitives with one switch.
"""
from collections import namedtuple
from functools import lru_cache

import numpy as np
from numba import njit

from ._numeric_rules import _GENERATED_VIRTUAL, _ACTUAL_TRANSITION
from ._numeric_state import OK, INVALID, NUMERIC_ERROR, CANCELLED, CAPACITY, STALE
from ._numeric_state import NumericChainView
from ._numeric_chain_ops import chain_rows
from .contracts import RuleScope

MAX = (1 << 63) - 1
MIN = -(1 << 63)
SEVERITY = 1000000
EDGE, CHAIN = 0, 1

TaskColumns = namedtuple("TaskColumns", (
    "width thickness minimum maximum present weight duration role source period "
    "hot soft priority narrow surface spec original_weight due backlog scales"
))
RuleTables = namedtuple("RuleTables", "meta values flags bands")
EdgeNode = namedtuple("EdgeNode", (
    "width thickness minimum maximum role hot soft has_width has_thickness has_minimum has_maximum"
))
KernelResult = namedtuple("KernelResult", (
    "status quality facts scores hits totals ends completion late waits "
    "violations metrics counts event_counts"
))
ReuseColumns = namedtuple("ReuseColumns", "rows offsets ids periods facts scores hits event_counts")


def _freeze(array):
    array.setflags(write=False)
    return array


@lru_cache(maxsize=128)
def task_columns(task):
    n = task.nodes
    return TaskColumns(
        n.width, n.thickness, n.min_temperature, n.max_temperature, n.present,
        n.weight, n.duration_ms, n.role, n.source, n.source_period,
        n.hot_roll_grade, n.soft_hard_class, task.priority, task.narrow_matches,
        task.surface_matches, task.same_spec_groups, task.originals.weight,
        task.originals.due_ms, task.originals.old_backlog,
        _freeze(np.array((task.units.width, task.units.thickness,
                          task.units.temperature, task.units.weight), dtype=np.int64)),
    )


@njit(inline="always")
def column_value(column, row):
    if isinstance(column, tuple):
        base, tail = column
        return base[row] if row < base.size else tail[row - base.size]
    return column[row]


@njit(inline="always")
def matrix_value(column, i, j, node_axis):
    if isinstance(column, tuple):
        base, tail = column
        if node_axis == 0:
            return base[i, j] if i < base.shape[0] else tail[i - base.shape[0], j]
        return base[i, j] if j < base.shape[1] else tail[i, j - base.shape[1]]
    return column[i, j]


@njit(inline="always")
def column_size(column):
    if isinstance(column, tuple):
        return column[0].size + column[1].size
    return column.size


@njit
def private_task_columns(t, tail, derived, count):
    """Two-part zero-copy columns; rule functions specialize the same formulas."""
    return TaskColumns(
        (t.width, tail.width[:count]), (t.thickness, tail.thickness[:count]),
        (t.minimum, tail.min_temperature[:count]), (t.maximum, tail.max_temperature[:count]),
        (t.present, tail.present[:count]), (t.weight, tail.weight[:count]),
        (t.duration, tail.duration_ms[:count]), (t.role, tail.role[:count]),
        (t.source, tail.source[:count]), (t.period, tail.source_period[:count]),
        (t.hot, tail.hot_roll_grade[:count]), (t.soft, tail.soft_hard_class[:count]),
        (t.priority, derived.priority[:, :count]), (t.narrow, derived.narrow_matches[:, :count]),
        (t.surface, derived.surface_matches[:, :count]), (t.spec, derived.same_spec_groups[:, :count]),
        t.original_weight, t.due, t.backlog, t.scales,
    )


@lru_cache(maxsize=128)
def rule_tables(rules):
    meta = np.zeros((len(rules), 4), dtype=np.int64)
    values = np.zeros((len(rules), 8), dtype=np.int64)
    flags = np.zeros((len(rules), 2), dtype=np.bool_)
    bands = []
    scopes = {RuleScope.EDGE: EDGE, RuleScope.CHAIN: CHAIN}
    for i, rule in enumerate(rules):
        start = len(bands)
        for b in rule.bands:
            bands.append((b.minimum, b.maximum, b.has_minimum, b.has_maximum,
                          b.include_minimum, b.include_maximum, b.relative,
                          b.tolerance_numerator, b.tolerance_denominator))
        meta[i] = (int(rule.kind), scopes.get(rule.scope, 2), start, len(bands))
        values[i, :len(rule.values)] = rule.values
        flags[i, :len(rule.flags)] = rule.flags
    return RuleTables(*map(_freeze, (
        meta, values, flags, np.array(bands, dtype=np.int64).reshape((-1, 9))
    )))


@njit
def _error(s, code):
    if s[0] == OK or s[0] == CAPACITY:
        s[0] = code


@njit
def _location(s, rule, chain, position):
    if s[0] == OK or s[0] == CAPACITY:
        s[1:4] = (rule, chain, position)


@njit
def _add(a, b, s):
    if (b > 0 and a > MAX - b) or (b < 0 and a < MIN - b):
        _error(s, NUMERIC_ERROR)
        return 0
    return a + b


@njit
def _sub(a, b, s):
    if (b < 0 and a > MAX + b) or (b > 0 and a < MIN + b):
        _error(s, NUMERIC_ERROR)
        return 0
    return a - b


@njit
def _mul(a, b, s):
    if a == 0 or b == 0:
        return 0
    if a == MIN:
        if b == 1:
            return MIN
        _error(s, NUMERIC_ERROR)
        return 0
    if b == MIN:
        if a == 1:
            return MIN
        _error(s, NUMERIC_ERROR)
        return 0
    x, y = abs(a), abs(b)
    if x > MAX // y:
        if (a < 0) != (b < 0) and x == MAX // y + 1 and MAX % y == y - 1:
            return MIN
        _error(s, NUMERIC_ERROR)
        return 0
    return a * b


@njit
def _abs(a, s):
    if a == MIN:
        _error(s, NUMERIC_ERROR)
        return 0
    return abs(a)


@njit
def _round(n, d, s):
    # All kernel rounding subjects are nonnegative. No floating projection.
    if n < 0 or d <= 0:
        _error(s, NUMERIC_ERROR)
        return 0
    q, r = n // d, n % d
    return _add(q, int(r >= d - r), s)


@njit
def _ratio(n, d, s, floor_one=True):
    value = _round(_mul(n, SEVERITY, s), d, s)
    return max(SEVERITY, value) if floor_one else value


@njit
def _violation(out, detail, rule, reason, chain, start, end, severity, prohibited=True,
               plan_rule=False):
    slot = chain if chain >= 0 and not plan_rule else out.scores.shape[0] - 1
    if prohibited:
        out.scores[slot, 0] = _add(out.scores[slot, 0], 1, out.status)
        out.scores[slot, 1] = _add(out.scores[slot, 1], severity, out.status)
        out.hits[slot, rule] += 1
    index = out.counts[0]
    out.counts[0] += 1
    if detail:
        if index >= out.violations.shape[0]:
            if out.status[0] == OK:
                out.status[0] = CAPACITY
        else:
            out.violations[index] = (rule, reason, chain, start, end, severity, int(prohibited))


@njit
def _metric(out, detail, rule, kind, chain, value, denominator, weight_scale):
    if chain >= 0:
        if kind == 6:
            out.scores[chain, 2] = _add(out.scores[chain, 2], value, out.status)
        elif kind == 7:
            # Weight scale is a power of ten >= 100: divide first, no product overflow.
            rounded = _round(value, weight_scale // 100, out.status)
            out.scores[chain, 3] = _add(out.scores[chain, 3], rounded, out.status)
    elif kind == 16:
        out.totals[6] = _add(out.totals[6], value, out.status)
    index = out.counts[1]
    out.counts[1] += 1
    if detail:
        if index >= out.metrics.shape[0]:
            if out.status[0] == OK:
                out.status[0] = CAPACITY
        else:
            out.metrics[index] = (rule, kind, chain, value, denominator)


@njit
def _tolerance_values(a, b, r, i, s):
    basis = max(a, b) if r.values[i, 0] else min(a, b)
    numerator, denominator = r.values[i, 1], r.values[i, 2]
    for j in range(r.meta[i, 2], r.meta[i, 3]):
        band = r.bands[j]
        lower = not band[2] or (basis >= band[0] if band[4] else basis > band[0])
        upper = not band[3] or (basis <= band[1] if band[5] else basis < band[1])
        if lower and upper:
            numerator = _mul(basis, band[7], s) if band[6] else band[7]
            denominator = band[8] if band[6] else 1
            break
    difference = _abs(_sub(a, b, s), s)
    return _mul(difference, denominator, s), numerator


@njit
def _tolerance(t, r, i, left, right, s):
    return _tolerance_values(column_value(t.thickness, left), column_value(t.thickness, right), r, i, s)


@njit
def edge_node(t, row):
    return EdgeNode(column_value(t.width, row), column_value(t.thickness, row), column_value(t.minimum, row), column_value(t.maximum, row),
                    np.int64(column_value(t.role, row)), column_value(t.hot, row), column_value(t.soft, row), matrix_value(t.present, row, 0, 0),
                    matrix_value(t.present, row, 1, 0), matrix_value(t.present, row, 2, 0), matrix_value(t.present, row, 3, 0))


@njit
def private_edge_node(t, tail, count, row, s):
    base = column_size(t.weight)
    if row < 0 or row >= base + count:
        _error(s, INVALID)
        return EdgeNode(0, 0, 0, 0, -1, 0, 0, False, False, False, False)
    if row < base:
        return edge_node(t, row)
    i = row - base
    return EdgeNode(tail.width[i], tail.thickness[i], tail.min_temperature[i], tail.max_temperature[i],
                    np.int64(tail.role[i]), tail.hot_roll_grade[i], tail.soft_hard_class[i],
                    tail.present[i, 0], tail.present[i, 1], tail.present[i, 2], tail.present[i, 3])


@njit
def _gather_column(base, tail, rows):
    out = np.empty(rows.size, dtype=base.dtype)
    for i in range(rows.size):
        row = rows[i]
        out[i] = base[row] if row < base.size else tail[row - base.size]
    return out


@njit
def _gather_derived(base, tail, rows):
    out = np.empty((base.shape[0], rows.size), dtype=base.dtype)
    for i in range(rows.size):
        row = rows[i]
        out[:, i] = base[:, row] if row < base.shape[1] else tail[:, row - base.shape[1]]
    return out


@njit
def gather_local_task_columns(t, tail, derived, rows):
    """Project a small resource-local rule subject, never copy the whole task."""
    present = np.empty((rows.size, t.present.shape[1]), dtype=np.bool_)
    for i in range(rows.size):
        row = rows[i]
        present[i] = t.present[row] if row < column_size(t.weight) else tail.present[row - column_size(t.weight)]
    return TaskColumns(
        _gather_column(t.width, tail.width, rows), _gather_column(t.thickness, tail.thickness, rows),
        _gather_column(t.minimum, tail.min_temperature, rows),
        _gather_column(t.maximum, tail.max_temperature, rows), present,
        _gather_column(t.weight, tail.weight, rows), _gather_column(t.duration, tail.duration_ms, rows),
        _gather_column(t.role, tail.role, rows), _gather_column(t.source, tail.source, rows),
        _gather_column(t.period, tail.source_period, rows),
        _gather_column(t.hot, tail.hot_roll_grade, rows), _gather_column(t.soft, tail.soft_hard_class, rows),
        _gather_derived(t.priority, derived.priority, rows),
        _gather_derived(t.narrow, derived.narrow_matches, rows),
        _gather_derived(t.surface, derived.surface_matches, rows),
        _gather_derived(t.spec, derived.same_spec_groups, rows),
        t.original_weight, t.due, t.backlog, t.scales,
    )


@njit
def edge_allowed_values(left, right, r, i, s):
    """One rule formula for formal nodes, private rows and virtual proposals."""
    kind = r.meta[i, 0]
    v = r.values[i]
    if kind == 0:
        return (left.has_width and right.has_width
                and max(0, _sub(right.width, left.width, s)) <= v[0])
    if kind == 1:
        if left.role == _GENERATED_VIRTUAL or right.role == _GENERATED_VIRTUAL:
            return r.flags[i, 0]
        if left.role == _ACTUAL_TRANSITION or right.role == _ACTUAL_TRANSITION:
            return r.flags[i, 1]
        a, b = left.soft, right.soft
        if a != v[1] and b != v[1]:
            return a == b
        if v[0] == 4:
            return left.hot != v[2] and right.hot != v[2] and left.hot == right.hot
        return v[0] == 1 or v[0] == 2 or v[0] == 3
    if kind == 2:
        if r.flags[i, 0] or (r.flags[i, 1] and (
                left.role == _GENERATED_VIRTUAL or right.role == _GENERATED_VIRTUAL)):
            return True
        if not (left.has_minimum and left.has_maximum
                and right.has_minimum and right.has_maximum):
            return True
        overlap = _sub(min(left.maximum, right.maximum), max(left.minimum, right.minimum), s)
        return overlap >= v[0]
    if kind == 3:
        if not left.has_thickness or not right.has_thickness:
            return True
        difference, tolerance = _tolerance_values(left.thickness, right.thickness, r, i, s)
        return difference <= tolerance
    if kind == 4:
        if not left.has_width or not right.has_width:
            return False
        virtual = left.role == _GENERATED_VIRTUAL or right.role == _GENERATED_VIRTUAL
        delta = _sub(right.width, left.width, s)
        if virtual:
            delta = _abs(delta, s)
        return delta <= v[1 if virtual else 0]
    return True


@njit
def edge_allowed(t, r, i, left, right, s):
    return edge_allowed_values(edge_node(t, left), edge_node(t, right), r, i, s)


@njit
def all_edges_allowed_values(left, right, r, s):
    for i in range(r.meta.shape[0]):
        if r.meta[i, 1] == EDGE:
            _location(s, i, -1, -1)
            if not edge_allowed_values(left, right, r, i, s):
                return False
    return s[0] == OK


@njit
def edge_matrix_kernel(t, r, left_rows, right_rows):
    allowed = np.zeros((left_rows.size, right_rows.size), dtype=np.bool_)
    status = np.array((OK, -1, -1, -1), dtype=np.int64)
    for i in range(left_rows.size):
        for j in range(right_rows.size):
            allowed[i, j] = all_edges_allowed_values(
                edge_node(t, left_rows[i]), edge_node(t, right_rows[j]), r, status)
            if status[0] != OK:
                return allowed, status
    return allowed, status


@njit
def _edge(t, r, i, left, right, chain, position, out, detail):
    s = out.status
    _location(s, i, chain, position)
    kind, v = r.meta[i, 0], r.values[i]
    if (kind == 0 or kind == 4) and (not matrix_value(t.present, left, 0, 0) or not matrix_value(t.present, right, 0, 0)):
        _violation(out, detail, i, 0, chain, position, position + 1, SEVERITY)
        return
    allowed = edge_allowed(t, r, i, left, right, s)
    reason, severity = -1, 0
    if kind == 0:
        increase = max(0, _sub(column_value(t.width, right), column_value(t.width, left), s))
        _metric(out, detail, i, 0, chain, increase, 1, t.scales[3])
        reason = 1
        if not allowed:
            severity = _round(_mul(_sub(increase, v[0], s), SEVERITY, s), t.scales[0], s)
    elif kind == 1:
        reason, severity = 2, SEVERITY
    elif kind == 2 and not allowed:
        overlap = _sub(min(column_value(t.maximum, left), column_value(t.maximum, right)),
                       max(column_value(t.minimum, left), column_value(t.minimum, right)), s)
        reason, severity = 3, _ratio(_sub(v[0], overlap, s), max(v[0], t.scales[2]), s)
    elif kind == 3 and not allowed:
        diff, tolerance = _tolerance(t, r, i, left, right, s)
        reason = 4
        severity = SEVERITY if tolerance == 0 else _ratio(_sub(diff, tolerance, s), tolerance, s)
    elif kind == 4 and not allowed:
        virtual = column_value(t.role, left) == _GENERATED_VIRTUAL or column_value(t.role, right) == _GENERATED_VIRTUAL
        delta = _sub(column_value(t.width, right), column_value(t.width, left), s)
        if virtual:
            delta = _abs(delta, s)
        limit = v[1 if virtual else 0]
        reason, severity = 5, _ratio(_sub(delta, limit, s), max(limit, t.scales[0]), s)
    if not allowed:
        _violation(out, detail, i, reason, chain, position, position + 1, severity)


@njit
def _chain(t, r, i, rows, chain, out, detail):
    s, v, kind = out.status, r.values[i], r.meta[i, 0]
    _location(s, i, chain, 0)
    length = rows.size
    if kind == 7 or kind == 8 or kind == 9 or kind == 11:
        start, previous, total, maximum = -1, -1, 0, 0
        limit = v[0] if kind == 11 else v[1]
        metric = 11 if kind == 11 else kind - 4
        reason = 12 if kind == 11 else kind
        for pos in range(length + 1):
            group = -1
            if pos < length:
                row = rows[pos]
                if kind == 7:
                    group = 0 if matrix_value(t.surface, v[0], row, 1) else -1
                elif kind == 8:
                    group = 0 if matrix_value(t.narrow, v[0], row, 1) else -1
                elif kind == 9:
                    group = matrix_value(t.spec, v[0], row, 1)
                else:
                    group = 0 if column_value(t.role, row) == _GENERATED_VIRTUAL else -1
            if start >= 0 and (group < 0 or group != previous):
                maximum = max(maximum, total)
                if total > limit:
                    severity = _mul(_sub(total, limit, s), SEVERITY, s)
                    if kind == 8 or kind == 9:
                        severity = _round(severity, t.scales[3], s)
                    _violation(out, detail, i, reason, chain, start, pos - 1, severity)
                start, total = -1, 0
            if group >= 0:
                if start < 0:
                    start = pos
                total = _add(total, 1 if kind == 7 or kind == 11 else column_value(t.weight, rows[pos]), s)
            previous = group
        _metric(out, detail, i, metric, chain, maximum, 1, t.scales[3])
    elif kind == 10:
        total = out.facts[chain, 0]
        gap, excess = max(0, _sub(v[0], total, s)), max(0, _sub(total, v[1], s))
        if gap:
            _violation(out, detail, i, 10, chain, 0, length - 1,
                       _round(_mul(gap, SEVERITY, s), t.scales[3], s), False)
        if excess:
            _violation(out, detail, i, 11, chain, 0, length - 1,
                       _round(_mul(excess, SEVERITY, s), t.scales[3], s))
        for metric, value in ((6, int(gap > 0)), (7, gap), (8, int(excess > 0)),
                              (9, excess), (10, _abs(_sub(total, v[2], s), s))):
            _metric(out, detail, i, metric, chain, value, 1, t.scales[3])
    elif kind == 12:
        baseline, has_baseline, count = 0, False, 0
        for row in rows:
            if not matrix_value(t.present, row, 0, 0):
                has_baseline = False
            elif has_baseline and column_value(t.width, row) > baseline:
                count += 1
            else:
                baseline, has_baseline = column_value(t.width, row), True
        if count > v[0]:
            _violation(out, detail, i, 13, chain, 0, length - 1,
                       _mul(count - v[0], SEVERITY, s))
        _metric(out, detail, i, 12, chain, count, 1, t.scales[3])
    elif kind == 13:
        count, previous = 0, False
        for pos in range(length - 1):
            a, b = rows[pos], rows[pos + 1]
            reverse = matrix_value(t.present, a, 0, 0) and matrix_value(t.present, b, 0, 0) and column_value(t.width, b) > column_value(t.width, a)
            if previous and reverse:
                count += 1
                _violation(out, detail, i, 14, chain, pos - 1, pos, SEVERITY)
            previous = reverse
        _metric(out, detail, i, 13, chain, count, 1, t.scales[3])
    elif kind == 4:
        anchor, anchor_pos = -1, -1
        for pos in range(length):
            row = rows[pos]
            if column_value(t.role, row) == _GENERATED_VIRTUAL:
                continue
            if anchor >= 0 and pos > anchor_pos + 1:
                if not matrix_value(t.present, anchor, 0, 0) or not matrix_value(t.present, row, 0, 0):
                    _violation(out, detail, i, 0, chain, anchor_pos, pos, SEVERITY)
                else:
                    delta = _sub(column_value(t.width, row), column_value(t.width, anchor), s)
                    if delta > v[0]:
                        _violation(out, detail, i, 6, chain, anchor_pos, pos,
                                   _ratio(_sub(delta, v[0], s), max(v[0], t.scales[0]), s))
            anchor, anchor_pos = row, pos


@njit
def chain_rules(t, r, rows, chain, out, detail):
    first = r.meta.shape[0]
    for i in range(r.meta.shape[0]):
        if r.meta[i, 1] == EDGE:
            first = i
            break
    for i in range(first):
        if r.meta[i, 1] == CHAIN:
            _chain(t, r, i, rows, chain, out, detail)
    for pos in range(rows.size - 1):
        for i in range(r.meta.shape[0]):
            if r.meta[i, 1] == EDGE:
                _edge(t, r, i, rows[pos], rows[pos + 1], chain, pos, out, detail)
    for i in range(r.meta.shape[0]):
        if (r.meta[i, 1] == CHAIN and i >= first) or r.meta[i, 0] == 4:
            _chain(t, r, i, rows, chain, out, detail)


@njit
def _facts(t, rows, period, chain, out):
    s = out.status
    _location(s, -1, chain, 0)
    for row in rows:
        out.facts[chain, 0] = _add(out.facts[chain, 0], column_value(t.weight, row), s)
        out.facts[chain, 1] = _add(out.facts[chain, 1], column_value(t.duration, row), s)
        column = 3 if column_value(t.role, row) == _GENERATED_VIRTUAL else 2
        out.facts[chain, column] = _add(out.facts[chain, column], column_value(t.weight, row), s)
        if column == 2 and period < column_value(t.period, row):
            out.facts[chain, 4] = _add(out.facts[chain, 4], column_value(t.weight, row), s)


@njit
def flat_chain_view(rows, offsets, periods, ids=None):
    count = periods.size
    identities = np.arange(count, dtype=np.int64) if ids is None else ids
    return NumericChainView(rows, rows[:0], offsets[:-1], offsets[1:],
                            np.zeros(count, dtype=np.bool_), identities, periods, count, 0)


@njit
def _plan_rules_view(t, r, view, out, detail):
    s = out.status
    real, virtual = out.totals[0], out.totals[1]
    total = _add(real, virtual, s)
    for i in range(r.meta.shape[0]):
        _location(s, i, -1, -1)
        kind, v = r.meta[i, 0], r.values[i]
        if kind == 14:
            count = 0
            for c in range(view.count):
                chain = chain_rows(view, c)
                for pos in range(chain.size):
                    row = chain[pos]
                    if column_value(t.role, row) != _GENERATED_VIRTUAL and view.periods[c] > column_value(t.period, row):
                        count += 1
                        _violation(out, detail, i, 15, c, pos, pos, SEVERITY, True, True)
            _metric(out, detail, i, 14, -1, count, 1, t.scales[3])
        elif kind == 15:
            a, b = _mul(virtual, v[1], s), _mul(total, v[0], s)
            if total and a > b:
                severity = SEVERITY if v[0] == 0 else _ratio(
                    _sub(a, b, s), _mul(total, v[1], s), s, False)
                _violation(out, detail, i, 16, -1, -1, -1, max(1, severity))
            _metric(out, detail, i, 15, -1, virtual, total or 1, t.scales[3])
        elif kind == 16:
            gap = _inter_width_gap_view(t, view, s)
            _metric(out, detail, i, 16, -1, gap, 1, t.scales[3])
        elif kind == 17:
            gap = 0
            for c in range(view.count):
                gap = _add(gap, max(0, _sub(v[0], out.facts[c, 0], s)), s)
            _metric(out, detail, i, 17, -1, gap, 1, t.scales[3])


@njit
def _clock_view(t, view, out):
    s, clock, pos = out.status, 0, 0
    _location(s, -2, -1, -1)
    for c in range(view.count):
        for row in chain_rows(view, c):
            clock = _add(clock, column_value(t.duration, row), s)
            out.ends[pos] = clock
            pos += 1
            source = column_value(t.source, row)
            if source >= 0:
                out.completion[source] = max(out.completion[source], clock)
    old = 0
    for source in range(t.original_weight.size):
        end, due = out.completion[source], t.due[source]
        late = due > 0 and end > due
        out.late[source] = late
        if late:
            out.totals[3] = _add(out.totals[3], t.original_weight[source], s)
        wait = _round(max(0, _sub(end, max(due, 0), s)), 1000, s)
        out.waits[source] = wait
        out.totals[5] = _add(out.totals[5], _mul(t.original_weight[source], wait, s), s)
        if t.backlog[source]:
            old = max(old, end)
    out.totals[4] = _round(old, 1000, s)


@njit
def _inter_width_gap_view(t, view, status):
    gap = 0
    for c in range(view.count - 1):
        a, b = chain_rows(view, c)[-1], chain_rows(view, c + 1)[0]
        if not matrix_value(t.present, a, 0, 0) or not matrix_value(t.present, b, 0, 0):
            _error(status, INVALID)
            status[2] = c
            return 0
        gap = _add(gap, _abs(_sub(column_value(t.width, a), column_value(t.width, b), status), status), status)
    return gap


@njit
def _plan_rules(t, r, rows, offsets, periods, out, detail):
    _plan_rules_view(t, r, flat_chain_view(rows, offsets, periods), out, detail)


@njit
def _clock(t, rows, out):
    _clock_view(t, flat_chain_view(rows, np.array((0, rows.size), dtype=np.int64),
                                  np.zeros(1, dtype=np.int64)), out)


@njit
def _inter_width_gap(t, rows, offsets, status):
    return _inter_width_gap_view(t, flat_chain_view(rows, offsets,
        np.zeros(offsets.size - 1, dtype=np.int64)), status)


@njit
def allocate_result(chains, rules, nodes, originals, violation_capacity=0, metric_capacity=0):
    return KernelResult(
        np.zeros(5, np.int64), np.zeros(9, np.int64),
        np.zeros((chains, 5), np.int64), np.zeros((chains + 1, 4), np.int64),
        np.zeros((chains + 1, rules), np.int64), np.zeros(7, np.int64),
        np.zeros(nodes, np.int64), np.zeros(originals, np.int64),
        np.zeros(originals, np.bool_), np.zeros(originals, np.int64),
        np.empty((violation_capacity, 7), np.int64),
        np.empty((metric_capacity, 5), np.int64), np.zeros(4, np.int64),
        np.zeros((chains, 2), np.int64),
    )


@njit
def evaluate_kernel(t, r, rows, offsets, periods, objective_order, detail=False,
                    violation_capacity=0, metric_capacity=0, cancelled=False,
                    chain_ids=None, reuse=None):
    """A formal flat plan is the zero-change case of the common view kernel."""
    count = periods.size
    if cancelled or offsets.size != count + 1 or offsets[0] != 0 or offsets[-1] != rows.size:
        out = allocate_result(count, r.meta.shape[0], rows.size, t.original_weight.size,
                              violation_capacity, metric_capacity)
        out.status[0] = CANCELLED if cancelled else INVALID
        return out
    return evaluate_view_kernel(t, r, flat_chain_view(rows, offsets, periods, chain_ids),
        objective_order, detail, violation_capacity, metric_capacity, cancelled,
        reuse, chain_ids is not None)


@njit
def evaluate_view_kernel(t, r, view, objective_order, detail=False,
                         violation_capacity=0, metric_capacity=0, cancelled=False,
                         reuse=None, allow_reuse=True):
    """Evaluate base/changed chains and base/private columns without flattening."""
    count, size = view.count, 0
    invalid_chain = -1
    if count < 0 or count > min(view.starts.size, view.stops.size, view.private.size,
                                view.ids.size, view.periods.size):
        out = allocate_result(0, r.meta.shape[0], 0, t.original_weight.size)
        out.status[0] = INVALID
        return out
    for c in range(count):
        capacity = view.changed_rows.size if view.private[c] else view.base_rows.size
        if (view.starts[c] < 0 or view.stops[c] > capacity or view.starts[c] >= view.stops[c]
                or c > 0 and view.periods[c] < view.periods[c - 1]):
            invalid_chain = c
            break
        size += view.stops[c] - view.starts[c]
    if invalid_chain >= 0 or cancelled:
        out = allocate_result(count, r.meta.shape[0], 0, t.original_weight.size)
        out.status[0] = CANCELLED if cancelled else INVALID
        out.status[2] = invalid_chain
        return out
    out = allocate_result(count, r.meta.shape[0], size, t.original_weight.size,
                          violation_capacity, metric_capacity)
    for c in range(count):
        chain = chain_rows(view, c)
        for row in chain:
            if row < 0 or row >= column_size(t.weight):
                out.status[0] = INVALID
                return out
        old = -1
        if not detail and reuse is not None and allow_reuse:
            for j in range(reuse.ids.size):
                if view.ids[c] == reuse.ids[j] and view.periods[c] == reuse.periods[j]:
                    before = reuse.rows[reuse.offsets[j]:reuse.offsets[j + 1]]
                    if chain.size == before.size and np.array_equal(chain, before):
                        old = j
                    break
        if old >= 0:
            out.facts[c] = reuse.facts[old]
            out.scores[c] = reuse.scores[old]
            out.hits[c] = reuse.hits[old]
            out.event_counts[c] = reuse.event_counts[old]
            out.counts[:2] += reuse.event_counts[old]
        else:
            v0, m0 = out.counts[0], out.counts[1]
            _facts(t, chain, view.periods[c], c, out)
            chain_rules(t, r, chain, c, out, detail)
            out.event_counts[c] = (out.counts[0] - v0, out.counts[1] - m0)
            out.counts[2] += 1
            out.counts[3] += chain.size
        for j in range(3):
            out.totals[j] = _add(out.totals[j], out.facts[c, j + 2], out.status)
        if out.status[0] != OK and out.status[0] != CAPACITY:
            return out
    _plan_rules_view(t, r, view, out, detail)
    _clock_view(t, view, out)
    for i in range(r.meta.shape[0]):
        if r.meta[i, 0] == 19:
            for k in range(3):
                _metric(out, detail, i, 18 + k, -1, out.totals[3 + k], 1, t.scales[3])
    for i in range(r.meta.shape[0]):
        if r.meta[i, 0] == 5 or r.meta[i, 0] == 6:
            total = 0
            for c in range(count):
                for row in chain_rows(view, c):
                    total = _add(total, matrix_value(t.priority, r.values[i, 0], row, 1), out.status)
            _metric(out, detail, i, r.meta[i, 0] - 4, -2, total, 1, t.scales[3])
    canonical = np.zeros(9, np.int64)
    for c in range(count + 1):
        for j in range(4):
            canonical[j] = _add(canonical[j], out.scores[c, j], out.status)
    canonical[4:] = (out.totals[4], out.totals[5], out.totals[6], out.totals[1], count)
    for j in range(9):
        out.quality[j] = canonical[objective_order[j]]
    if out.status[0] == OK:
        out.status[1:4] = -1
    return out




@njit
def evaluate_chain_kernel(t, r, rows, chain_index=0, detail=False,
                          violation_capacity=0, metric_capacity=0):
    out = allocate_result(chain_index + 1, r.meta.shape[0], 0, 0,
                          violation_capacity, metric_capacity)
    _facts(t, rows, 0, chain_index, out)
    chain_rules(t, r, rows, chain_index, out, detail)
    return out


@njit
def edge_kernel(t, r, left, right, chain=-1, position=-1, detail=False):
    # One edge can emit at most one violation/metric per rule.
    count = r.meta.shape[0] if detail else 0
    out = allocate_result(max(0, chain + 1), r.meta.shape[0], 0, 0, count, count)
    for i in range(r.meta.shape[0]):
        if r.meta[i, 1] == EDGE:
            _edge(t, r, i, left, right, chain, position, out, detail)
    return out


@njit
def edges_allowed_kernel(t, r, left, right):
    status = np.zeros(5, np.int64)
    for i in range(r.meta.shape[0]):
        if r.meta[i, 1] == EDGE:
            _location(status, i, -1, -1)
            if not edge_allowed(t, r, i, left, right, status):
                return False, status
    return True, status


@njit
def chain_order_kernel(t, rows, offsets, order, current_quality, objective_order):
    out = allocate_result(order.size, 0, rows.size, t.original_weight.size)
    ordered = np.empty_like(rows)
    boundaries = np.zeros_like(offsets)
    position = 0
    for c in range(order.size):
        old = order[c]
        for p in range(offsets[old], offsets[old + 1]):
            ordered[position] = rows[p]
            position += 1
        boundaries[c + 1] = position
    _clock(t, ordered, out)
    gap = _inter_width_gap(t, ordered, boundaries, out.status)
    out.quality[:] = current_quality
    for i in range(objective_order.size):
        if objective_order[i] == 4:
            out.quality[i] = out.totals[4]
        elif objective_order[i] == 5:
            out.quality[i] = out.totals[5]
        elif objective_order[i] == 6:
            out.quality[i] = gap
    return out


def pack_rows(rows):
    return _freeze(np.ascontiguousarray(rows, dtype=np.int64))


@njit
def static_plan_kernel(t, r, rows, offsets, periods):
    capacity = (rows.size + 1) * (r.meta.shape[0] + 1)
    out = allocate_result(periods.size, r.meta.shape[0], 0, 0, capacity, capacity)
    for c in range(periods.size):
        _facts(t, rows[offsets[c]:offsets[c + 1]], periods[c], c, out)
        for j in range(3):
            out.totals[j] = _add(out.totals[j], out.facts[c, j + 2], out.status)
    _plan_rules(t, r, rows, offsets, periods, out, True)
    return out
