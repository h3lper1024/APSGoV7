"""Bounded integer envelopes of the Decimal clock; never an acceptance authority."""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from threading import Lock
from time import perf_counter

import numpy as np
from numba import get_num_threads, njit, prange, set_num_threads, threading_layer

from .delivery_timing import score_seconds_to_hours, _context
from .rules.base import NumericProjection, QualityAggregation, QualityDirection

_SCALE = 10**9  # Clock bounds only, not a replacement for production timestamps.
_LIMIT = 2**60
_launch_lock = Lock()  # Workqueue cannot launch concurrently; busy requests use exact evaluation.
_METRICS = ("old_backlog_last_completion_hours", "delivery_wait_tardiness_tonne_hours")


@njit(cache=False, fastmath=False, boundscheck=True)
def _seconds(ticks):
    quotient, remainder = divmod(ticks * 3600, _SCALE)
    return quotient + (remainder * 2 > _SCALE or (remainder * 2 == _SCALE and quotient % 2 != 0))


def _clock_bounds(sequences, durations, owners, dues, weights, output):
    for candidate in prange(len(sequences)):
        finishes = np.zeros((len(weights), 2), dtype=np.int64)
        lower, upper = 0, 0
        for position in range(sequences.shape[1]):
            row = sequences[candidate, position]
            # One extra tick encloses rounding of each original Decimal addition.
            lower = max(0, lower + durations[row, 0] - 1)
            upper += durations[row, 1] + 1
            owner = owners[row]
            if owner >= 0:
                finishes[owner, 0], finishes[owner, 1] = lower, upper
        clearance_low, clearance_high, burden_low, burden_high = 0, 0, 0, 0
        for owner in range(len(weights)):
            lower, upper = finishes[owner, 0], finishes[owner, 1]
            if dues[owner, 1] <= 0:
                clearance_low = max(clearance_low, lower)
                clearance_high = max(clearance_high, upper)
            # max(0, finish - max(0, due)), including Decimal subtraction rounding.
            wait_low = max(0, lower - max(0, dues[owner, 1]) - 1)
            wait_high = max(0, upper - max(0, dues[owner, 0]) + 1)
            burden_low += _seconds(wait_low) * weights[owner]
            burden_high += _seconds(wait_high) * weights[owner]
        output[candidate, 0] = _seconds(clearance_low)
        output[candidate, 1] = _seconds(clearance_high)
        output[candidate, 2] = burden_low
        output[candidate, -1] = burden_high


_serial_bounds = njit(cache=False, fastmath=False, boundscheck=True)(_clock_bounds)
_parallel_bounds = njit(cache=False, fastmath=False, boundscheck=True, parallel=True)(_clock_bounds)


def _ticks(value):
    value = Fraction(value) * _SCALE
    return value.numerator // value.denominator, -(-value.numerator // value.denominator)


@dataclass(frozen=True, slots=True)
class DeliveryBounds:
    source: object
    chains: tuple
    metrics: tuple
    timing: object

    def matches(self, plan, state, timing):
        return (self.source is state.current_plan and self.timing is timing and len(plan.chains) == len(self.chains)
                and all(a is b for a, b in zip(plan.chains, self.chains)))

    def rejects(self, criteria, current):
        for key, (lower, upper), criterion, target in zip(_METRICS, self.metrics, criteria, current):
            if (criterion.metric_key != key or criterion.direction is not QualityDirection.MINIMIZE
                    or criterion.aggregation is not QualityAggregation.SUM
                    or criterion.numeric_projection is not NumericProjection.EXACT_DECIMAL):
                return False
            if lower > target:
                return True
            if not lower == upper == target:
                return False
        return False  # Later quality levels still own tie-breaking.


