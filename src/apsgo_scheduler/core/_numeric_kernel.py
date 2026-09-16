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
KernelResult = namedtuple("KernelResult", (
    "status quality facts scores hits totals ends completion late waits "
    "violations metrics counts event_counts"
))
ReuseColumns = namedtuple("ReuseColumns", "rows offsets ids periods facts scores hits event_counts")
BatchResult = namedtuple("BatchResult", (
    "status quality facts scores hits totals ends completion late waits counts event_counts"
))


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
def _tolerance(t, r, i, left, right, s):
    a, b = t.thickness[left], t.thickness[right]
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
def edge_allowed(t, r, i, left, right, s):
    kind = r.meta[i, 0]
    v = r.values[i]
    if kind == 0:
        return (t.present[left, 0] and t.present[right, 0]
                and max(0, _sub(t.width[right], t.width[left], s)) <= v[0])
    if kind == 1:
        if t.role[left] == _GENERATED_VIRTUAL or t.role[right] == _GENERATED_VIRTUAL:
            return r.flags[i, 0]
        if t.role[left] == _ACTUAL_TRANSITION or t.role[right] == _ACTUAL_TRANSITION:
            return r.flags[i, 1]
        a, b = t.soft[left], t.soft[right]
        if a != v[1] and b != v[1]:
            return a == b
        if v[0] == 4:
            return t.hot[left] != v[2] and t.hot[right] != v[2] and t.hot[left] == t.hot[right]
        return v[0] == 1 or v[0] == 2 or v[0] == 3
    if kind == 2:
        if r.flags[i, 0] or (r.flags[i, 1] and (
                t.role[left] == _GENERATED_VIRTUAL or t.role[right] == _GENERATED_VIRTUAL)):
            return True
        if not (t.present[left, 2] and t.present[left, 3]
                and t.present[right, 2] and t.present[right, 3]):
            return True
        overlap = _sub(min(t.maximum[left], t.maximum[right]),
                       max(t.minimum[left], t.minimum[right]), s)
        return overlap >= v[0]
    if kind == 3:
        if not t.present[left, 1] or not t.present[right, 1]:
            return True
        difference, tolerance = _tolerance(t, r, i, left, right, s)
        return difference <= tolerance
    if kind == 4:
        if not t.present[left, 0] or not t.present[right, 0]:
            return False
        virtual = t.role[left] == _GENERATED_VIRTUAL or t.role[right] == _GENERATED_VIRTUAL
        delta = _sub(t.width[right], t.width[left], s)
        if virtual:
            delta = _abs(delta, s)
        return delta <= v[1 if virtual else 0]
    return True


