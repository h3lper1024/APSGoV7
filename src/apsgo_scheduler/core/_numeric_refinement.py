"""Deterministic post-search structural refinement on the numeric state."""

import logging
from collections import deque
from dataclasses import dataclass, field
from time import perf_counter

from . import _numeric_refinement_scan as numeric_scan
from . import _numeric_candidate_kernel as common_candidate
from ._numeric_kernel import task_columns, rule_tables
from ._numeric_batch import NumericCandidateBatchWorkspace
from ._numeric_rules import NumericRuleKind
from ._numeric_search import (
    NumericSearchCheckpoint, NumericSearchState, _run_numeric_local_search,
    _validate_search_inputs, improve_numeric_controlled_split, capture_candidate_result,
    consume_candidate_result, NumericDeferredCandidateFailure,
)
from ._numeric_units import NumericValueError
from .budget import SolveRuntimeBudget
from .contracts import SearchStopReason

logger = logging.getLogger(__name__)

_PROPOSALS_PER_FAMILY = 64
_SERIAL_BATCH_SIZE = 8
_CRITICAL_FAMILY_COUNT = len(("intra", "node", "block"))
_REGULAR_FAMILY_COUNT = 6
_FAMILIES = ("intra", "node", "block", "cut", "order", "reclaim")


@dataclass(slots=True)
class NumericRefinementDiagnostics:
    """Aggregated refinement work counters; never records individual candidates."""

    raw_combinations: dict[str, int] = field(default_factory=dict)
    unique_combinations: dict[str, int] = field(default_factory=dict)
    lane_filtered: dict[str, int] = field(default_factory=dict)
    routed_combinations: dict[str, int] = field(default_factory=dict)
    candidate_checks: dict[str, int] = field(default_factory=dict)
    complete_evaluations: dict[str, int] = field(default_factory=dict)
    accepted: dict[str, int] = field(default_factory=dict)
    plan_materializations: dict[str, int] = field(default_factory=dict)
    batch_prepared: dict[str, int] = field(default_factory=dict)
    batch_consumed: dict[str, int] = field(default_factory=dict)
    batch_rejected: dict[str, int] = field(default_factory=dict)
    batch_accepted: dict[str, int] = field(default_factory=dict)
    batch_stale: dict[str, int] = field(default_factory=dict)
    batch_cancelled: dict[str, int] = field(default_factory=dict)
    batch_stopped: dict[str, int] = field(default_factory=dict)
    numeric_precomputed: dict[str, int] = field(default_factory=dict)
    numeric_consumed: dict[str, int] = field(default_factory=dict)
    numeric_discarded: dict[str, int] = field(default_factory=dict)
    numeric_batch_calls: int = 0
    numeric_batch_prepare_seconds: float = 0.0
    numeric_batch_evaluate_seconds: float = 0.0
    maximum_numeric_batch_bytes: int = 0
    maximum_numeric_batch_seconds: float = 0.0
    layout_call_count: int = 0
    layout_seconds: float = 0.0
    maximum_generator_advance_seconds: float = 0.0
    maximum_batch_size: int = 0

    @staticmethod
    def _increment(values, key, count=1):
        values[key] = values.get(key, 0) + count

    def record(self, name, key, count=1):
        if count:
            self._increment(getattr(self, name), key, count)

    def snapshot(self):
        return {
            name: dict(sorted(getattr(self, name).items()))
            for name in (
                "raw_combinations",
                "unique_combinations",
                "lane_filtered",
                "routed_combinations",
                "candidate_checks",
                "complete_evaluations",
                "accepted",
                "plan_materializations",
                "batch_prepared",
                "batch_consumed",
                "batch_rejected",
                "batch_accepted",
                "batch_stale",
                "batch_cancelled",
                "batch_stopped",
                "numeric_precomputed",
                "numeric_consumed",
                "numeric_discarded",
            )
        } | {
            "layout_call_count": self.layout_call_count,
            "layout_seconds": self.layout_seconds,
            "maximum_generator_advance_seconds": self.maximum_generator_advance_seconds,
            "maximum_batch_size": self.maximum_batch_size,
            "numeric_batch_calls": self.numeric_batch_calls,
            "numeric_batch_prepare_seconds": self.numeric_batch_prepare_seconds,
            "numeric_batch_evaluate_seconds": self.numeric_batch_evaluate_seconds,
            "maximum_numeric_batch_bytes": self.maximum_numeric_batch_bytes,
            "maximum_numeric_batch_seconds": self.maximum_numeric_batch_seconds,
        }


def _maximum_chain_weight(state):
    rules = state.program.for_kind(NumericRuleKind.CHAIN_WEIGHT)
    return None if not rules else rules[0].values[1]