class ChainOrderBatch:
    """Current-plan-only preparation and bounded results; no generator, budget or cache writes."""

    def __init__(self, previous):
        self.previous = previous
        self.nodes = previous.delivery.nodes
        timing = previous.delivery.timing
        orders = tuple(timing.orders)
        order_rows = {key: i for i, key in enumerate(orders)}
        row_by_identity = {id(node): i for i, node in enumerate(self.nodes)}
        self.chain_rows = {id(chain): tuple(row_by_identity[id(node)] for node in chain.nodes)
                           for chain in previous.plan.chains}
        if any(value != 0 and not -100 <= value.adjusted() <= 6 for value in previous.delivery.durations):
            raise _Unsupported("duration_range")
        durations = tuple(_ticks(value) for value in previous.delivery.durations)
        dues = tuple(_ticks(timing.orders[key].due_hours) for key in orders)
        weights = tuple(timing.orders[key].weight for key in orders)
        exponent = max(0, max(-value.as_tuple().exponent for value in weights))
        if exponent > 6:
            raise _Unsupported("weight_scale")
        self.weight_scale = 10**exponent
        integers = tuple(Fraction(value) * self.weight_scale for value in weights)
        if any(value.denominator != 1 for value in integers):
            raise _Unsupported("weight_scale")
        integers = tuple(int(value) for value in integers)
        clock_max = sum(upper + 1 for _, upper in durations) + len(self.nodes) + 2
        # Original Decimal values stay far from overflow/underflow. A 28-digit
        # rounding error below this clock bound is strictly smaller than 1e-9 h.
        seconds_max = (clock_max * 3600 + _SCALE - 1) // _SCALE
        if (clock_max * 3600 >= _LIMIT or seconds_max * sum(integers) >= _LIMIT
                or any(abs(value) >= _LIMIT for pair in dues for value in pair)):
            raise _Unsupported("integer_range")
        self.durations = np.asarray(durations, dtype=np.int64)
        self.dues = np.asarray(dues, dtype=np.int64)
        self.weights = np.asarray(integers, dtype=np.int64)
        self.owners = np.asarray([order_rows[node.source_order_id] if node.virtual_lineage is None else -1
                                  for node in self.nodes], dtype=np.int64)
        for array in (self.durations, self.dues, self.weights, self.owners):
            array.flags.writeable = False

    @classmethod
    def prepare(cls, previous, state, cache):
        if (previous is None or not previous.matches(state, cache.rule_set, cache.context)
                or previous.delivery is None or not previous.delivery.second_precision):
            return None
        try:
            return cls(previous)
        except _Unsupported:
            return None

    def compute(self, candidates, threads, statistics):
        if not 0 < len(candidates) <= 32 or type(threads) is not int or threads not in (1, 2, 4, 8):
            raise ValueError("delivery batch requires 1..32 candidates and 1/2/4/8 threads")
        started = perf_counter()
        expected = {id(chain) for chain in self.previous.plan.chains}
        if any(len(chains) != len(expected) or {id(chain) for chain in chains} != expected for chains in candidates):
            raise ValueError("delivery batch must preserve actual whole chains")
        sequences = np.asarray([[row for chain in chains for row in self.chain_rows[id(chain)]]
                                for chains in candidates], dtype=np.int64)
        sequences.flags.writeable = False
        output = np.empty((len(candidates), 4), dtype=np.int64)
        statistics["peak_input_output_bytes"] = max(statistics.get("peak_input_output_bytes", 0),
            sequences.nbytes + output.nbytes + sum(array.nbytes for array in
                                                   (self.durations, self.dues, self.weights, self.owners)))
        statistics["preparation_seconds"] = statistics.get("preparation_seconds", 0.0) + perf_counter() - started
        if not _launch_lock.acquire(blocking=False):
            statistics["busy_fallbacks"] = statistics.get("busy_fallbacks", 0) + 1
            return ()
        kernel = _serial_bounds if threads == 1 else _parallel_bounds
        cold = not kernel.signatures
        started = perf_counter()
        old_threads = None
        try:
            # Numba's mask is thread-local; restore the caller's mask even on failure.
            old_threads = get_num_threads()
            set_num_threads(threads)
            kernel(sequences, self.durations, self.owners, self.dues, self.weights, output)
            if threads > 1:
                statistics["threading_layer"] = threading_layer()
        finally:
            try:
                if old_threads is not None:
                    set_num_threads(old_threads)
            finally:
                _launch_lock.release()
        key = "compile_and_first_call_seconds" if cold else "kernel_seconds"
        elapsed = perf_counter() - started
        statistics[key] = statistics.get(key, 0.0) + elapsed
        statistics["max_kernel_call_seconds"] = max(statistics.get("max_kernel_call_seconds", 0.0), elapsed)
        statistics["precomputed"] = statistics.get("precomputed", 0) + len(candidates)
        started = perf_counter()
        results = tuple(DeliveryBounds(self.previous.plan, chains, (
            (score_seconds_to_hours(Decimal(int(row[0]))), score_seconds_to_hours(Decimal(int(row[1])))),
            tuple(score_seconds_to_hours(_context().divide(Decimal(int(value)), Decimal(self.weight_scale)))
                  for value in row[2:]),
        ), self.previous.delivery.timing) for chains, row in zip(candidates, output))
        statistics["result_seconds"] = statistics.get("result_seconds", 0.0) + perf_counter() - started
        return results


class _Unsupported(Exception):
    """Representational limits only; programming and compilation errors must propagate."""
