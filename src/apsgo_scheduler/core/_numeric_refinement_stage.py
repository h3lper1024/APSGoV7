"""Phased refinement: exact compact cursors, bounded speculation and paid leases.

The original direct refinement entry remains available. This entry reuses its
settings, common batch evaluator and public consumer; it adds control, not an
alternative score, candidate policy, clock or borrow-admission predicate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter

import numpy as np

from . import _numeric_refinement as legacy
from . import _numeric_search as search
from . import _numeric_refinement_scan as scan
from ._numeric_refinement_cursor import FamilyCursor, MORE, ERROR, DONE
from ._numeric_stage_operators import NumericStageResult, _identity, _validate
from ._search_phase_budget import SearchAttemptIdentity, SearchPhaseExit
from .contracts import SearchStopReason

FAMILIES = (("intra", "node", "block"), ("intra", "node", "block", "cut", "order", "reclaim"))
FAMILY_QUANTUM = 64


class _PhaseYield(Exception):
    """Cooperative pre-charge exit; never manufactured numeric cancellation."""


@dataclass
class NumericRefinementProgress:
    binding: tuple | None = None
    options: tuple | None = None
    families: dict = field(default_factory=dict)
    last_owners: dict = field(default_factory=dict)
    next_family: list = field(default_factory=lambda: [0, 0])
    next_lane: int = 0
    first_cleanup: bool = True
    active: tuple | None = None
    remaining: int = FAMILY_QUANTUM
    # Only numeric description coordinates and remaining variant IDs, no views.
    pending: tuple | None = None
    complete: bool = False
    not_applicable: bool = False
    revision: int = 0
    force_lazy: bool = False

    def bind(self, state, options):
        current = _identity(state)
        if current == self.binding and options != self.options:
            raise ValueError("same-generation refinement options changed")
        if current != self.binding:
            self.binding, self.options = current, options
            self.families.clear()
            self.pending = self.active = None
            self.remaining = FAMILY_QUANTUM
            self.complete = self.not_applicable = self.force_lazy = False
            self.revision += 1

    def complete_for(self, state):
        return self.complete and self.binding == _identity(state)

    def prepare_families(self, x):
        if self.families:
            return
        for lane, names in enumerate(FAMILIES):
            for name in names:
                key = (lane, name)
                owners = tuple(map(int, x.sources))
                previous = self.last_owners.get(key)
                if previous in owners:
                    position = owners.index(previous) + 1
                    owners = owners[position:] + owners[:position]
                self.families[key] = FamilyCursor(name, lane == 0, owners)

    def advance_family(self):
        if self.active is None:
            return
        lane, name = self.active
        if self.first_cleanup:
            self.first_cleanup = False
        else:
            self.next_family[lane] = (FAMILIES[lane].index(name) + 1) % len(FAMILIES[lane])
            self.next_lane = 1 - lane
        self.active = None
        self.remaining = FAMILY_QUANTUM
        self.revision += 1

    def select_family(self):
        if self.pending is not None:
            self.active = self.pending[0]
            return self.active
        if self.active is not None:
            if self.remaining and not self.families[self.active].complete:
                return self.active
            self.advance_family()
        if self.first_cleanup:
            key = (1, "reclaim")
            if not self.families[key].complete:
                self.active = key
                return key
            self.first_cleanup = False
        for lane in (self.next_lane, 1 - self.next_lane):
            names = FAMILIES[lane]
            for offset in range(len(names)):
                key = (lane, names[(self.next_family[lane] + offset) % len(names)])
                if not self.families[key].complete:
                    self.active = key
                    return key
        self.complete = True
        return None


def refinement_applicable(state):
    return (bool(state.program.for_kind(search.NumericRuleKind.EARLIEST_START))
            or not any(v.prohibited for v in state.evaluation.violations))


def _guard(state, binding, phase):
    if _identity(state) != binding:
        raise ValueError("refinement preparation identity changed")
    if not phase.allows_new_work():
        raise _PhaseYield
    return True


def _attempt_identity(state, sequence):
    return SearchAttemptIdentity(state.task.fingerprint, state.plan.fingerprint,
                                 state.plan.generation, sequence)


def _record(diag, name, key, count=1):
    if diag is not None and count:
        diag.record(name, key, count)


def _started(progress, key, descriptor, variants, counted):
    if not counted:
        progress.remaining -= 1
        if int(descriptor[scan.OWNER]) >= 0:
            progress.last_owners[key] = int(descriptor[scan.OWNER])
    progress.pending = (key, tuple(map(int, descriptor)), tuple(variants))
    progress.revision += 1


def _consume(state, budget, attempt, result, workspace, diagnostics, key, precomputed):
    def continuation():
        return attempt.allows_continue(_attempt_identity(state, budget.candidate_check_count))
    before = state.complete_candidate_evaluation_count
    accepted = search.consume_candidate_result(state, budget, workspace, result,
                                                allows_continue=continuation)
    _record(diagnostics, "candidate_checks", key)
    _record(diagnostics, "complete_evaluations", key, state.complete_candidate_evaluation_count - before)
    if precomputed and not isinstance(result, search.NumericDeferredCandidateFailure) and result.summary is not None:
        _record(diagnostics, "numeric_consumed", key)
    if accepted:
        _record(diagnostics, "accepted", key)
        _record(diagnostics, "plan_materializations", key)
    return accepted


def _remaining_variants(result, variants):
    if isinstance(result, search.NumericDeferredCandidateFailure):
        # Consumption raises this failure; this branch cannot authorize later work.
        return ()
    variant = int(result.descriptor[scan.VARIANT])
    if variant not in variants:
        raise ValueError("candidate batch changed the declared repair variant")
    return tuple(variants[variants.index(variant) + 1:])


def _consume_prepared(state, budget, phase, progress, family_key, descriptor, variants,
                      attempts, counted, diagnostics, key):
    started = counted
    for workspace, result in attempts:
        with phase.candidate_attempt(_attempt_identity(state, budget.candidate_check_count + 1)) as attempt:
            if not attempt.granted:
                return False, False, started
            _started(progress, family_key, descriptor, variants, started)
            started = True
            remaining_variants = _remaining_variants(result, variants)
            accepted = _consume(state, budget, attempt, result, workspace, diagnostics, key, True)
            variants = remaining_variants
            progress.pending = None if not variants else (family_key, tuple(map(int, descriptor)), variants)
        if accepted:
            progress.pending = None
            return True, True, True
    if variants:
        raise ValueError("batch omitted an unconsumed repair variant")
    progress.pending = None
    return False, True, started


def _consume_lazy(state, budget, phase, progress, family_key, descriptor, policy, variants,
                  pool, counted, diagnostics, key):
    """One remaining credit: don't speculate two variants against one slot.

    First logical check is charged before the first result. Further variants are
    prepared before their original positive charge, but new-work polling prevents
    unbounded unpaid preparation. A nonexistent cleaned variant remains uncharged.
    """
    binding = _identity(state)
    callback = [lambda: _guard(state, binding, phase)]
    stream = pool.attempts(descriptor, policy, variants,
        virtual_sequence=state.virtual_sequence, split_sequence=state.split_sequence,
        allows_continue=lambda: callback[0](), capture=search.capture_candidate_result)
    started, first = counted, not counted
    try:
        while variants:
            result_pair = None
            if not first:
                _guard(state, binding, phase)
                callback[0] = lambda: _guard(state, binding, phase)
                result_pair = next(stream, None)
                if result_pair is None:
                    raise ValueError("missing original repair attempt")
            with phase.candidate_attempt(_attempt_identity(state, budget.candidate_check_count + 1)) as attempt:
                if not attempt.granted:
                    return False, False, started
                _started(progress, family_key, descriptor, variants, started)
                started = True
                callback[0] = lambda: attempt.allows_continue(
                    _attempt_identity(state, budget.candidate_check_count))
                if first:
                    result_pair = next(stream, None)
                    if result_pair is None:
                        raise ValueError("a description must have its original attempt")
                workspace, result = result_pair
                remaining_variants = _remaining_variants(result, variants)
                accepted = _consume(state, budget, attempt, result, workspace, diagnostics, key, False)
                variants = remaining_variants
                progress.pending = None if not variants else (family_key, tuple(map(int, descriptor)), variants)
                first = False
            if accepted:
                progress.pending = None
                return True, True, True
        return False, True, started
    except _PhaseYield:
        return False, False, started
    finally:
        stream.close()


def _collect(state, phase, binding, progress, family_key, bank, x, columns, rules,
             pool, bridges, diagnostics, key, entries):
    """Prefetch at most the worst-case number of still affordable variants."""
    if progress.pending is not None:
        _, coordinates, variants = progress.pending
        descriptor = np.array(coordinates, np.int64)
        policy, _ = legacy._descriptor_settings(state, descriptor, bridges)
        return [(descriptor, policy, variants, 0, True)]
    work = 0
    maximum = min(pool.maximum_candidates, progress.remaining, 1 if progress.force_lazy else FAMILY_QUANTUM)
    while len(entries) < maximum:
        marker = len(bank.journal)
        while True:
            _guard(state, binding, phase)
            value = bank.step(x, columns, rules, state.task.nodes.role,
                              state.task.nodes.purpose, state.task.nodes.split_group)
            action = int(value[scan.ACTION])
            if action == ERROR:
                raise search.NumericValueError("refinement.scan", "integer scan overflow")
            if action != MORE:
                break
            if not entries and bank.journal:
                progress.revision += 1
                bank.commit()
        if action == DONE:
            break
        policy, variants = legacy._descriptor_settings(state, value, bridges)
        available = phase.remaining_candidate_checks
        if entries and work + len(variants) > available:
            bank.rollback(marker)
            break
        for counter in ("raw_combinations", "unique_combinations", "routed_combinations"):
            _record(diagnostics, counter, key)
        entries.append((value, policy, tuple(variants), len(bank.journal), False))
        work += len(variants)
        if work >= available:
            break
    return entries


def _family_batch(state, budget, phase, progress, family_key, x, columns, rules,
                  pool, bridges, diagnostics):
    bank = progress.families[family_key]
    key = f"{'critical' if family_key[0] == 0 else 'regular'}:{family_key[1]}"
    binding, acknowledged = _identity(state), 0
    entries = []
    prefetched = consumed = 0
    accepted = all_consumed = False
    # Previous calls must have either committed or rolled back their journal.
    if bank.journal:
        raise ValueError("unsettled refinement prefetch checkpoint")
    try:
        entries = _collect(state, phase, binding, progress, family_key, bank,
                           x, columns, rules, pool, bridges, diagnostics, key, entries)
        if not entries:
            all_consumed = True
            return False
        _record(diagnostics, "batch_prepared", key, len(entries))
        if diagnostics is not None:
            diagnostics.maximum_batch_size = max(diagnostics.maximum_batch_size, len(entries))
        lazy = progress.force_lazy or sum(len(item[2]) for item in entries) > phase.remaining_candidate_checks
        prepared = None
        if not lazy:
            started = perf_counter()
            try:
                prepared = pool.prepare_many([(d, p, v) for d, p, v, _, _ in entries],
                    virtual_sequence=state.virtual_sequence, split_sequence=state.split_sequence,
                    allows_continue=lambda: _guard(state, binding, phase))
            finally:
                if diagnostics is not None:
                    elapsed = perf_counter() - started
                    diagnostics.numeric_batch_prepare_seconds += elapsed
                    diagnostics.maximum_numeric_batch_seconds = max(diagnostics.maximum_numeric_batch_seconds, elapsed)
                    diagnostics.maximum_numeric_batch_bytes = max(diagnostics.maximum_numeric_batch_bytes, pool.allocated_bytes)
            if len(prepared) != len(entries):
                raise ValueError("batch result/description length mismatch")
            prefetched = sum(not isinstance(r, search.NumericDeferredCandidateFailure) and r.summary is not None
                             for attempts in prepared for _, r in attempts)
            _record(diagnostics, "numeric_precomputed", key, prefetched)
            if diagnostics is not None and prefetched:
                diagnostics.numeric_batch_calls += 1
        for index, (descriptor, policy, variants, marker, counted) in enumerate(entries):
            before_consumed = diagnostics.numeric_consumed.get(key, 0) if diagnostics is not None else 0
            if lazy:
                accepted, finished, begun = _consume_lazy(state, budget, phase, progress, family_key,
                    descriptor, policy, variants, pool, counted, diagnostics, key)
            else:
                accepted, finished, begun = _consume_prepared(state, budget, phase, progress, family_key,
                    descriptor, variants, prepared[index], counted, diagnostics, key)
            if diagnostics is not None:
                consumed += diagnostics.numeric_consumed.get(key, 0) - before_consumed
            if begun:
                progress.force_lazy = False
                acknowledged = marker
                if not counted:
                    _record(diagnostics, "batch_consumed", key)
            if finished:
                _record(diagnostics, "batch_accepted" if accepted else "batch_rejected", key)
            if accepted:
                _record(diagnostics, "batch_stale", key, len(entries) - index - 1)
                progress.advance_family()
                return True
            if not finished:
                if not begun:
                    progress.force_lazy = True
                    progress.revision += 1
                _record(diagnostics, "batch_stopped", key, len(entries) - index - int(begun))
                return False
        all_consumed = True
        return False
    except _PhaseYield:
        # Raw scans with no proposal can retain their coordinates. If speculative
        # evaluation used the time slice, retry its first description lazily next
        # time: a paid bounded attempt may finish past the soft deadline.
        all_consumed = not entries
        _record(diagnostics, "batch_stopped", key, len(entries))
        if budget.stop_reason is SearchStopReason.USER_CANCELLED:
            _record(diagnostics, "batch_cancelled", key, len(entries))
        if entries:
            progress.force_lazy = True
            progress.revision += 1
        return False
    finally:
        if not all_consumed:
            bank.rollback(acknowledged)
        if bank.journal:
            progress.revision += 1
        bank.commit()
        _record(diagnostics, "numeric_discarded", key, prefetched - consumed)
        pool.release()


def run_refinement_slice(state, budget, *, candidate_checks, time_slice_seconds, progress,
                         maximum_virtual_bridge_nodes=2, diagnostics=None, batch_size=8,
                         name="refinement"):
    """Resume existing action families; soft yield never writes a global stop."""
    _validate(state, budget, 0, maximum_virtual_bridge_nodes)
    if not isinstance(progress, NumericRefinementProgress):
        raise ValueError("NumericRefinementProgress required")
    if type(batch_size) is not int or not 1 <= batch_size <= FAMILY_QUANTUM:
        raise ValueError("one to 64 descriptions required")
    progress.bind(state, (maximum_virtual_bridge_nodes, batch_size))
    before = state.plan.generation
    with budget.phase_scope(name, candidate_checks, time_slice_seconds) as phase:
        if progress.complete_for(state):
            phase.finish(SearchPhaseExit.NOT_APPLICABLE if progress.not_applicable else SearchPhaseExit.SCAN_COMPLETE)
        elif not refinement_applicable(state):
            progress.complete = progress.not_applicable = True
            phase.finish(SearchPhaseExit.NOT_APPLICABLE)
        else:
            while phase.allows_new_work():
                columns, rules = legacy.task_columns(state.task), legacy.rule_tables(state.program.rules)
                x = scan.build_scan(state, columns)
                progress.prepare_families(x)
                pool = legacy.NumericCandidateBatchWorkspace(state.task, state.program, state.quality,
                    state.plan, state.evaluation, maximum_candidates=batch_size, _native_executor="serial")
                binding = _identity(state)
                try:
                    while phase.allows_new_work():
                        family = progress.select_family()
                        if family is None:
                            phase.finish()
                            break
                        accepted = _family_batch(state, budget, phase, progress, family,
                            x, columns, rules, pool, maximum_virtual_bridge_nodes, diagnostics)
                        if accepted:
                            if _identity(state) == binding:
                                raise ValueError("accepted refinement did not advance the state")
                            progress.bind(state, (maximum_virtual_bridge_nodes, batch_size))
                            break
                        if progress.families[family].complete and progress.pending is None:
                            progress.advance_family()
                    if progress.complete_for(state):
                        break
                finally:
                    pool.release()
    return NumericStageResult(phase.release_unused(), (), before, state.plan.generation,
                              progress.complete_for(state))