def _scan_family(
    state,
    budget,
    recipes,
    maximum_virtual_bridge_nodes=2,
    *,
    diagnostics=None,
    diagnostic_key="regular:unknown",
    index=None,
    cursor=None,
    family=None,
    _batch_size=_SERIAL_BATCH_SIZE,
    _workspace=None,
):
    if type(_batch_size) is not int or not 1 <= _batch_size <= _PROPOSALS_PER_FAMILY:
        raise NumericValueError("candidate_batch_size", "one to 64 descriptions required")
    if (cursor is None) != (family is None):
        raise NumericValueError("candidate_batch_cursor", "cursor and family required together")
    pool = _workspace
    if pool is None and state is not None:
        pool = NumericCandidateBatchWorkspace(state.task, state.program, state.quality,
            state.plan, state.evaluation, maximum_candidates=_batch_size)
    if pool is not None:
        pool.require_current(state.task, state.program, state.quality, state.plan, state.evaluation)
    try:
        remaining = _PROPOSALS_PER_FAMILY
        while remaining:
            if not budget.allows_search():
                return False, False
            available = budget.candidate_check_limit - budget.candidate_check_count
            limit = min(_batch_size, pool.maximum_candidates if pool is not None else _batch_size, remaining, available + 1)
            batch, exhausted = [], False
            for _ in range(limit):
                if not budget.allows_search():
                    break
                try:
                    started = perf_counter()
                    batch.append(next(recipes))
                    if diagnostics is not None:
                        diagnostics.maximum_generator_advance_seconds = max(
                            diagnostics.maximum_generator_advance_seconds,
                            perf_counter() - started,
                        )
                except StopIteration:
                    exhausted = not budget.must_stop
                    break
            if diagnostics is not None:
                diagnostics.record("batch_prepared", diagnostic_key, len(batch))
                diagnostics.maximum_batch_size = max(
                    diagnostics.maximum_batch_size, len(batch)
                )
            if not batch:
                return False, exhausted
            before_computed = diagnostics.numeric_precomputed.get(diagnostic_key, 0) if diagnostics else 0
            before_consumed = diagnostics.numeric_consumed.get(diagnostic_key, 0) if diagnostics else 0
            prepared = _prepare_descriptor_batch(
                state, budget, [item[1] if cursor is not None else item for item in batch],
                maximum_virtual_bridge_nodes, diagnostics, diagnostic_key, pool,
            ) if _batch_size > 1 else [None] * len(batch)

            def record_discarded():
                if pool is not None:
                    pool.release()
                if diagnostics is not None:
                    diagnostics.record(
                        "numeric_discarded", diagnostic_key,
                        diagnostics.numeric_precomputed.get(diagnostic_key, 0) - before_computed
                        - diagnostics.numeric_consumed.get(diagnostic_key, 0) + before_consumed,
                    )

            for position, item in enumerate(batch):
                owner, recipe = item if cursor is not None else (None, item)
                if not budget.consume_candidate_check():
                    stopped = len(batch) - position
                    if diagnostics is not None:
                        diagnostics.record("batch_stopped", diagnostic_key, stopped)
                        if budget.stop_reason is SearchStopReason.USER_CANCELLED:
                            diagnostics.record("batch_cancelled", diagnostic_key, stopped)
                    record_discarded()
                    return False, False
                remaining -= 1
                if cursor is not None:
                    cursor[family] = owner
                if diagnostics is not None:
                    diagnostics.record("candidate_checks", diagnostic_key)
                    diagnostics.record("batch_consumed", diagnostic_key)
                evaluations = state.complete_candidate_evaluation_count if state is not None else 0
                accepted = _try_descriptor(
                    state,
                    budget,
                    recipe,
                    maximum_virtual_bridge_nodes,
                    diagnostics,
                    diagnostic_key,
                    prepared[position],
                    pool,
                )
                if diagnostics is not None:
                    diagnostics.record(
                        "complete_evaluations",
                        diagnostic_key,
                        state.complete_candidate_evaluation_count - evaluations,
                    )
                    diagnostics.record(
                        "batch_accepted" if accepted else "batch_rejected",
                        diagnostic_key,
                    )
                    if accepted:
                        diagnostics.record(
                            "batch_stale", diagnostic_key, len(batch) - position - 1
                        )
                if accepted:
                    if diagnostics is not None:
                        diagnostics.record("accepted", diagnostic_key)
                    record_discarded()
                    return True, False
            record_discarded()
            if exhausted:
                return False, True
        return False, False
    finally:
        if pool is not None:
            pool.release()


