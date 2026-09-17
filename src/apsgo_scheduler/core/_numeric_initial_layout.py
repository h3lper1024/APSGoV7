"""Same-period ready-chain layout; select an initial state, never a search move.

Keep the existing complete baseline. Only a strictly better complete proposal
with no additional prohibited hits of any other rule may replace it. Readiness
is a construction hint; the existing evaluator remains authoritative.
"""

import logging
from time import perf_counter

import numpy as np
from numba import njit

from ._numeric_chain_ops import flatten_view, reorder_chains
from ._numeric_evaluation import evaluate_numeric_plan
from ._numeric_kernel import _add, _error, flat_chain_view, task_columns
from ._numeric_readiness import READINESS_CURSOR_SIZE, chain_readiness_step
from ._numeric_rules import NumericReason, NumericRuleKind
from ._numeric_state import INVALID, MORE_WORK, OK, NumericPlan
from ._numeric_units import NumericValueError

logger = logging.getLogger(__name__)
_WORK_LIMIT = 256


@njit
def _select_ready_chain_step(releases, pending, start, stop, clock, cursor, status,
                             work_limit=256):
    """Scan one period in stable order, resuming [position, first_pending].

    No selection mutates pending. If this period has no ready chain, return its
    first pending chain. A partial scan returns -1, never a tentative choice.
    """
    if status.size != 5:
        return INVALID, -1, False
    if status[0] != OK:
        return status[0], -1, False
    if (cursor.size != 2 or releases.size != pending.size or start < 0
            or stop > releases.size or stop <= start or clock < 0 or work_limit <= 0):
        _error(status, INVALID)
        return status[0], -1, False
    position, first = cursor
    if (position < start or position > stop
            or first != -1 and (first < start or first >= position or not pending[first])):
        _error(status, INVALID)
        return status[0], -1, False
    work = 0
    while position < stop and work < work_limit:
        row = position
        position += 1
        work += 1
        if pending[row]:
            if first < 0:
                first = row
            cursor[0], cursor[1] = position, first
            if releases[row] <= clock:
                return OK, row, False
    cursor[0], cursor[1] = position, first
    if position < stop:
        return MORE_WORK, -1, False
    if first < 0:
        _error(status, INVALID)
        return status[0], -1, False
    return OK, first, True


def _check_status(code, status, path):
    if code != OK:
        raise NumericValueError(path, f"numeric readiness failed: {int(code)} at {status.tolist()}")


def ready_chain_order(releases, durations, periods, budget):
    """Return (order, starts, blocked, scans), or None on shared-budget stop.

    Inputs are chain-indexed int64 arrays, not source-order or node indices.
    The order contains each chain once; periods must already be nondecreasing.
    No waiting, period resets, candidate charges or mutation of input arrays.
    """
    if not isinstance(releases, np.ndarray):
        raise NumericValueError("initial.ready_order", "integer chain summaries required")
    count = releases.size
    if (count == 0 or any(not isinstance(a, np.ndarray) or a.ndim != 1
                         or a.dtype != np.int64 or a.size != count
                         for a in (releases, durations, periods))
            or np.any(durations < 0) or np.any(periods < 0)
            or np.any(periods[1:] < periods[:-1])):
        raise NumericValueError("initial.ready_order", "nonempty ordered integer chain summaries required")
    order, starts = np.empty(count, np.int64), np.empty(count, np.int64)
    blocked, pending = np.zeros(count, np.bool_), np.ones(count, np.bool_)
    status, cursor = np.zeros(5, np.int64), np.zeros(2, np.int64)
    clock, output, scans, start = 0, 0, 0, 0
    while start < count:
        if not budget.allows_search():
            return None
        stop = int(np.searchsorted(periods, periods[start], side="right"))
        for _ in range(stop - start):
            cursor[:] = (start, -1)
            while True:
                if not budget.allows_search():
                    return None
                before = int(cursor[0])
                code, selected, fallback = _select_ready_chain_step(
                    releases, pending, start, stop, clock, cursor, status, _WORK_LIMIT
                )
                scans += int(cursor[0]) - before
                if code != MORE_WORK:
                    _check_status(code, status, "initial.ready_order")
                    break
            if not budget.allows_search():
                return None
            next_clock = _add(clock, int(durations[selected]), status)
            _check_status(int(status[0]), status, "initial.ready_clock")
            order[output], starts[output], blocked[output] = selected, clock, fallback
            pending[selected] = False
            clock = int(next_clock)
            output += 1
        start = stop
    if not budget.allows_search():
        return None
    return order, starts, blocked, scans