@njit
def _edge(t, r, i, left, right, chain, position, out, detail):
    s = out.status
    _location(s, i, chain, position)
    kind, v = r.meta[i, 0], r.values[i]
    if (kind == 0 or kind == 4) and (not t.present[left, 0] or not t.present[right, 0]):
        _violation(out, detail, i, 0, chain, position, position + 1, SEVERITY)
        return
    allowed = edge_allowed(t, r, i, left, right, s)
    reason, severity = -1, 0
    if kind == 0:
        increase = max(0, _sub(t.width[right], t.width[left], s))
        _metric(out, detail, i, 0, chain, increase, 1, t.scales[3])
        reason = 1
        if not allowed:
            severity = _round(_mul(_sub(increase, v[0], s), SEVERITY, s), t.scales[0], s)
    elif kind == 1:
        reason, severity = 2, SEVERITY
    elif kind == 2 and not allowed:
        overlap = _sub(min(t.maximum[left], t.maximum[right]),
                       max(t.minimum[left], t.minimum[right]), s)
        reason, severity = 3, _ratio(_sub(v[0], overlap, s), max(v[0], t.scales[2]), s)
    elif kind == 3 and not allowed:
        diff, tolerance = _tolerance(t, r, i, left, right, s)
        reason = 4
        severity = SEVERITY if tolerance == 0 else _ratio(_sub(diff, tolerance, s), tolerance, s)
    elif kind == 4 and not allowed:
        virtual = t.role[left] == _GENERATED_VIRTUAL or t.role[right] == _GENERATED_VIRTUAL
        delta = _sub(t.width[right], t.width[left], s)
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
                    group = 0 if t.surface[v[0], row] else -1
                elif kind == 8:
                    group = 0 if t.narrow[v[0], row] else -1
                elif kind == 9:
                    group = t.spec[v[0], row]
                else:
                    group = 0 if t.role[row] == _GENERATED_VIRTUAL else -1
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
                total = _add(total, 1 if kind == 7 or kind == 11 else t.weight[rows[pos]], s)
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
            if not t.present[row, 0]:
                has_baseline = False
            elif has_baseline and t.width[row] > baseline:
                count += 1
            else:
                baseline, has_baseline = t.width[row], True
        if count > v[0]:
            _violation(out, detail, i, 13, chain, 0, length - 1,
                       _mul(count - v[0], SEVERITY, s))
        _metric(out, detail, i, 12, chain, count, 1, t.scales[3])
    elif kind == 13:
        count, previous = 0, False
        for pos in range(length - 1):
            a, b = rows[pos], rows[pos + 1]
            reverse = t.present[a, 0] and t.present[b, 0] and t.width[b] > t.width[a]
            if previous and reverse:
                count += 1
                _violation(out, detail, i, 14, chain, pos - 1, pos, SEVERITY)
            previous = reverse
        _metric(out, detail, i, 13, chain, count, 1, t.scales[3])
    elif kind == 4:
        anchor, anchor_pos = -1, -1
        for pos in range(length):
            row = rows[pos]
            if t.role[row] == _GENERATED_VIRTUAL:
                continue
            if anchor >= 0 and pos > anchor_pos + 1:
                if not t.present[anchor, 0] or not t.present[row, 0]:
                    _violation(out, detail, i, 0, chain, anchor_pos, pos, SEVERITY)
                else:
                    delta = _sub(t.width[row], t.width[anchor], s)
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
        out.facts[chain, 0] = _add(out.facts[chain, 0], t.weight[row], s)
        out.facts[chain, 1] = _add(out.facts[chain, 1], t.duration[row], s)
        column = 3 if t.role[row] == _GENERATED_VIRTUAL else 2
        out.facts[chain, column] = _add(out.facts[chain, column], t.weight[row], s)
        if column == 2 and period < t.period[row]:
            out.facts[chain, 4] = _add(out.facts[chain, 4], t.weight[row], s)


@njit
def _plan_rules(t, r, rows, offsets, periods, out, detail):
    s = out.status
    real, virtual = out.totals[0], out.totals[1]
    total = _add(real, virtual, s)
    for i in range(r.meta.shape[0]):
        _location(s, i, -1, -1)
        kind, v = r.meta[i, 0], r.values[i]
        if kind == 14:
            count = 0
            for c in range(periods.size):
                for p in range(offsets[c], offsets[c + 1]):
                    row = rows[p]
                    if t.role[row] != _GENERATED_VIRTUAL and periods[c] > t.period[row]:
                        count += 1
                        pos = p - offsets[c]
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
            gap = _inter_width_gap(t, rows, offsets, s)
            _metric(out, detail, i, 16, -1, gap, 1, t.scales[3])
        elif kind == 17:
            gap = 0
            for c in range(periods.size):
                gap = _add(gap, max(0, _sub(v[0], out.facts[c, 0], s)), s)
            _metric(out, detail, i, 17, -1, gap, 1, t.scales[3])