def _descriptor_attempts(state, budget, descriptor, maximum_virtual_bridge_nodes, pool=None):
    """Stage policy and variant order are explicit; buffers belong to the common batch."""
    if pool is None:
        pool = NumericCandidateBatchWorkspace(state.task, state.program, state.quality,
            state.plan, state.evaluation, maximum_candidates=1)
    pool.require_current(state.task, state.program, state.quality, state.plan, state.evaluation)
    policy, variants = _descriptor_settings(state, descriptor, maximum_virtual_bridge_nodes)
    yield from pool.attempts(descriptor, policy, variants,
        virtual_sequence=state.virtual_sequence, split_sequence=state.split_sequence,
        allows_continue=budget.allows_search, capture=capture_candidate_result)


def _descriptor_settings(state, descriptor, maximum_virtual_bridge_nodes):
    segments = int(descriptor[common_candidate.ACTION]) in (
        common_candidate.INTRA, common_candidate.NODE_MOVE, common_candidate.NODE_SWAP,
        common_candidate.BLOCK_MOVE, common_candidate.BLOCK_SWAP)
    maximum = _maximum_chain_weight(state) if segments else None
    # With release-time repair, existing violations need not disappear in one
    # move. The shared consumer/commit guard forbids increasing other rule hits.
    rejected_kinds = () if state.program.for_kind(NumericRuleKind.EARLIEST_START) else tuple(NumericRuleKind)
    policy = common_candidate.CandidateCheckPolicy(maximum_virtual_bridge_nodes,
        -1 if maximum is None else maximum,
        rejected_kinds)
    return policy, (1, 0) if segments else (0,)


def _prepare_descriptor_batch(state, budget, descriptors, maximum_virtual_bridge_nodes, diagnostics, key, pool=None):
    """Stage the ordered numeric descriptions in reusable generation-local slots."""
    started = perf_counter()
    prepared, count, private_bytes = [], 0, 0
    remaining_descriptors = descriptors
    if descriptors and pool is not None and pool.native_executor is not None:
        pool.require_current(state.task, state.program, state.quality, state.plan, state.evaluation)
        entries = [(descriptor, *_descriptor_settings(state, descriptor, maximum_virtual_bridge_nodes))
                   for descriptor in descriptors]
        prepared = pool.prepare_many(entries, virtual_sequence=state.virtual_sequence,
            split_sequence=state.split_sequence, allows_continue=budget.allows_search)
        count = sum(not isinstance(result, NumericDeferredCandidateFailure) and result.summary is not None
                    for attempts in prepared for _, result in attempts)
        remaining_descriptors = ()
    for descriptor in remaining_descriptors:
        attempts = []
        for workspace, result in _descriptor_attempts(state, budget, descriptor, maximum_virtual_bridge_nodes, pool):
            attempts.append((workspace, result))
            private_bytes += workspace.allocated_bytes
            if not isinstance(result, NumericDeferredCandidateFailure) and result.summary is not None:
                count += 1
            if not budget.allows_search():
                break
        prepared.append(attempts)
        if not budget.allows_search():
            prepared.extend([] for _ in range(len(descriptors) - len(prepared)))
            break
    if diagnostics is not None:
        elapsed = perf_counter() - started
        diagnostics.numeric_batch_prepare_seconds += elapsed
        diagnostics.maximum_numeric_batch_bytes = max(diagnostics.maximum_numeric_batch_bytes,
            private_bytes if pool is None else pool.allocated_bytes)
        diagnostics.maximum_numeric_batch_seconds = max(diagnostics.maximum_numeric_batch_seconds, elapsed)
        if count:
            diagnostics.numeric_batch_calls += 1
            diagnostics.record("numeric_precomputed", key, count)
    return prepared


def _try_descriptor(state, budget, descriptor, maximum_virtual_bridge_nodes,
                    diagnostics=None, diagnostic_key=None, prepared=None, pool=None):
    attempts = iter(prepared) if prepared is not None else _descriptor_attempts(
        state, budget, descriptor, maximum_virtual_bridge_nodes, pool)
    for position, (workspace, result) in enumerate(attempts):
        if position and not budget.consume_candidate_check():
            return False
        accepted = consume_candidate_result(state, budget, workspace, result)
        if diagnostics is not None:
            if prepared is not None and not isinstance(result, NumericDeferredCandidateFailure) and result.summary is not None:
                diagnostics.record("numeric_consumed", diagnostic_key)
            if accepted:
                diagnostics.record("plan_materializations", diagnostic_key)
        if accepted:
            return True
    return False


def _descriptor_family_stream(scan, columns, rules, family, cursor, lane, budget, diagnostics):
    key = f"{'critical' if lane else 'regular'}:{family}"
    for value in numeric_scan.family_stream(scan, columns, rules, family, cursor[family], lane, budget):
        if diagnostics is not None:
            for name in ("raw_combinations", "unique_combinations", "routed_combinations"):
                diagnostics.record(name, key)
        yield int(value[numeric_scan.OWNER]), value


