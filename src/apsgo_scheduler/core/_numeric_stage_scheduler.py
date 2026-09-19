"""Reserved sequential search stages over one ledger, followed by fair refill.

No solver policy, score or external stop enum is added. Every grant is debited
once; only its unspent remainder is returned. Child remainders are not added to
the top-level pool. Finalization remains outside this scheduler.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import _numeric_stage_operators as operators
from ._numeric_refinement_stage import (
    NumericRefinementProgress, run_refinement_slice, refinement_applicable,
)
from ._numeric_rules import NumericRuleKind, NumericReason
from ._search_phase_budget import SearchPhaseExit
from .budget import _finite_time
from .contracts import SearchStopReason, require_int

from ._numeric_stage_diagnostics import (
    observe_schedule, observe_dispatch, observe_stage, configure_stage_diagnostics,
)

SCHEDULE_VERSION = "reserved_sequential_v1"
PRIMARY_STAGES = ("basic", "split", "refinement")
TOP_PERCENTAGES = (20, 10, 60)


def allocate_checks(remaining):
    require_int(remaining, "remaining_candidate_checks")
    first = tuple(remaining * percent // 100 for percent in TOP_PERCENTAGES)
    return (*first, remaining - sum(first))


def _remaining_time(budget):
    return max(0.0, budget.search_deadline_monotonic - _finite_time(budget.clock(), "clock()"))


def _has_early(state):
    if not state.program.for_kind(NumericRuleKind.EARLIEST_START):
        return False
    result = state.evaluation.kernel_result
    return any(int(row[1]) == int(NumericReason.EARLY_START)
               for row in result.violations[:int(result.counts[0])])


def _stop_before_new(budget):
    if not budget.allows_search():
        return True
    # All calls are synchronous and scopes have closed here. Do not create an
    # extra "candidate" just to manufacture the stop at exact global exhaustion.
    if budget.candidate_check_count == budget.candidate_check_limit:
        budget.stop_reason = SearchStopReason.CANDIDATE_LIMIT_REACHED
        return True
    return False


@dataclass(frozen=True, slots=True)
class StageObservation:
    name: str
    refill: bool
    requested_checks: int
    requested_seconds: float
    result: operators.NumericStageResult
    binding_before: tuple
    binding_after: tuple
    quality_before: tuple
    quality_after: tuple
    complete_evaluations: int
    accepted_moves: int


@dataclass(frozen=True, slots=True)
class NumericStageScheduleReport:
    version: str
    count_at_entry: int
    count_at_exit: int
    allocations: tuple[int, ...]
    observations: tuple[StageObservation, ...]
    unspent_checks: int
    stop_reason: SearchStopReason | None

    def summary(self):
        return dict(version=self.version, count_at_entry=self.count_at_entry,
            count_at_exit=self.count_at_exit, allocations=self.allocations,
            unspent_checks=self.unspent_checks,
            stop_reason=None if self.stop_reason is None else self.stop_reason.value,
            stages=tuple(dict(name=o.name, refill=o.refill,
                used=o.result.budget.used_candidate_checks,
                exit=o.result.budget.reason.value, scan_complete=o.result.scan_complete,
                generation_before=o.result.generation_before,
                generation_after=o.result.generation_after) for o in self.observations))


@dataclass
class _Ledger:
    pool: int
    pending: dict
    settled: set = field(default_factory=set)

    def initial(self, name):
        if name not in self.pending:
            raise ValueError("initial reservation was already claimed")
        return self.pending.pop(name)

    def refill(self, maximum):
        amount = min(self.pool, max(1, maximum))
        self.pool -= amount
        return amount

    def settle(self, number, requested, result):
        if number in self.settled:
            raise ValueError("stage grant was already settled")
        used = result.budget.used_candidate_checks
        if result.budget.failed or not 0 <= used <= result.budget.candidate_limit <= requested:
            raise ValueError("invalid stage allowance result")
        self.settled.add(number)
        self.pool += requested - used

    def check(self, remaining):
        if self.pool < 0 or self.pool + sum(self.pending.values()) != remaining:
            raise ValueError("stage ledger does not match the global candidate balance")


@observe_stage("skipped", skipped=True)
def _skipped(state, budget, name, reason):
    before = state.plan.generation
    with budget.phase_scope(name, 0, 0) as phase:
        if not budget.must_stop:
            phase.finish(reason)
    return operators.NumericStageResult(phase.release_unused(), (), before, before, False)


@observe_stage("replay", checks_key="checks", seconds_key="seconds")
def _replay_operator(state, budget, progress, operator, checks, seconds, slack, bridges, name):
    """Refill a single replay child, including when the initial split share was 0.

    Repartitioning a one-credit refill four ways would starve three children.
    The logical replay count is still recorded once for the split cycle.
    """
    result = operators.run_operator_slice(state, budget, progress=progress.replay.operators[operator],
        candidate_checks=checks, time_slice_seconds=seconds,
        pair_scan_slack_weight=slack, maximum_virtual_bridge_nodes=bridges, name=name)
    performed = result.budget.used_candidate_checks > 0 or (
        result.scan_complete and result.budget.candidate_limit > 0
        and result.budget.reason is SearchPhaseExit.SCAN_COMPLETE)
    if performed and not progress.replay_started:
        progress.replay_started = True
        state.replay_count += 1
    if progress.replay.complete_for(state):
        progress.pending_replay = False
    return replace(result, pending_replay=progress.pending_replay)


def _pending_work(state, basic, split, refinement):
    pending = []
    needs_refinement = not refinement.complete_for(state)
    if needs_refinement and _has_early(state) and refinement_applicable(state):
        pending.append("refinement")
    if split.pending_replay:
        pending.extend("replay:" + name for name in operators.BASIC_OPERATORS
                       if not split.replay.operators[name].complete_for(state))
    if not split.scan.complete_for(state):
        pending.append("split_scan")
    pending.extend("basic:" + name for name in operators.BASIC_OPERATORS
                   if not basic.operators[name].complete_for(state))
    if needs_refinement and "refinement" not in pending:
        pending.append("refinement")
    return tuple(pending)


def _progress_stamp(state, basic, split, refinement):
    def op(p):
        return (p.binding, p.cursor, p.complete, p.not_applicable)
    return (operators._identity(state), tuple(op(p) for p in basic.operators.values()),
            op(split.scan), tuple(op(p) for p in split.replay.operators.values()),
            split.pending_replay, split.replay_started, refinement.revision,
            refinement.complete, refinement.pending)


@observe_schedule
def run_numeric_stage_schedule(state, budget, *, pair_scan_slack_weight,
                               maximum_virtual_bridge_nodes=2, diagnostics=None,
                               batch_size=8):
    """Production schedule; helper/prefix entries keep their legacy coverage."""
    operators._validate(state, budget, pair_scan_slack_weight, maximum_virtual_bridge_nodes)
    if budget._active_phase is not None:
        raise ValueError("production scheduling must own the top-level stage scope")
    if type(batch_size) is not int or not 1 <= batch_size <= 64:
        raise ValueError("one to 64 batch descriptions required")
    start_count = budget.candidate_check_count
    available = budget.candidate_check_limit - start_count
    allocation = list(allocate_checks(available))
    if allocation[2] == 0 and allocation[3] and _has_early(state) and refinement_applicable(state):
        allocation[2], allocation[3] = 1, allocation[3] - 1
    configure_stage_diagnostics(SCHEDULE_VERSION, PRIMARY_STAGES, allocation)
    initial_time = _remaining_time(budget)
    times = {name: initial_time * percent / 100 for name, percent in zip(PRIMARY_STAGES, TOP_PERCENTAGES)}
    ledger = _Ledger(allocation[3], dict(zip(PRIMARY_STAGES, allocation[:3])))
    basic, split, refinement = (operators.NumericBasicProgress(), operators.NumericSplitProgress(),
                                NumericRefinementProgress())
    observations = []
    split_replay = allocation[1] // 2
    caps = dict(zip(PRIMARY_STAGES, allocation[:3]))
    caps["split_scan"] = allocation[1] - split_replay
    caps.update(("basic:" + name, n) for name, n in zip(operators.BASIC_OPERATORS, operators._shares(allocation[0])))
    caps.update(("replay:" + name, n) for name, n in zip(operators.BASIC_OPERATORS, operators._shares(split_replay)))
    factors = dict(zip(operators.BASIC_OPERATORS, (.5, .25, .1, .15)))
    times["split_scan"] = times["split"] / 2
    times.update(("basic:" + name, times["basic"] * factor) for name, factor in factors.items())
    times.update(("replay:" + name, times["split"] * factor / 2) for name, factor in factors.items())
    common = dict(pair_scan_slack_weight=pair_scan_slack_weight,
                  maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes)

    @observe_dispatch
    def execute(name, requested, refill):
        before_binding = operators._identity(state)
        before_quality = tuple(map(int, state.evaluation.quality_key))
        evaluated, accepted = state.complete_candidate_evaluation_count, state.accepted_move_count
        count_before = budget.candidate_check_count
        seconds = min(times[name], _remaining_time(budget))
        label = name + (":refill" if refill else "")
        if _stop_before_new(budget):
            result = _skipped(state, budget, label, SearchPhaseExit.INSUFFICIENT_BUDGET)
        elif requested == 0:
            result = _skipped(state, budget, label, SearchPhaseExit.INSUFFICIENT_BUDGET)
        elif name == "basic":
            result = operators.run_basic_slice(state, budget, progress=basic,
                candidate_checks=requested, time_slice_seconds=seconds, name=label, **common)
        elif name == "split":
            result = operators.run_split_slice(state, budget, progress=split,
                candidate_checks=requested, time_slice_seconds=seconds, name=label, **common)
        elif name == "split_scan":
            result = operators.run_split_scan_slice(state, budget, progress=split,
                candidate_checks=requested, time_slice_seconds=seconds, name=label, **common)
        elif name.startswith("replay:"):
            result = _replay_operator(state, budget, split, name.split(":")[1], requested,
                seconds, pair_scan_slack_weight, maximum_virtual_bridge_nodes, label)
        elif name.startswith("basic:"):
            result = operators.run_operator_slice(state, budget,
                progress=basic.operators[name.split(":")[1]], candidate_checks=requested,
                time_slice_seconds=seconds, name=label, **common)
        else:
            result = run_refinement_slice(state, budget, progress=refinement,
                candidate_checks=requested, time_slice_seconds=seconds, name=label,
                maximum_virtual_bridge_nodes=maximum_virtual_bridge_nodes,
                diagnostics=diagnostics, batch_size=batch_size)
        if result.budget.count_at_entry != count_before or result.budget.count_at_exit != budget.candidate_check_count:
            raise ValueError("stage returned a foreign candidate ledger")
        observation = StageObservation(name, refill, requested, seconds, result,
            before_binding, operators._identity(state), before_quality,
            tuple(map(int, state.evaluation.quality_key)),
            state.complete_candidate_evaluation_count - evaluated, state.accepted_move_count - accepted)
        observations.append(observation)
        ledger.settle(len(observations), requested, result)
        ledger.check(budget.candidate_check_limit - budget.candidate_check_count)

    for name in PRIMARY_STAGES:
        execute(name, ledger.initial(name), False)
    while not _stop_before_new(budget):
        pending = _pending_work(state, basic, split, refinement)
        if not pending:
            budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
            break
        round_stamp = _progress_stamp(state, basic, split, refinement)
        round_count = budget.candidate_check_count
        for name in pending:
            if _stop_before_new(budget):
                break
            if name not in _pending_work(state, basic, split, refinement):
                continue
            grant = ledger.refill(caps[name])
            if grant == 0:
                raise ValueError("pending search has no conserved refill allowance")
            execute(name, grant, True)
        if (not budget.must_stop and round_stamp == _progress_stamp(state, basic, split, refinement)
                and round_count == budget.candidate_check_count):
            raise ValueError("pending stage work made no resumable progress")
    used = sum(o.result.budget.used_candidate_checks for o in observations)
    if used != budget.candidate_check_count - start_count:
        raise ValueError("top-level stage consumption does not match the global count")
    ledger.check(budget.candidate_check_limit - budget.candidate_check_count)
    return NumericStageScheduleReport(SCHEDULE_VERSION, start_count, budget.candidate_check_count,
        tuple(allocation), tuple(observations), ledger.pool, budget.stop_reason)