@njit
def _clock(t, rows, out):
    s, clock = out.status, 0
    _location(s, -2, -1, -1)
    for pos in range(rows.size):
        row = rows[pos]
        clock = _add(clock, t.duration[row], s)
        out.ends[pos] = clock
        source = t.source[row]
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
def _inter_width_gap(t, rows, offsets, status):
    gap = 0
    for c in range(offsets.size - 2):
        a, b = rows[offsets[c + 1] - 1], rows[offsets[c + 1]]
        if not t.present[a, 0] or not t.present[b, 0]:
            _error(status, INVALID)
            status[2] = c
            return 0
        gap = _add(gap, _abs(_sub(t.width[a], t.width[b], status), status), status)
    return gap


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
    """Single candidate entry. No objects, callbacks, or alternate score formula."""
    count = periods.size
    out = allocate_result(count, r.meta.shape[0], rows.size, t.original_weight.size,
                          violation_capacity, metric_capacity)
    if cancelled:
        out.status[0] = CANCELLED
        return out
    if offsets.size != count + 1 or offsets[0] != 0 or offsets[-1] != rows.size:
        out.status[0] = INVALID
        return out
    for row in rows:
        if row < 0 or row >= t.weight.size:
            out.status[0] = INVALID
            return out
    for c in range(count):
        if offsets[c] >= offsets[c + 1] or (c > 0 and periods[c] < periods[c - 1]):
            out.status[0] = INVALID
            out.status[2] = c
            return out
        chain = rows[offsets[c]:offsets[c + 1]]
        old = -1
        if not detail and reuse is not None and chain_ids is not None:
            for j in range(reuse.ids.size):
                if chain_ids[c] == reuse.ids[j] and periods[c] == reuse.periods[j]:
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
            _facts(t, chain, periods[c], c, out)
            chain_rules(t, r, chain, c, out, detail)
            out.event_counts[c] = (out.counts[0] - v0, out.counts[1] - m0)
            out.counts[2] += 1
            out.counts[3] += chain.size
        for j in range(3):
            out.totals[j] = _add(out.totals[j], out.facts[c, j + 2], out.status)
        if out.status[0] != OK and out.status[0] != CAPACITY:
            return out
    _plan_rules(t, r, rows, offsets, periods, out, detail)
    _clock(t, rows, out)
    for i in range(r.meta.shape[0]):
        if r.meta[i, 0] == 19:
            for k in range(3):
                _metric(out, detail, i, 18 + k, -1, out.totals[3 + k], 1, t.scales[3])
    for i in range(r.meta.shape[0]):
        if r.meta[i, 0] == 5 or r.meta[i, 0] == 6:
            total = 0
            for row in rows:
                total = _add(total, t.priority[r.values[i, 0], row], out.status)
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
def evaluate_batch_kernel(t, r, rows, chain_offsets, candidate_offsets, ids, periods,
                          objective_order, generations, current_generation, reuse,
                          cancelled=False):
    """Bounded serial outer loop; every row calls the authoritative single kernel."""
    size, chains = generations.size, periods.size
    originals = t.original_weight.size
    out = BatchResult(
        np.zeros((size, 5), np.int64), np.zeros((size, 9), np.int64),
        np.zeros((chains, 5), np.int64), np.zeros((chains + size, 4), np.int64),
        np.zeros((chains + size, r.meta.shape[0]), np.int64), np.zeros((size, 7), np.int64),
        np.zeros(rows.size, np.int64), np.zeros((size, originals), np.int64),
        np.zeros((size, originals), np.bool_), np.zeros((size, originals), np.int64),
        np.zeros((size, 4), np.int64), np.zeros((chains, 2), np.int64),
    )
    for i in range(size):
        if generations[i] != current_generation:
            out.status[i, 0] = STALE
            continue
        first, stop = candidate_offsets[i], candidate_offsets[i + 1]
        start_row, stop_row = chain_offsets[first], chain_offsets[stop]
        result = evaluate_kernel(
            t, r, rows[start_row:stop_row], chain_offsets[first:stop + 1] - start_row,
            periods[first:stop], objective_order, False, 0, 0, cancelled,
            ids[first:stop], reuse,
        )
        out.status[i] = result.status
        out.quality[i] = result.quality
        out.facts[first:stop] = result.facts
        out.scores[first + i:stop + i + 1] = result.scores
        out.hits[first + i:stop + i + 1] = result.hits
        out.totals[i] = result.totals
        out.ends[start_row:stop_row] = result.ends
        out.completion[i] = result.completion
        out.late[i] = result.late
        out.waits[i] = result.waits
        out.counts[i] = result.counts
        out.event_counts[first:stop] = result.event_counts
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
