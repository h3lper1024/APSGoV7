"""One shared search allowance and an independent, bounded finalization window."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite
from time import monotonic
from typing import Protocol

from .contracts import SearchStopReason, SolverPolicy, require_enum, require_int, sum_decimals


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool:
        """Read cancellation without side effects or blocking."""
        ...


def _finite_time(value, name: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite int or float")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be finite") from error
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(slots=True)
class SolveRuntimeBudget:
    started_at_monotonic: float
    search_deadline_monotonic: float
    final_deadline_monotonic: float
    candidate_check_limit: int
    candidate_check_count: int
    cancellation: CancellationToken | None
    stop_reason: SearchStopReason | None = None
    clock: Callable[[], float] = field(default=monotonic, repr=False, compare=False, kw_only=True)

    def __post_init__(self):
        for name in (
            "started_at_monotonic",
            "search_deadline_monotonic",
            "final_deadline_monotonic",
        ):
            setattr(self, name, _finite_time(getattr(self, name), name))
        if (
            not self.started_at_monotonic
            < self.search_deadline_monotonic
            < self.final_deadline_monotonic
        ):
            raise ValueError("deadlines must satisfy started_at < search_deadline < final_deadline")
        require_int(self.candidate_check_limit, "candidate_check_limit")
        require_int(self.candidate_check_count, "candidate_check_count")
        if self.candidate_check_count > self.candidate_check_limit:
            raise ValueError("candidate check count must not exceed its limit")
        if self.stop_reason is not None:
            require_enum(self.stop_reason, SearchStopReason, "stop_reason")
        if not callable(self.clock):
            raise ValueError("clock must be callable")
        if self.cancellation is not None and not callable(
            getattr(self.cancellation, "is_cancelled", None)
        ):
            raise ValueError("cancellation must provide a callable is_cancelled")

    @classmethod
    def from_policy(
        cls,
        policy: SolverPolicy,
        started_at_monotonic: float,
        cancellation: CancellationToken | None = None,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> SolveRuntimeBudget:
        if not isinstance(policy, SolverPolicy):
            raise ValueError("policy must be SolverPolicy")
        started_at = _finite_time(started_at_monotonic, "started_at_monotonic")
        total = _finite_time(float(policy.total_time_limit_seconds), "total_time_limit_seconds")
        search_seconds = float(
            sum_decimals(
                (policy.total_time_limit_seconds, policy.finalization_reserve_seconds.copy_negate())
            )
        )
        return cls(
            started_at,
            started_at + search_seconds,
            started_at + total,
            policy.candidate_check_limit,
            0,
            cancellation,
            clock=clock,
        )

    @property
    def must_stop(self) -> bool:
        return self.stop_reason is not None

    def _cancellation_is_requested(self) -> bool:
        if self.cancellation is None:
            return False
        cancelled = self.cancellation.is_cancelled()
        if type(cancelled) is not bool:
            raise ValueError("is_cancelled must return bool")
        return cancelled

    def permit(self, count: int = 1) -> bool:
        require_int(count, "count")
        if self.must_stop:
            return False
        if self.candidate_check_count + count > self.candidate_check_limit:
            self.stop_reason = SearchStopReason.CANDIDATE_LIMIT_REACHED
            return False
        if self._cancellation_is_requested():
            self.stop_reason = SearchStopReason.USER_CANCELLED
            return False
        if _finite_time(self.clock(), "clock()") >= self.search_deadline_monotonic:
            self.stop_reason = SearchStopReason.SEARCH_TIME_LIMIT_REACHED
            return False
        self.candidate_check_count += count
        return True

    def consume_candidate_check(self) -> bool:
        return self.permit(1)

    def allows_search(self) -> bool:
        return self.permit(0)

    def allows_finalization(self) -> bool:
        if self.stop_reason in {
            SearchStopReason.USER_CANCELLED,
            SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
            SearchStopReason.INPUT_INVALID,
            SearchStopReason.SYSTEM_ERROR,
        }:
            return False
        if self._cancellation_is_requested():
            self.stop_reason = SearchStopReason.USER_CANCELLED
            return False
        if _finite_time(self.clock(), "clock()") >= self.final_deadline_monotonic:
            self.stop_reason = SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
            return False
        return True
