"""Opt-in search scopes over one global ledger; no scheduling or solver imports.

New work polls the phase. A candidate attempt replaces one positive permit at
its original charging point. Its continuation ignores only soft phase limits,
never global cancellation/deadlines. SB2/SB3 own solver wiring and allocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .budget import SolveRuntimeBudget, _finite_time
from .contracts import SearchStopReason, require_enum, require_int, require_text


class SearchPhaseExit(str, Enum):
    SCAN_COMPLETE = "scan_complete"
    CANDIDATE_SLICE_EXHAUSTED = "candidate_slice_exhausted"
    TIME_SLICE_EXHAUSTED = "time_slice_exhausted"
    NOT_APPLICABLE = "not_applicable"
    INSUFFICIENT_BUDGET = "insufficient_budget"
    GLOBAL_STOP = "global_stop"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class SearchAttemptIdentity:
    task_fingerprint: str
    plan_fingerprint: str
    generation: int
    sequence: int

    def __post_init__(self):
        require_text(self.task_fingerprint, "task_fingerprint")
        require_text(self.plan_fingerprint, "plan_fingerprint")
        require_int(self.generation, "generation")
        require_int(self.sequence, "sequence", minimum=1)


@dataclass(frozen=True, slots=True)
class SearchPhaseResult:
    name: str
    parent_name: str | None
    candidate_limit: int
    count_at_entry: int
    count_at_exit: int
    started_at: float
    soft_deadline: float
    ended_at: float | None
    reason: SearchPhaseExit
    global_stop_reason: SearchStopReason | None
    failed: bool

    @property
    def used_candidate_checks(self) -> int:
        return self.count_at_exit - self.count_at_entry

    @property
    def unused_candidate_checks(self) -> int:
        return self.candidate_limit - self.used_candidate_checks

    @property
    def elapsed_seconds(self) -> float | None:
        return None if self.ended_at is None else self.ended_at - self.started_at

    @property
    def unused_time_seconds(self) -> float:
        # Unknown or failed cleanup cannot mint time for a later phase.
        if self.ended_at is None or self.failed:
            return 0.0
        return max(0.0, self.soft_deadline - self.ended_at)

    @property
    def soft_overrun_seconds(self) -> float | None:
        return None if self.ended_at is None else max(0.0, self.ended_at - self.soft_deadline)


class SearchPhaseBudget:
    """Single-use, LIFO scope. Children consume their parent's existing allowance.

    Allocations are clipped on entry, not added to the global limit. A closed
    result is frozen before sibling work can affect its counts. release_unused
    claims that result once; it does NOT credit a pool or refund charged work.
    The future allocator must not credit child and parent remainders twice.
    """

    def __init__(self, budget: SolveRuntimeBudget, name: str,
                 candidate_checks: int, time_slice_seconds: float):
        if not isinstance(budget, SolveRuntimeBudget):
            raise ValueError("one shared SolveRuntimeBudget required")
        require_text(name, "phase_name")
        require_int(candidate_checks, "phase_candidate_checks")
        seconds = _finite_time(time_slice_seconds, "phase_time_slice_seconds")
        if seconds < 0:
            raise ValueError("phase_time_slice_seconds must be nonnegative")
        self.budget, self.name = budget, name
        self.requested_checks, self.requested_seconds = candidate_checks, seconds
        self.parent: SearchPhaseBudget | None = None
        self.candidate_limit = self.count_at_entry = 0
        self.started_at = self.soft_deadline = 0.0
        self._entered = self._closed = self._released = False
        self._reason: SearchPhaseExit | None = None
        self._attempt: SearchCandidateAttempt | None = None
        self._result: SearchPhaseResult | None = None

    def __enter__(self) -> SearchPhaseBudget:
        if self._entered:
            raise ValueError("phase scope cannot be reentered")
        parent = self.budget._active_phase
        if parent is not None and (parent._attempt is not None or parent._reason is not None):
            raise ValueError("cannot enter a child of a busy or finished phase")
        now = _finite_time(self.budget.clock(), "clock()")
        deadline = _finite_time(now + self.requested_seconds, "phase_deadline")
        available = self.budget.candidate_check_limit - self.budget.candidate_check_count
        if parent is not None:
            available = min(available, parent.remaining_candidate_checks)
            deadline = min(deadline, parent.soft_deadline)
        self.parent = parent
        self.candidate_limit = min(self.requested_checks, available)
        self.count_at_entry = self.budget.candidate_check_count
        self.started_at = now
        self.soft_deadline = min(deadline, self.budget.search_deadline_monotonic)
        self._entered = True
        self.budget._active_phase = self
        return self

    def _require_active(self) -> None:
        if not self._entered or self._closed or self.budget._active_phase is not self:
            raise ValueError("the current active phase is required")

    @property
    def remaining_candidate_checks(self) -> int:
        self._require_active()
        available = self.budget.candidate_check_limit - self.budget.candidate_check_count
        phase: SearchPhaseBudget | None = self
        while phase is not None:
            used = self.budget.candidate_check_count - phase.count_at_entry
            available = min(available, phase.candidate_limit - used)
            phase = phase.parent
        return max(0, available)

    def _allows_new(self, count: int) -> bool:
        self._require_active()
        if self._attempt is not None:
            raise ValueError("close the current attempt before requesting new work")
        # Real stops take precedence even after a local slice was exhausted.
        if not self.budget._permit_global(0):
            self._reason = SearchPhaseExit.GLOBAL_STOP
            return False
        if self.budget.candidate_check_count + count > self.budget.candidate_check_limit:
            self.budget.stop_reason = SearchStopReason.CANDIDATE_LIMIT_REACHED
            self._reason = SearchPhaseExit.GLOBAL_STOP
            return False
        if self._reason is not None:
            return False
        if count > self.remaining_candidate_checks:
            self._reason = (SearchPhaseExit.INSUFFICIENT_BUDGET if self.candidate_limit == 0
                            else SearchPhaseExit.CANDIDATE_SLICE_EXHAUSTED)
            return False
        now = _finite_time(self.budget.clock(), "clock()")
        if now >= self.budget.search_deadline_monotonic:
            self.budget.stop_reason = SearchStopReason.SEARCH_TIME_LIMIT_REACHED
            self._reason = SearchPhaseExit.GLOBAL_STOP
            return False
        if now >= self.soft_deadline:
            self._reason = SearchPhaseExit.TIME_SLICE_EXHAUSTED
            return False
        return True

    def allows_new_work(self) -> bool:
        """Poll enumeration/new work without charging; not an in-flight callback."""
        return self._allows_new(1)

    def _permit(self, count: int) -> bool:
        if not self._allows_new(count):
            return False
        # Keep the original time/cancellation check at the actual charging point.
        if not self.budget._permit_global(count):
            self._reason = SearchPhaseExit.GLOBAL_STOP
            return False
        return True

    def candidate_attempt(self, identity: SearchAttemptIdentity) -> SearchCandidateAttempt:
        """One context replaces ONE original consume_candidate_check(), not two."""
        return SearchCandidateAttempt(self, identity)

    def finish(self, reason: SearchPhaseExit = SearchPhaseExit.SCAN_COMPLETE) -> None:
        """Declare coverage explicitly; merely leaving a scope never means convergence."""
        self._require_active()
        require_enum(reason, SearchPhaseExit, "phase_exit_reason")
        if reason not in {SearchPhaseExit.SCAN_COMPLETE, SearchPhaseExit.NOT_APPLICABLE,
                          SearchPhaseExit.INSUFFICIENT_BUDGET}:
            raise ValueError("budget exits are reported by the budget, not the caller")
        if self._attempt is not None or self._reason is not None:
            raise ValueError("cannot finish an active attempt or overwrite a phase exit")
        if reason is not SearchPhaseExit.SCAN_COMPLETE and self.budget.candidate_check_count != self.count_at_entry:
            raise ValueError("a skipped phase cannot have charged candidates")
        self._reason = reason

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self._require_active()
        leaked_attempt = self._attempt is not None
        if leaked_attempt:
            self._attempt._close()
        # Restore the stack even if the clock/token raises during exit polling.
        self.budget._active_phase = self.parent
        self._closed = True
        ended_at, cleanup_error = None, None
        try:
            self.budget._permit_global(0)
            ended_at = _finite_time(self.budget.clock(), "clock()")
            if ended_at < self.started_at:
                ended_at = None
                raise ValueError("phase clock must not move backwards")
        except Exception as error:
            cleanup_error = error
        failed = exc_type is not None or cleanup_error is not None or leaked_attempt
        reason = self._reason or SearchPhaseExit.INCOMPLETE
        if failed:
            reason = SearchPhaseExit.INCOMPLETE
        if self.budget.must_stop:
            reason = SearchPhaseExit.GLOBAL_STOP
        self._result = SearchPhaseResult(
            self.name, None if self.parent is None else self.parent.name,
            self.candidate_limit, self.count_at_entry, self.budget.candidate_check_count,
            self.started_at, self.soft_deadline, ended_at, reason, self.budget.stop_reason, failed)
        if exc_type is None:
            if cleanup_error is not None:
                raise cleanup_error
            if leaked_attempt:
                raise ValueError("candidate attempt must close before its phase")
        return False

    @property
    def result(self) -> SearchPhaseResult:
        if self._result is None:
            raise ValueError("phase result requires a closed scope")
        return self._result

    def release_unused(self) -> SearchPhaseResult:
        """Claim unused allowance once. Parent/child amounts are not additive."""
        result = self.result
        if self._released:
            raise ValueError("unused phase allowance was already released")
        self._released = True
        return result


class SearchCandidateAttempt:
    """One charged attempt bound to its phase, candidate identity and generation.

    The caller passes the CURRENT identity to each continuation check. Closing
    the context invalidates the lease even after rejection or an exception.
    This does not authorize acceptance: quality, identity and FB2 checks remain.
    """

    def __init__(self, phase: SearchPhaseBudget, identity: SearchAttemptIdentity):
        if not isinstance(identity, SearchAttemptIdentity):
            raise ValueError("SearchAttemptIdentity required")
        self.phase, self.identity = phase, identity
        self.granted = False
        self._entered = self._closed = False

    def __enter__(self) -> SearchCandidateAttempt:
        if self._entered:
            raise ValueError("candidate attempt cannot be reentered")
        self.phase._require_active()
        if self.phase._attempt is not None:
            raise ValueError("candidate attempts cannot be nested")
        self._entered = True
        self.granted = self.phase.budget.consume_candidate_check()
        if self.granted:
            self.phase._attempt = self
        return self

    def allows_continue(self, current_identity: SearchAttemptIdentity) -> bool:
        if not isinstance(current_identity, SearchAttemptIdentity) or current_identity != self.identity:
            raise ValueError("candidate identity or generation changed")
        if not self.granted or self._closed or self.phase._attempt is not self:
            return False
        self.phase._require_active()
        return self.phase.budget._permit_global(0)

    def _close(self) -> None:
        if self.phase._attempt is self:
            self.phase._attempt = None
        self._closed = True

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self._close()
        return False