def _chain_summaries(columns, plan, budget):
    releases = np.empty(plan.chain_ids.size, np.int64)
    durations = np.empty_like(releases)
    status = np.zeros(5, np.int64)
    cursor = np.zeros(READINESS_CURSOR_SIZE, np.int64)
    for chain in range(plan.chain_ids.size):
        cursor.fill(0)
        rows = plan.node_rows[int(plan.chain_offsets[chain]):int(plan.chain_offsets[chain + 1])]
        while True:
            if not budget.allows_search():
                return None
            code, release, duration, complete = chain_readiness_step(
                columns, rows, cursor, status, _WORK_LIMIT
            )
            if code == MORE_WORK:
                continue
            _check_status(code, status, f"initial.readiness.chain[{int(plan.chain_ids[chain])}]")
            if not complete:
                raise NumericValueError("initial.readiness", "incomplete successful chain summary")
            releases[chain], durations[chain] = release, duration
            break
    return (releases, durations) if budget.allows_search() else None


def _reordered_plan(task, base, order, budget):
    count = base.chain_ids.size
    if (order.ndim != 1 or order.dtype != np.int64 or order.size != count
            or not np.array_equal(np.sort(order), np.arange(count))
            or not np.array_equal(base.chain_periods[order], base.chain_periods)):
        raise NumericValueError("initial.ready_order", "same-period permutation required")
    if not budget.allows_search():
        return None
    view = flat_chain_view(base.node_rows, base.chain_offsets, base.chain_periods, base.chain_ids)
    # Reorder only private metadata, never the baseline's read-only arrays.
    view = view._replace(starts=view.starts.copy(), stops=view.stops.copy(),
                         private=view.private.copy(), ids=view.ids.copy(),
                         periods=view.periods.copy())
    code = reorder_chains(view, order)
    if code != OK:
        raise NumericValueError("initial.ready_order", "invalid chain permutation")
    rows, offsets = flatten_view(view)
    if not budget.allows_search():
        return None
    candidate = NumericPlan.build(task, rows, offsets, view.ids, view.periods,
                                  generation=base.generation)
    # One-to-one references prove node/source/weight conservation without
    # manufacturing new resources or reinterpreting period ownership.
    if (candidate.task_fingerprint != base.task_fingerprint
            or candidate.generation != base.generation
            or candidate.node_rows.size != base.node_rows.size
            or not np.array_equal(candidate.chain_ids, base.chain_ids[order])
            or not np.array_equal(candidate.chain_periods, base.chain_periods)):
        raise NumericValueError("initial.ready_plan", "chain identity or period changed")
    for position, original in enumerate(order):
        if not budget.allows_search():
            return None
        left = candidate.node_rows[int(candidate.chain_offsets[position]):
                                   int(candidate.chain_offsets[position + 1])]
        right = base.node_rows[int(base.chain_offsets[original]):
                               int(base.chain_offsets[original + 1])]
        if not np.array_equal(left, right):
            raise NumericValueError("initial.ready_plan", "chain contents changed")
    return candidate if budget.allows_search() else None


def _quality(evaluation):
    return tuple(int(value) for value in evaluation.quality_key)


def _require_evaluation(task, program, quality, plan, evaluation):
    if (program.task_fingerprint != task.fingerprint
            or quality.task_fingerprint != task.fingerprint
            or quality.rule_program_fingerprint != program.fingerprint
            or plan.task_fingerprint != task.fingerprint
            or evaluation.plan_fingerprint != plan.fingerprint
            or evaluation.plan_generation != plan.generation
            or evaluation.task_fingerprint != task.fingerprint
            or evaluation.rule_program_fingerprint != program.fingerprint
            or evaluation.quality_program_fingerprint != quality.fingerprint):
        raise NumericValueError("initial.ready_evaluation", "matching plan and complete evaluation required")


def _comparison_reason(program, baseline, proposal):
    """The same rule program owns both evaluations; compare by compiled identity."""
    before, after = baseline.kernel_result.hits, proposal.kernel_result.hits
    if (before.ndim != 2 or after.shape != before.shape
            or before.shape[1] != len(program.rules)):
        raise NumericValueError("initial.ready_evaluation", "incompatible rule hit columns")
    for rule in program.rules:
        if rule.kind is not NumericRuleKind.EARLIEST_START:
            # Boundary-only Python integer sums cannot silently wrap int64.
            old = sum(int(value) for value in before[:, rule.index])
            new = sum(int(value) for value in after[:, rule.index])
            if new > old:
                return "other_rule_increased"
    previous, current = _quality(baseline), _quality(proposal)
    if len(previous) != len(current):
        raise NumericValueError("initial.ready_evaluation", "incompatible quality keys")
    return "strict_improvement" if current < previous else "quality_equal" if current == previous else "quality_worse"


def _early_locations(evaluation):
    # One physical node can be reported by multiple configured rule instances.
    return {(v.chain_index, v.start_position): v.severity // 1000
            for v in evaluation.violations if v.reason is NumericReason.EARLY_START}