def improve_numeric_refinement(
    state,
    budget,
    *,
    maximum_virtual_bridge_nodes=2,
    diagnostics=None,
    _batch_size=_SERIAL_BATCH_SIZE,
):
    """Alternate critical and regular action families until no improvement remains."""
    _validate_search_inputs(state.task, state.program, state.quality, state, budget)
    if type(maximum_virtual_bridge_nodes) is not int or not 0 <= maximum_virtual_bridge_nodes <= 2:
        raise NumericValueError(
            "maximum_virtual_bridge_nodes", "zero, one or two bridge nodes required"
        )
    if type(_batch_size) is not int or not 1 <= _batch_size <= _PROPOSALS_PER_FAMILY:
        raise NumericValueError("candidate_batch_size", "one to 64 descriptions required")
    if (not state.program.for_kind(NumericRuleKind.EARLIEST_START)
            and any(value.prohibited for value in state.evaluation.violations)):
        return state
    if budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE:
        budget.stop_reason = None
    cursors = [{family: None for family in _FAMILIES} for _ in range(2)]
    next_family, next_lane = [0, 0], 0
    first_cleanup = True
    while budget.allows_search():
        pool = NumericCandidateBatchWorkspace(state.task, state.program, state.quality,
            state.plan, state.evaluation, maximum_candidates=_batch_size, _native_executor="serial")
        columns, rules = task_columns(state.task), rule_tables(state.program.rules)
        scan = numeric_scan.build_scan(state, columns)
        streams = [
            [
                _descriptor_family_stream(scan, columns, rules, family, cursors[lane],
                    lane == 0, budget, diagnostics)
                for family in _FAMILIES[: _CRITICAL_FAMILY_COUNT if lane == 0 else -1]
            ]
            for lane in range(2)
        ]
        cleanup = numeric_scan.bounded(numeric_scan.reclaim(scan, state.task.nodes.role,
            state.task.nodes.purpose, state.task.nodes.split_group), budget)
        streams[1].append(cleanup)
        sizes = (_CRITICAL_FAMILY_COUNT, _REGULAR_FAMILY_COUNT)
        pending = [
            deque((next_family[lane] + offset) % size for offset in range(size))
            for lane, size in enumerate(sizes)
        ]
        accepted = False
        if first_cleanup:
            first_cleanup = False
            accepted, _ = _scan_family(
                state,
                budget,
                cleanup,
                maximum_virtual_bridge_nodes,
                diagnostics=diagnostics,
                diagnostic_key="regular:reclaim",
                _batch_size=_batch_size,
                _workspace=pool,
            )
        while not accepted and any(pending) and budget.allows_search():
            lane = next_lane if pending[next_lane] else 1 - next_lane
            family = pending[lane].popleft()
            family_name = _FAMILIES[family]
            cursor = None if family_name == "reclaim" else cursors[lane]
            accepted, exhausted = _scan_family(
                state,
                budget,
                streams[lane][family],
                maximum_virtual_bridge_nodes,
                diagnostics=diagnostics,
                diagnostic_key=f"{'critical' if lane == 0 else 'regular'}:{family_name}",
                cursor=cursor,
                family=family_name if cursor is not None else None,
                _batch_size=_batch_size,
                _workspace=pool,
            )
            next_family[lane] = (family + 1) % sizes[lane]
            next_lane = 1 - lane
            if not accepted and not exhausted:
                pending[lane].append(family)
        if not accepted:
            if budget.allows_search():
                budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
            return state
    return state


def run_numeric_serial_search(
    task,
    program,
    quality,
    initial,
    budget,
    *,
    pair_scan_slack_weight,
    maximum_virtual_bridge_nodes=2,
):
    """Run construction output through local search, split replay and refinement."""
    if not isinstance(budget, SolveRuntimeBudget):
        raise NumericValueError("budget", "shared runtime budget required")
    started = perf_counter()
    state = NumericSearchState.start(task, program, quality, initial)
    from ._numeric_stage_scheduler import run_numeric_stage_schedule

    diagnostics = NumericRefinementDiagnostics()
    report = run_numeric_stage_schedule(
        state, budget, pair_scan_slack_weight=pair_scan_slack_weight,
        maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes,
        diagnostics=diagnostics, batch_size=_SERIAL_BATCH_SIZE,
    )
    logger.info("numeric_refinement_summary %s", diagnostics.snapshot())
    logger.info("numeric_search_budget_summary %s", report.summary())
    return state, NumericSearchCheckpoint.capture(state.task, state, budget, started)
