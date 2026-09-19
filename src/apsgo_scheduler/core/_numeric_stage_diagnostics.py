"""Read-only observations at the SB2/SB3 stage boundaries.

No budget polling, candidate charging, clock reconstruction or score evaluation
belongs here. The service formatter supplies request_id; schedule_id and call_id
also correlate direct core calls. Outside an observed schedule decorators are
transparent, so legacy/direct entries do not acquire new scheduling semantics.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from inspect import signature
import json
import logging
from math import isfinite
from time import perf_counter
from uuid import uuid4

from ._numeric_rules import NumericReason

logger = logging.getLogger(__name__)
_CURRENT = ContextVar("apsgo_numeric_stage_diagnostics", default=None)
_BASIC_CHILDREN = ("whole", "node", "fill", "order")
_BATCH_COUNTERS = ("batch_prepared", "batch_consumed", "batch_stale", "batch_stopped",
                   "numeric_precomputed", "numeric_consumed", "numeric_discarded")


def _value(value):
    return getattr(value, "value", value)


def _difference(after, before):
    return None if after is None or before is None else after - before


def _number(value):
    if isinstance(value, bool):
        raise ValueError("diagnostic count must not be boolean")
    result = int(value)
    if result < 0 or result != value:
        raise ValueError("nonnegative exact diagnostic count required")
    return result


def _identity(state):
    return dict(task=state.task.fingerprint, plan=state.plan.fingerprint,
        generation=_number(state.plan.generation), rules=state.program.fingerprint,
        quality=state.quality.fingerprint, virtual_sequence=_number(state.virtual_sequence),
        split_sequence=_number(state.split_sequence))


def evaluation_snapshot(state):
    """Read only the already-bound evaluation; missing/stale data stays unknown.

    Duplicate configured earliest rules describe one physical node. Sum its
    milliseconds once, but count every other prohibited finding as reported.
    Quality units stay in the compiled projection, not mislabeled as tonnes.
    """
    identity = _identity(state)
    task, plan, program, quality, evaluation = (
        state.task, state.plan, state.program, state.quality, state.evaluation)
    if (program.task_fingerprint != task.fingerprint
            or quality.task_fingerprint != task.fingerprint
            or quality.rule_program_fingerprint != program.fingerprint
            or plan.task_fingerprint != task.fingerprint
            or evaluation.task_fingerprint != task.fingerprint
            or evaluation.plan_fingerprint != plan.fingerprint
            or evaluation.plan_generation != plan.generation
            or evaluation.rule_program_fingerprint != program.fingerprint
            or evaluation.quality_program_fingerprint != quality.fingerprint):
        raise ValueError("diagnostic evaluation is not bound to the official state")
    order = tuple(_value(item) for item in quality.objectives)
    values = tuple(int(item) for item in evaluation.quality_key)
    if len(order) != len(values) or len(set(order)) != len(order):
        raise ValueError("diagnostic quality columns do not match their objectives")
    named = dict(zip(order, values))
    early, sources, other = {}, set(), 0
    for finding in evaluation.violations:
        if finding.reason is not NumericReason.EARLY_START:
            other += int(finding.prohibited)
            continue
        chain, position = int(finding.chain_index), int(finding.start_position)
        if not 0 <= chain < len(plan.chain_ids):
            raise ValueError("invalid early finding chain")
        first, last = int(plan.chain_offsets[chain]), int(plan.chain_offsets[chain + 1])
        if not 0 <= position < last - first:
            raise ValueError("invalid early finding position")
        row = int(plan.node_rows[first + position])
        if not 0 <= row < len(task.nodes.source):
            raise ValueError("invalid early finding node")
        source = int(task.nodes.source[row])
        if not 0 <= source < len(task.source_ids):
            raise ValueError("early finding must refer to a real source")
        severity = _number(finding.severity)
        if severity % 1000:
            raise ValueError("earliest severity must retain integer milliseconds")
        location, milliseconds = (chain, position), severity // 1000
        if location in early and early[location] != milliseconds:
            raise ValueError("duplicate earliest findings disagree")
        early[location] = milliseconds
        sources.add(source)
    return dict(identity=identity, quality_order=order, quality_key=values,
        early_node_count=len(early), early_source_count=len(sources),
        early_total_ms=sum(early.values()), other_prohibited_count=other,
        underweight_chain_count=named["underweight_chain_count"],
        underweight_gap_quality_units=named["underweight_total_gap"],
        virtual_weight_quality_units=named["generated_virtual_weight"],
        quality_units="compiled_numeric_projection")


def _canonical_stage(name):
    name = name.removesuffix(":refill")
    if name == "split:scan":
        return "split_scan"
    return "replay" + name[len("split:replay"):] if name.startswith("split:replay") else name


class StageDiagnostics:
    """Task-local trace, retaining only compact coverage and accounting records.

    Observation errors never alter the solver or hide a propagating exception.
    No clock/token/permit method is called on the shared budget by this class.
    """
    def __init__(self, state, budget, batch=None):
        self.state, self.budget, self.batch = state, budget, batch
        self.schedule_id = uuid4().hex
        self.version, self.allocations, self.primary = None, None, ()
        self.frames, self.completed, self.latest = [], [], {}
        self.dispatch = None
        self.failures = 0
        self.sequence = 0
        self._token = None

    def safe(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            self.failures += 1
            return None

    def configure(self, version, primary, allocations):
        self.version, self.primary = version, tuple(primary)
        self.allocations = dict(zip((*self.primary, "reserve"), map(int, allocations)))

    def snapshot(self):
        identity = self.safe(_identity, self.state)
        evaluation = self.safe(evaluation_snapshot, self.state)
        return dict(identity=identity, evaluation=evaluation,
            evaluation_status="available" if evaluation is not None else "unavailable_or_stale",
            candidate_count=self.safe(lambda: _number(self.budget.candidate_check_count)),
            complete_evaluations=self.safe(lambda: _number(self.state.complete_candidate_evaluation_count)),
            accepted_moves=self.safe(lambda: _number(self.state.accepted_move_count)),
            replay_count=self.safe(lambda: _number(self.state.replay_count)),
            global_stop_reason=self.safe(lambda: _value(self.budget.stop_reason)))

    def batch_snapshot(self):
        if self.batch is None:
            return None
        def read():
            result = {name: sum(_number(v) for v in getattr(self.batch, name).values())
                      for name in _BATCH_COUNTERS}
            seconds = float(self.batch.numeric_batch_prepare_seconds)
            if not isfinite(seconds) or seconds < 0:
                raise ValueError("invalid batch time")
            result["numeric_batch_prepare_seconds"] = seconds
            return result
        return self.safe(read)

    def emit(self, event, payload):
        # Standard logging handlers may raise. Never retry recursively via logging.
        self.safe(lambda: logger.info("%s %s", event,
            json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True),
            extra={"numeric_stage_payload": payload}))

    def begin(self, name, checks, seconds, children=(), progress=None, skipped=False):
        parent = self.frames[-1] if self.frames else None
        self.sequence += 1
        frame = dict(call_id=self.sequence, parent_call_id=None if parent is None else parent["call_id"],
            name=name, requested_checks=checks, requested_seconds=seconds, children=tuple(children),
            observed_children=set(), before=self.snapshot(), batch_before=self.batch_snapshot(),
            started=perf_counter(), progress=progress, skipped=skipped,
            cleanup_calls=0, cleanup_failures=0,
            refill=bool(self.dispatch and self.dispatch[2]),
            complete_at_entry=self.safe(progress.complete_for, self.state)
                if callable(getattr(progress, "complete_for", None)) else None)
        if parent is not None:
            parent["observed_children"].add(name)
        elif self.dispatch is not None:
            # A global-stop placeholder has an actual grant of zero, but its
            # original reserved request must not disappear from the diagnostic.
            frame["requested_checks"] = self.dispatch[1]
        self.frames.append(frame)
        return frame

    def finish(self, frame, result=None, error=None):
        try:
            self._finish(frame, result, error)
        finally:
            if self.frames and self.frames[-1] is frame:
                self.frames.pop()

    def _finish(self, frame, result, error):
        after = self.snapshot()
        b = None if result is None else result.budget
        stop = after["global_stop_reason"]
        reason = ("global_stop" if stop and error else "exception") if error else (
            "unobserved" if b is None else _value(b.reason))
        before = frame["before"]
        count = _difference(after["candidate_count"], before["candidate_count"])
        entered = None if b is None else not (
            frame["skipped"] or reason in ("not_applicable", "insufficient_budget")
            or before["global_stop_reason"] is not None or b.candidate_limit == 0
            or b.soft_deadline <= b.started_at)
        if count is not None and count > 0:
            entered = True
        skip_reason = None
        if entered is False:
            skip_reason = reason
            if reason == "not_applicable" and _canonical_stage(frame["name"]) == "replay":
                skip_reason = "no_pending_replay"
            elif reason not in ("not_applicable", "global_stop", "insufficient_budget"):
                skip_reason = "time_slice_exhausted" if b.soft_deadline <= b.started_at else reason
        if (error is None and reason == "scan_complete" and frame["complete_at_entry"]
                and count == 0):
            entered, skip_reason = False, "already_complete_for_generation"
        declared_complete = None if result is None else bool(result.scan_complete)
        binding_valid = (after["evaluation"] is not None and result is not None
                         and result.generation_after == after["identity"]["generation"])
        complete = (None if result is None or not binding_valid or error is not None else
                    declared_complete and reason != "not_applicable")
        coverage = after["identity"] if complete else None
        batch_after = self.batch_snapshot()
        batch_delta = None if frame["batch_before"] is None or batch_after is None else {
            key: _difference(batch_after[key], frame["batch_before"][key]) for key in batch_after}
        cleanup = (False if frame["cleanup_failures"] else None if error is not None
                   else True if result is not None else None)
        pending = (None if result is None else bool(result.pending_replay))
        # Missing child calls are observations of skipped work, not function calls
        # or newly allocated budget. Do not invent the unissued child grants.
        for suffix in frame["children"]:
            name = frame["name"] + ":" + suffix
            if name not in frame["observed_children"]:
                why = "global_stop" if stop else "parent_exception" if error else skip_reason or "parent_not_entered"
                self.missing(name, frame["call_id"], why, after, refill=frame["refill"])
        payload = dict(schema="numeric_search_phase_v1", schedule_id=self.schedule_id,
            schedule_version=self.version, allocations=self.allocations,
            call_id=frame["call_id"], parent_call_id=frame["parent_call_id"],
            name=frame["name"], stage=_canonical_stage(frame["name"]), refill=frame["refill"],
            top_level=frame["parent_call_id"] is None, entered=entered, skip_reason=skip_reason,
            requested_checks=frame["requested_checks"], requested_seconds=frame["requested_seconds"],
            granted_checks=None if b is None else b.candidate_limit,
            soft_deadline=None if b is None else b.soft_deadline,
            elapsed_seconds=perf_counter() - frame["started"],
            budget_elapsed_seconds=None if b is None else b.elapsed_seconds,
            soft_overrun_seconds=None if b is None else b.soft_overrun_seconds,
            before=before, after=after, candidate_checks=count,
            complete_evaluations=_difference(after["complete_evaluations"], before["complete_evaluations"]),
            accepted_moves=_difference(after["accepted_moves"], before["accepted_moves"]),
            exit_reason=reason, global_stop_reason=stop,
            global_remaining_checks=self.safe(lambda: self.budget.candidate_check_limit - self.budget.candidate_check_count),
            scan_complete=complete, coverage_binding=coverage,
            not_applicable_at_exit=reason == "not_applicable",
            pending_resume=None if complete is None else not complete
                and skip_reason not in ("not_applicable", "no_pending_replay"),
            pending_replay=pending, batch=batch_delta,
            batch_status="partial" if error else "unavailable" if batch_delta is None else "reported_counters",
            cleanup_completed=cleanup, cleanup_calls=frame["cleanup_calls"],
            cleanup_failures=frame["cleanup_failures"],
            cleanup_unknown_reason="exception_before_cleanup_confirmation" if cleanup is None else None,
            error_type=None if error is None else type(error).__name__,
            diagnostic_failures=self.failures)
        self._publish(payload)

    def _publish(self, payload):
        # Only compact scalar records survive this call; arrays/state never do.
        entry = {key: payload[key] for key in ("call_id", "parent_call_id", "name", "stage",
                 "top_level", "candidate_checks", "scan_complete", "coverage_binding")}
        entry["count_at_entry"] = payload["before"]["candidate_count"]
        entry["count_at_exit"] = payload["after"]["candidate_count"]
        self.completed.append(entry)
        self.latest[entry["stage"]] = entry
        self.emit("numeric_search_phase_summary", payload)

    def missing(self, name, parent, reason, snapshot=None, *, refill=False):
        snapshot = self.snapshot() if snapshot is None else snapshot
        self.sequence += 1
        self._publish(dict(schema="numeric_search_phase_v1", schedule_id=self.schedule_id,
            schedule_version=self.version, allocations=self.allocations,
            call_id=self.sequence, parent_call_id=parent, name=name, stage=_canonical_stage(name),
            top_level=parent is None, refill=refill, entered=False, skip_reason=reason,
            requested_checks=None, requested_seconds=None, granted_checks=0, soft_deadline=None,
            elapsed_seconds=0.0, budget_elapsed_seconds=0.0, soft_overrun_seconds=0.0,
            before=snapshot, after=snapshot, candidate_checks=0, complete_evaluations=0,
            accepted_moves=0, exit_reason=reason, global_stop_reason=snapshot["global_stop_reason"],
            global_remaining_checks=self.safe(lambda: self.budget.candidate_check_limit - self.budget.candidate_check_count),
            scan_complete=False, coverage_binding=None, not_applicable_at_exit=False,
            pending_resume=reason not in ("no_pending_replay", "not_applicable",
                "already_complete_for_generation"), pending_replay=None,
            batch=None, batch_status="not_executed", cleanup_completed=True,
            cleanup_calls=0, cleanup_failures=0, cleanup_unknown_reason=None,
            error_type=None, diagnostic_failures=self.failures))

    def __enter__(self):
        self.initial = self.snapshot()
        self._token = _CURRENT.set(self)
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.safe(self.finish_schedule, exc)
        finally:
            _CURRENT.reset(self._token)
        return False

    def finish_schedule(self, error):
        final = self.snapshot()
        for name in self.primary:
            if not any(item["top_level"] and item["stage"] == name for item in self.completed):
                self.missing(name, None, "global_stop" if final["global_stop_reason"] else
                             "schedule_exception" if error else "unobserved", final)
        top = [item for item in self.completed if item["top_level"]]
        total = None if any(item["candidate_checks"] is None for item in top) else sum(
            item["candidate_checks"] for item in top)
        actual = _difference(final["candidate_count"], self.initial["candidate_count"])
        cursor = self.initial["candidate_count"]
        contiguous = cursor is not None
        for item in sorted(top, key=lambda row: row["call_id"]):
            contiguous = contiguous and item["count_at_entry"] == cursor
            cursor = item["count_at_exit"]
        contiguous = contiguous and cursor == final["candidate_count"]
        coverage = [dict(stage=key, call_id=value["call_id"],
                        scan_complete_at_exit=value["scan_complete"],
                        valid_for_final_state=value["coverage_binding"] is not None
                        and value["coverage_binding"] == final["identity"])
                    for key, value in sorted(self.latest.items())]
        self.emit("numeric_search_phase_totals", dict(schema="numeric_search_phase_totals_v1",
            schedule_id=self.schedule_id, schedule_version=self.version, allocations=self.allocations,
            count_at_entry=self.initial["candidate_count"], count_at_exit=final["candidate_count"],
            top_level_candidate_checks=total, actual_candidate_checks=actual,
            ledger_matches=None if total is None or actual is None else total == actual and contiguous,
            child_checks_included_in_parents=True, phase_count=len(self.completed),
            coverage=coverage, global_stop_reason=final["global_stop_reason"],
            error_type=None if error is None else type(error).__name__,
            diagnostic_failures=self.failures))


def configure_stage_diagnostics(version, primary, allocations):
    trace = _CURRENT.get()
    if trace is not None:
        trace.safe(trace.configure, version, primary, allocations)


def observe_schedule(function):
    @wraps(function)
    def run(state, budget, *args, **kwargs):
        with StageDiagnostics(state, budget, kwargs.get("diagnostics")):
            return function(state, budget, *args, **kwargs)
    return run


def observe_dispatch(function):
    @wraps(function)
    def run(name, requested, refill):
        trace = _CURRENT.get()
        if trace is None:
            return function(name, requested, refill)
        previous = trace.dispatch
        trace.dispatch = (name, requested, refill)
        try:
            return function(name, requested, refill)
        finally:
            trace.dispatch = previous
    return run


def observe_stage(default=None, *, children=(), checks_key="candidate_checks",
                  seconds_key="time_slice_seconds", skipped=False):
    """Observe actual synchronous calls, not reconstructed child quality later."""
    def decorate(function):
        parameters = signature(function)
        @wraps(function)
        def run(*args, **kwargs):
            trace = _CURRENT.get()
            if trace is None:
                return function(*args, **kwargs)
            bound = trace.safe(parameters.bind, *args, **kwargs)
            if bound is None:
                return function(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            if values.get("state") is not trace.state or values.get("budget") is not trace.budget:
                return function(*args, **kwargs)
            progress = values.get("progress")
            name = values.get("name") or getattr(progress, "operator", None) or default
            # split_scan and replay adapters delegate to the same named scope.
            # The outer adapter observes the pending-replay update too.
            if trace.frames and trace.frames[-1]["name"] == name:
                return function(*args, **kwargs)
            planned = children
            if skipped:
                planned = _BASIC_CHILDREN if name == "basic" else ("scan", "replay") if name == "split" else ()
            frame = trace.safe(trace.begin, name, values.get(checks_key, 0),
                               values.get(seconds_key), planned, progress, skipped)
            if frame is None:
                return function(*args, **kwargs)
            try:
                result = function(*args, **kwargs)
            except BaseException as error:
                trace.safe(trace.finish, frame, error=error)
                raise
            trace.safe(trace.finish, frame, result)
            return result
        return run
    return decorate


def observed_cleanup(function, *args, **kwargs):
    """Record the existing cleanup call; its exception retains normal semantics."""
    trace = _CURRENT.get()
    frame = trace.frames[-1] if trace is not None and trace.frames else None
    if frame is not None:
        frame["cleanup_calls"] += 1
    try:
        return function(*args, **kwargs)
    except BaseException:
        if frame is not None:
            frame["cleanup_failures"] += 1
        raise