def _evaluation_summary(task, plan, evaluation):
    early = _early_locations(evaluation)
    sources = {int(task.nodes.source[int(plan.node_rows[
        int(plan.chain_offsets[chain]) + position])]) for chain, position in early}
    return {"quality": _quality(evaluation), "early_node_count": len(early),
            "early_source_count": len(sources), "early_total_ms": sum(early.values())}


def _blocked_details(task, plan, evaluation, releases, order, starts, blocked, budget):
    first = {}
    for chain, position in _early_locations(evaluation):
        first[chain] = min(position, first.get(chain, position))
    records = []
    for output in np.flatnonzero(blocked):
        if not budget.allows_search():
            return None
        original = int(order[output])
        position = first.get(int(output))
        if position is None:
            raise NumericValueError("initial.ready_diagnostic", "blocked chain has no complete-evaluation finding")
        flat = int(plan.chain_offsets[output]) + position
        row = int(plan.node_rows[flat])
        source = int(task.nodes.source[row])
        records.append({"chain_id": int(plan.chain_ids[output]),
            "period": task.period_ids[int(plan.chain_periods[output])],
            "chain_start_ms": int(starts[output]), "release_offset_ms": int(releases[original]),
            "node_id": task.node_ids[row], "source_order_id": task.source_ids[source],
            "node_start_ms": int(evaluation.delivery.node_end_ms[flat]) - int(task.nodes.duration_ms[row]),
            "earliest_start_ms": int(task.originals.earliest_start_ms[source])})
    return records


def select_ready_initial_plan(task, program, quality, plan, evaluation, budget):
    """Return the selected (plan, evaluation); None preserves construction stop.

    No catch-and-fallback for invalid inputs, arithmetic or evaluation errors.
    Logs distinguish a rejected proposal from the actual selected initial state.
    """
    if not program.for_kind(NumericRuleKind.EARLIEST_START):
        return plan, evaluation
    started = perf_counter()
    _require_evaluation(task, program, quality, plan, evaluation)
    details = {"mode": "same_period_ready_chain", "enabled": True,
               "base": _evaluation_summary(task, plan, evaluation),
               "proposal": None, "selected": None, "reordered_chain_count": 0,
               "split_count": 0, "added_chain_count": 0,
               "candidate_checks_added": 0}

    def stopped():
        details.update(reason="interrupted", stop_reason=getattr(budget.stop_reason, "value", budget.stop_reason),
                       selected=None, reordered_chain_count=0,
                       elapsed_seconds=perf_counter() - started)
        details.pop("selected_chain_ids", None)
        logger.info("numeric_initial_readiness_summary %s", details.copy())
        return None

    summaries = _chain_summaries(task_columns(task), plan, budget)
    if summaries is None:
        return stopped()
    releases, durations = summaries
    ordered = ready_chain_order(releases, durations, plan.chain_periods, budget)
    if ordered is None:
        return stopped()
    order, starts, blocked, scans = ordered
    changed = int(np.count_nonzero(order != np.arange(order.size)))
    details.update(proposal_chain_ids=plan.chain_ids[order].tolist(),
                   proposed_reordered_chain_count=changed, blocked_fallback_count=int(blocked.sum()),
                   scanned_chain_count=scans, readiness_node_count=int(plan.node_rows.size))
    if changed:
        proposal = _reordered_plan(task, plan, order, budget)
        if proposal is None:
            return stopped()
        if not budget.allows_search():
            return stopped()
        proposed_evaluation = evaluate_numeric_plan(task, program, quality, proposal)
        _require_evaluation(task, program, quality, proposal, proposed_evaluation)
        if not budget.allows_search():
            return stopped()
        reason = _comparison_reason(program, evaluation, proposed_evaluation)
    else:
        proposal, proposed_evaluation, reason = plan, evaluation, "order_unchanged"
    details["proposal"] = _evaluation_summary(task, proposal, proposed_evaluation)
    records = _blocked_details(task, proposal, proposed_evaluation, releases,
                               order, starts, blocked, budget)
    if records is None or not budget.allows_search():
        return stopped()
    chosen = (proposal, proposed_evaluation) if reason == "strict_improvement" else (plan, evaluation)
    details.update(reason=reason, selected=details["proposal"] if reason == "strict_improvement" else details["base"],
                   selected_chain_ids=chosen[0].chain_ids.tolist(),
                   reordered_chain_count=changed if reason == "strict_improvement" else 0,
                   blocked_fallbacks=records, elapsed_seconds=perf_counter() - started)
    logger.info("numeric_initial_readiness_summary %s", details.copy())
    return chosen if budget.allows_search() else stopped()
