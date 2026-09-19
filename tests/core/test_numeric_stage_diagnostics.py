"""SB4 functional checks, not full-solver or integration verification.

Load actual diagnostics, budget, SB2 wrappers and SB3 scheduling/control in an
isolated package. Task/evaluation data, business operators and batch computation
are explicit test doubles. No database, solver entry package or old test suite
is executed. Contract validators/stop enum are extracted from repository source.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
from enum import Enum, IntEnum
import importlib.util
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
from uuid import uuid4

import numpy as np
import pytest

CORE = Path(__file__).resolve().parents[2] / "src/apsgo_scheduler/core"


@pytest.fixture
def rt(monkeypatch, caplog):
    package = "_apsgo_sb4_" + uuid4().hex
    root = ModuleType(package)
    root.__path__ = [str(CORE)]
    monkeypatch.setitem(sys.modules, package, root)

    def module(name):
        item = ModuleType(package + "." + name)
        monkeypatch.setitem(sys.modules, item.__name__, item)
        return item

    def load(name):
        spec = importlib.util.spec_from_file_location(package + "." + name, CORE / (name + ".py"))
        item = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, item.__name__, item)
        spec.loader.exec_module(item)
        return item

    contracts = module("contracts")
    contracts.Enum = Enum
    selected = {"SearchStopReason", "require_int", "require_text", "require_enum"}
    syntax = ast.parse((CORE / "contracts.py").read_text(encoding="utf-8"))
    definitions = [n for n in syntax.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in selected]
    assert {n.name for n in definitions} == selected
    exec(compile(ast.Module(body=definitions, type_ignores=[]), "contracts.py", "exec"), contracts.__dict__)
    # from_policy is not under test; these unused imports are explicit boundaries.
    contracts.SolverPolicy = type("UnusedSolverPolicy", (), {})
    contracts.sum_decimals = lambda values: sum(values)

    class Kind(IntEnum):
        EARLIEST_START = 1
        CHAIN_WEIGHT = 2
        DELIVERY = 3
        INTER_CHAIN_WIDTH = 4
        VIRTUAL_RATIO = 5

    class Reason(IntEnum):
        EARLY_START = 1
        OTHER = 2

    rules = module("_numeric_rules")
    rules.NumericRuleKind, rules.NumericReason = Kind, Reason
    budget_module = load("budget")
    phases = load("_search_phase_budget")
    diag = load("_numeric_stage_diagnostics")
    caplog.set_level(logging.INFO, logger=diag.logger.name)
    search = module("_numeric_search")
    search.NumericRuleKind = Kind
    search.NumericValueError = ValueError
    search.NumericDeferredCandidateFailure = type("NumericDeferredCandidateFailure", (), {})
    search._validate_search_inputs = lambda task, program, quality, state, budget: None
    operators = load("_numeric_stage_operators")
    legacy = module("_numeric_refinement")
    scan = module("_numeric_refinement_scan")
    scan.ACTION, scan.OWNER, scan.VARIANT = 0, 1, 2
    scan.build_scan = lambda state, columns: NS(sources=np.array([0]))
    cursor = module("_numeric_refinement_cursor")
    cursor.MORE, cursor.ERROR, cursor.DONE = -1, -2, -3

    class Cursor:
        def __init__(self, family, lane, owners):
            self.complete = False
            self.journal = []
        def rollback(self, marker):
            del self.journal[marker:]
        def commit(self):
            self.journal.clear()

    cursor.FamilyCursor = Cursor
    refinement = load("_numeric_refinement_stage")
    scheduler = load("_numeric_stage_scheduler")

    def drive(state, budget, phase, progress, slack, bridges):
        """Business double: rejects one paid candidate per available slot."""
        progress.bind(state, (slack, bridges))
        while phase.allows_new_work():
            identity = phases.SearchAttemptIdentity(state.task.fingerprint,
                state.plan.fingerprint, state.plan.generation, budget.candidate_check_count + 1)
            with phase.candidate_attempt(identity) as attempt:
                if not attempt.granted:
                    return
                state.complete_candidate_evaluation_count += 1
                progress.cursor = (*progress.cursor[:-1], progress.cursor[-1] + 1)

    monkeypatch.setattr(operators, "_drive_operator", drive)

    class Pool:
        def __init__(self, *args, **kwargs):
            self.release_count = 0
        def release(self):
            self.release_count += 1

    legacy.task_columns = lambda task: None
    legacy.rule_tables = lambda rules: None
    legacy.NumericCandidateBatchWorkspace = Pool

    def family(state, budget, phase, progress, key, x, columns, rules, pool, bridges, diagnostics):
        identity = phases.SearchAttemptIdentity(state.task.fingerprint,
            state.plan.fingerprint, state.plan.generation, budget.candidate_check_count + 1)
        with phase.candidate_attempt(identity) as attempt:
            if attempt.granted:
                state.complete_candidate_evaluation_count += 1
                progress.families[key].complete = True
                progress.revision += 1
        return False

    monkeypatch.setattr(refinement, "_family_batch", family)
    result = NS(diag=diag, operators=operators, phases=phases, scheduler=scheduler,
                refinement=refinement, legacy=legacy, search=search, contracts=contracts,
                budget_module=budget_module, Kind=Kind, Reason=Reason, Pool=Pool,
                package=package, load=load)
    return result


class Clock:
    def __init__(self):
        self.now, self.calls = 1.0, 0
    def __call__(self):
        self.calls += 1
        return self.now


def budget_for(rt, limit=100, used=0):
    clock = Clock()
    budget = rt.budget_module.SolveRuntimeBudget(0.0, 1000.0, 1010.0,
                                                limit, used, None, clock=clock)
    return budget, clock


def state_for(rt, early=True):
    objectives = ("prohibited_violation_count", "prohibited_violation_severity",
        "underweight_chain_count", "underweight_total_gap", "old_backlog_last_completion_hours",
        "delivery_wait_tardiness_tonne_hours", "inter_chain_width_gap",
        "generated_virtual_weight", "chain_count")
    task = NS(fingerprint="task-0", nodes=NS(source=np.array([5, -1, 5, 9]),
        role=np.zeros(4, dtype=np.int64), purpose=np.zeros(4, dtype=np.int64),
        split_group=np.full(4, -1, dtype=np.int64)), source_ids=tuple(str(i) for i in range(10)))
    plan = NS(task_fingerprint=task.fingerprint, fingerprint="plan-0", generation=0,
        node_rows=np.array([2, 0, 3]), chain_offsets=np.array([0, 2, 3]), chain_ids=np.array([7, 2]))
    program = NS(task_fingerprint=task.fingerprint, fingerprint="rules-0", rules=(),
        for_kind=lambda kind: (True,) if early and kind == rt.Kind.EARLIEST_START else ())
    quality = NS(task_fingerprint=task.fingerprint, rule_program_fingerprint=program.fingerprint,
                 fingerprint="quality-0", objectives=tuple(NS(value=s) for s in objectives))
    def finding(chain, position, ms, reason=None, prohibited=True):
        return NS(chain_index=chain, start_position=position, severity=ms * 1000,
                  reason=rt.Reason.EARLY_START if reason is None else reason, prohibited=prohibited)
    findings = [finding(0, 0, 100), finding(0, 1, 300), finding(1, 0, 20)] if early else []
    findings += [finding(0, 0, 0, rt.Reason.OTHER)]
    result = NS(task_fingerprint=task.fingerprint, plan_fingerprint=plan.fingerprint,
        plan_generation=0, rule_program_fingerprint=program.fingerprint,
        quality_program_fingerprint=quality.fingerprint,
        quality_key=np.array([4, 420000, 2, 155, 0, 10, 20, 75, 2]),
        violations=tuple(findings), kernel_result=NS(
            violations=np.array([[0, int(rt.Reason.EARLY_START)]]) if early else np.empty((0, 2)),
            counts=np.array([1 if early else 0])))
    return NS(task=task, plan=plan, program=program, quality=quality, evaluation=result,
        virtual_sequence=0, split_sequence=0, complete_candidate_evaluation_count=0,
        accepted_move_count=0, replay_count=0)


def advance(state):
    state.plan = deepcopy(state.plan)
    state.plan.generation += 1
    state.plan.fingerprint = "plan-" + str(state.plan.generation)
    state.evaluation = deepcopy(state.evaluation)
    state.evaluation.plan_fingerprint = state.plan.fingerprint
    state.evaluation.plan_generation = state.plan.generation
    state.evaluation.quality_key[0] -= 1
    state.accepted_move_count += 1


def payloads(caplog, event="numeric_search_phase_summary"):
    return [r.numeric_stage_payload for r in caplog.records
            if hasattr(r, "numeric_stage_payload") and r.getMessage().startswith(event + " ")]


def trace_for(rt, state, budget, batch=None):
    trace = rt.diag.StageDiagnostics(state, budget, batch)
    trace.configure("reserved_sequential_v1", ("basic", "split", "refinement"), (20, 10, 60, 10))
    return trace


def run_operator(rt, state, budget, checks=2, name="whole", seconds=10.0, progress=None):
    return rt.operators.run_operator_slice(state, budget,
        progress=progress or rt.operators.NumericOperatorProgress("whole"),
        candidate_checks=checks, time_slice_seconds=seconds, name=name)


def test_snapshot_counts_nodes_and_sources_once(rt):
    state = state_for(rt)
    state.evaluation.violations += (deepcopy(state.evaluation.violations[0]),)
    value = rt.diag.evaluation_snapshot(state)
    assert (value["early_node_count"], value["early_source_count"], value["early_total_ms"]) == (3, 2, 420)
    assert value["other_prohibited_count"] == 1
    assert value["underweight_chain_count"] == 2
    assert value["underweight_gap_quality_units"] == 155
    assert value["virtual_weight_quality_units"] == 75


def test_snapshot_uses_objective_order_not_fixed_offsets(rt):
    state = state_for(rt)
    state.quality.objectives = tuple(reversed(state.quality.objectives))
    state.evaluation.quality_key = state.evaluation.quality_key[::-1]
    result = rt.diag.evaluation_snapshot(state)
    assert result["underweight_gap_quality_units"] == 155
    assert result["virtual_weight_quality_units"] == 75


@pytest.mark.parametrize("attribute", ["task_fingerprint", "plan_fingerprint", "plan_generation",
    "rule_program_fingerprint", "quality_program_fingerprint"])
def test_stale_evaluation_is_null_not_zero(rt, attribute):
    state = state_for(rt)
    setattr(state.evaluation, attribute, "stale")
    budget, _ = budget_for(rt)
    result = trace_for(rt, state, budget).snapshot()
    assert result["evaluation"] is None
    assert result["evaluation_status"] == "unavailable_or_stale"
    assert result["identity"]["plan"] == "plan-0"


@pytest.mark.parametrize("change", ["chain_negative", "chain_large", "position_negative",
    "position_large", "virtual", "source_large", "node_negative", "precision", "duplicates"])
def test_invalid_diagnostic_details_are_rejected_without_reading_negative_indices(rt, change):
    state = state_for(rt)
    f = state.evaluation.violations[0]
    if change == "chain_negative": f.chain_index = -1
    elif change == "chain_large": f.chain_index = 10
    elif change == "position_negative": f.start_position = -1
    elif change == "position_large": f.start_position = 9
    elif change == "virtual": state.task.nodes.source[2] = -1
    elif change == "source_large": state.task.nodes.source[2] = 10
    elif change == "node_negative": state.plan.node_rows[0] = -1
    elif change == "precision": f.severity = 1
    elif change == "duplicates":
        duplicate = deepcopy(f)
        duplicate.severity += 1000
        state.evaluation.violations += (duplicate,)
    with pytest.raises(ValueError): rt.diag.evaluation_snapshot(state)


def test_python_sum_does_not_overflow(rt):
    state = state_for(rt)
    large = 10 ** 16
    for f in state.evaluation.violations[:3]: f.severity = large * 1000
    assert rt.diag.evaluation_snapshot(state)["early_total_ms"] == 3 * large


def test_nonprohibited_other_finding_not_counted(rt):
    state = state_for(rt)
    state.evaluation.violations[-1].prohibited = False
    assert rt.diag.evaluation_snapshot(state)["other_prohibited_count"] == 0


def test_diagnostics_never_read_budget_clock_token_or_permit(rt):
    state = state_for(rt)
    def fail(*a, **k): raise AssertionError("diagnostics polled execution control")
    budget = NS(candidate_check_count=7, candidate_check_limit=100, stop_reason=None,
                clock=fail, allows_search=fail, permit=fail, cancellation=NS(is_cancelled=fail))
    with trace_for(rt, state, budget) as trace:
        trace.snapshot()
    assert trace.failures == 0


def test_scope_snapshot_is_detached_from_next_generation(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget): run_operator(rt, state, budget)
    entry = payloads(caplog)[0]
    advance(state)
    state.evaluation.violations[0].severity = 0
    assert entry["after"]["identity"]["generation"] == 0
    assert entry["after"]["evaluation"]["early_total_ms"] == 420


def test_actual_phase_limit_logs_exit_not_global_stop(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt, used=7)
    with trace_for(rt, state, budget): result = run_operator(rt, state, budget, checks=2)
    entry = payloads(caplog)[0]
    assert entry["entered"] is True
    assert entry["exit_reason"] == "candidate_slice_exhausted"
    assert entry["candidate_checks"] == 2
    assert entry["before"]["candidate_count"] == 7
    assert entry["after"]["candidate_count"] == 9
    assert entry["global_stop_reason"] is None
    assert entry["scan_complete"] is False
    assert entry["cleanup_completed"] is True
    assert result.budget.used_candidate_checks == 2


def test_zero_grant_distinct_from_not_applicable(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget): run_operator(rt, state, budget, checks=0)
    value = payloads(caplog)[0]
    assert value["entered"] is False
    assert value["skip_reason"] == "insufficient_budget"
    assert value["candidate_checks"] == 0


def test_exact_global_end_keeps_final_paid_work(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt, limit=2)
    with trace_for(rt, state, budget): run_operator(rt, state, budget, checks=2)
    entry = payloads(caplog)[0]
    assert state.complete_candidate_evaluation_count == 2
    assert entry["global_stop_reason"] == "candidate_limit_reached"
    assert entry["candidate_checks"] == 2


def test_soft_timeout_keeps_real_global_stop_null(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, clock = budget_for(rt)
    def drive(state, budget, phase, progress, slack, bridges):
        progress.bind(state, (slack, bridges))
        clock.now = phase.soft_deadline
        assert not phase.allows_new_work()
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with trace_for(rt, state, budget): run_operator(rt, state, budget)
    entry = payloads(caplog)[0]
    assert entry["exit_reason"] == "time_slice_exhausted"
    assert entry["global_stop_reason"] is None
    assert entry["entered"] is True


def test_paid_attempt_soft_overrun_and_acceptance_logged(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, clock = budget_for(rt)
    def drive(state, budget, phase, progress, slack, bridges):
        progress.bind(state, (slack, bridges))
        identity = rt.phases.SearchAttemptIdentity("task-0", "plan-0", 0, 1)
        with phase.candidate_attempt(identity) as attempt:
            clock.now = phase.soft_deadline + 2
            assert attempt.allows_continue(identity)
            state.complete_candidate_evaluation_count += 1
            advance(state)
        progress.bind(state, (slack, bridges))
        assert not phase.allows_new_work()
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with trace_for(rt, state, budget): run_operator(rt, state, budget, checks=1)
    entry = payloads(caplog)[0]
    assert entry["accepted_moves"] == 1
    assert entry["soft_overrun_seconds"] == 2
    assert entry["after"]["identity"]["generation"] == 1
    assert budget.search_deadline_monotonic == 1000
    assert budget.final_deadline_monotonic == 1010


@pytest.mark.parametrize("stop", ["USER_CANCELLED", "SEARCH_TIME_LIMIT_REACHED", "SYSTEM_ERROR"])
def test_preexisting_global_stop_logs_all_primary_skips(rt, caplog, stop):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    budget.stop_reason = getattr(rt.contracts.SearchStopReason, stop)
    report = rt.scheduler.run_numeric_stage_schedule(state, budget, pair_scan_slack_weight=0)
    entries = [r for r in payloads(caplog) if r["top_level"]]
    assert [r["name"] for r in entries] == ["basic", "split", "refinement"]
    assert all(r["entered"] is False and r["skip_reason"] == "global_stop" for r in entries)
    assert all(r["candidate_checks"] == 0 for r in entries)
    assert report.stop_reason is getattr(rt.contracts.SearchStopReason, stop)
    assert [r["requested_checks"] for r in entries] == [20, 10, 60]


def test_no_split_has_explicit_no_replay_entry(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    def drive(state, budget, phase, progress, slack, bridges):
        progress.bind(state, (slack, bridges))
        progress.complete = True
        phase.finish()
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with trace_for(rt, state, budget):
        rt.operators.run_split_slice(state, budget, candidate_checks=10,
            time_slice_seconds=10, progress=rt.operators.NumericSplitProgress(), pair_scan_slack_weight=0)
    items = payloads(caplog)
    replay = next(r for r in items if r["name"] == "split:replay")
    assert replay["entered"] is False
    assert replay["skip_reason"] == "no_pending_replay"
    assert state.replay_count == 0
    assert len([r for r in items if r["name"] == "split:scan"]) == 1


def test_child_quality_is_captured_when_child_finishes(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    def drive(state, budget, phase, progress, slack, bridges):
        progress.bind(state, (slack, bridges))
        if progress.operator == "whole": advance(state)
        progress.bind(state, (slack, bridges))
        progress.complete = True
        phase.finish()
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with trace_for(rt, state, budget):
        rt.operators.run_basic_slice(state, budget, candidate_checks=20,
            time_slice_seconds=100, progress=rt.operators.NumericBasicProgress(), pair_scan_slack_weight=0)
    entries = payloads(caplog)
    whole = next(r for r in entries if r["name"] == "basic:whole")
    node = next(r for r in entries if r["name"] == "basic:node")
    assert whole["before"]["identity"]["generation"] == 0
    assert whole["after"]["identity"]["generation"] == node["before"]["identity"]["generation"] == 1
    assert whole["accepted_moves"] == 1 and node["accepted_moves"] == 0


def test_coverage_invalidated_by_later_generation(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    def drive(state, budget, phase, progress, slack, bridges):
        progress.bind(state, (slack, bridges))
        progress.complete = True
        phase.finish()
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with trace_for(rt, state, budget):
        run_operator(rt, state, budget, name="basic:whole")
        advance(state)
        run_operator(rt, state, budget, name="basic:node")
    total = payloads(caplog, "numeric_search_phase_totals")[-1]
    coverage = {x["stage"]: x for x in total["coverage"]}
    assert coverage["basic:whole"]["scan_complete_at_exit"] is True
    assert coverage["basic:whole"]["valid_for_final_state"] is False
    assert coverage["basic:node"]["valid_for_final_state"] is True


def test_top_level_accounting_does_not_sum_children(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt, limit=100, used=7)
    with trace_for(rt, state, budget):
        rt.operators.run_basic_slice(state, budget, candidate_checks=20,
            time_slice_seconds=100, progress=rt.operators.NumericBasicProgress(), pair_scan_slack_weight=0)
    entries = payloads(caplog)
    parent = next(r for r in entries if r["name"] == "basic")
    kids = [r for r in entries if r["parent_call_id"] == parent["call_id"]]
    assert sum(r["candidate_checks"] for r in kids) == parent["candidate_checks"] == 20
    total = payloads(caplog, "numeric_search_phase_totals")[-1]
    assert total["ledger_matches"] is True
    assert total["top_level_candidate_checks"] == 20
    assert total["count_at_exit"] == 27


def test_cancelled_child_has_explicit_unentered_siblings(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    def drive(state, budget, phase, progress, slack, bridges):
        progress.bind(state, (slack, bridges))
        budget.stop_reason = rt.contracts.SearchStopReason.USER_CANCELLED
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with trace_for(rt, state, budget):
        rt.operators.run_basic_slice(state, budget, candidate_checks=20,
            time_slice_seconds=100, progress=rt.operators.NumericBasicProgress(), pair_scan_slack_weight=0)
    kids = [r for r in payloads(caplog) if r["name"] in ("basic:node", "basic:fill", "basic:order")]
    assert len(kids) == 3
    assert all(r["entered"] is False and r["skip_reason"] == "global_stop" for r in kids)
    assert all(r["requested_checks"] is None and r["candidate_checks"] == 0 for r in kids)


def test_exception_preserves_original_and_reports_missing_stages(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    error = RuntimeError("business failure")
    def drive(*a): raise error
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with pytest.raises(RuntimeError) as caught:
        rt.scheduler.run_numeric_stage_schedule(state, budget, pair_scan_slack_weight=0)
    assert caught.value is error
    assert budget._active_phase is None
    entries = payloads(caplog)
    failed = next(r for r in entries if r["name"] == "basic:whole")
    assert failed["error_type"] == "RuntimeError" and failed["scan_complete"] is None
    assert failed["cleanup_completed"] is None
    assert all(next(r for r in entries if r["name"] == name)["entered"] is False
               for name in ("split", "refinement"))
    assert rt.diag._CURRENT.get() is None


def test_error_after_charge_keeps_consumption_and_unknown_grant(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    def drive(state, budget, phase, progress, slack, bridges):
        assert budget.consume_candidate_check()
        raise RuntimeError("after charge")
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with pytest.raises(RuntimeError):
        with trace_for(rt, state, budget): run_operator(rt, state, budget)
    entry = payloads(caplog)[0]
    assert entry["candidate_checks"] == 1 and entry["entered"] is True
    assert entry["granted_checks"] is None  # no returned SearchPhaseResult to invent
    assert payloads(caplog, "numeric_search_phase_totals")[-1]["ledger_matches"] is True


def test_missing_after_evaluation_does_not_forge_improvement(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    def drive(*args):
        state.evaluation = None
        raise RuntimeError("missing evaluation")
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with pytest.raises(RuntimeError):
        with trace_for(rt, state, budget): run_operator(rt, state, budget)
    entry = payloads(caplog)[0]
    assert entry["before"]["evaluation"]["early_node_count"] == 3
    assert entry["after"]["evaluation"] is None
    assert entry["coverage_binding"] is None


def test_logging_failure_does_not_mask_solver_exception(rt, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    error = ValueError("original")
    def drive(*a): raise error
    def logfail(*a, **k): raise OSError("sink failure")
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    monkeypatch.setattr(rt.diag.logger, "info", logfail)
    with pytest.raises(ValueError) as caught:
        with trace_for(rt, state, budget): run_operator(rt, state, budget)
    assert caught.value is error
    assert rt.diag._CURRENT.get() is None


def test_logging_failure_does_not_change_success(rt, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    def fail(*a, **k): raise OSError("sink failure")
    monkeypatch.setattr(rt.diag.logger, "info", fail)
    with trace_for(rt, state, budget) as trace:
        result = run_operator(rt, state, budget)
    assert result.budget.used_candidate_checks == budget.candidate_check_count == 2
    assert trace.failures > 0


def test_cleanup_failure_is_false_not_success(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    error = RuntimeError("cleanup failed")
    def fail(): raise error
    def drive(*a): rt.diag.observed_cleanup(fail)
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with pytest.raises(RuntimeError) as caught:
        with trace_for(rt, state, budget): run_operator(rt, state, budget)
    assert caught.value is error
    entry = payloads(caplog)[0]
    assert entry["cleanup_calls"] == entry["cleanup_failures"] == 1
    assert entry["cleanup_completed"] is False


def test_cleanup_callback_result_is_preserved_without_trace(rt):
    assert rt.diag.observed_cleanup(lambda value: value, 17) == 17


def test_unobserved_direct_entry_has_no_logs(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    run_operator(rt, state, budget)
    assert payloads(caplog) == []
    assert budget.candidate_check_count == 2


def test_decorators_preserve_signatures(rt):
    import inspect
    for f in (rt.scheduler.run_numeric_stage_schedule, rt.operators.run_basic_slice,
              rt.refinement.run_refinement_slice):
        assert inspect.signature(f) == inspect.signature(f.__wrapped__)


def test_foreign_state_does_not_leak_into_outer_trace(rt, caplog):
    state, foreign = state_for(rt), state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget): run_operator(rt, foreign, budget)
    assert not any(r["name"] == "whole" for r in payloads(caplog))
    assert payloads(caplog, "numeric_search_phase_totals")[-1]["ledger_matches"] is False


def test_nested_traces_restore_outer_context(rt, caplog):
    outer, inner = state_for(rt), state_for(rt)
    b1, _ = budget_for(rt)
    b2, _ = budget_for(rt)
    with trace_for(rt, outer, b1) as first:
        with trace_for(rt, inner, b2) as second:
            run_operator(rt, inner, b2)
        assert rt.diag._CURRENT.get() is first
        run_operator(rt, outer, b1)
    assert rt.diag._CURRENT.get() is None
    assert first.schedule_id != second.schedule_id
    totals = payloads(caplog, "numeric_search_phase_totals")
    assert len(totals) == 2 and all(t["ledger_matches"] for t in totals)


def test_json_log_is_single_line_with_exact_integers(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget): run_operator(rt, state, budget, name="whole\nembedded")
    for record in caplog.records:
        if hasattr(record, "numeric_stage_payload"):
            message = record.getMessage()
            assert "\n" not in message
            value = json.loads(message.split(" ", 1)[1])
            assert value["schema"].endswith("v1")
            if "candidate_checks" in value: assert type(value["candidate_checks"]) is int


def test_batch_deltas_and_stale_discard_are_separate(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    batch = NS(**{k: {} for k in rt.diag._BATCH_COUNTERS}, numeric_batch_prepare_seconds=2.0)
    def drive(state, budget, phase, progress, slack, bridges):
        progress.bind(state, (slack, bridges))
        batch.numeric_precomputed["regular:node"] = 8
        batch.numeric_consumed["regular:node"] = 2
        batch.numeric_discarded["regular:node"] = 6
        batch.batch_stale["regular:node"] = 3
        batch.numeric_batch_prepare_seconds = 2.5
        progress.complete = True
        phase.finish()
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with trace_for(rt, state, budget, batch): run_operator(rt, state, budget)
    entry = payloads(caplog)[0]
    assert entry["batch"]["numeric_discarded"] == 6
    assert entry["batch"]["batch_stale"] == 3
    assert entry["batch"]["numeric_batch_prepare_seconds"] == .5
    assert entry["candidate_checks"] == 0


@pytest.mark.parametrize("used,limit", [(0, 100), (7, 108), (0, 1), (0, 3), (15, 16)])
def test_actual_schedule_guarantees_refinement_trace_and_ledger(rt, caplog, used, limit):
    state = state_for(rt)
    budget, _ = budget_for(rt, limit=limit, used=used)
    report = rt.scheduler.run_numeric_stage_schedule(state, budget, pair_scan_slack_weight=0)
    entries = payloads(caplog)
    refinement = next(r for r in entries if r["name"] == "refinement")
    assert refinement["entered"] is True
    assert refinement["candidate_checks"] > 0
    total = payloads(caplog, "numeric_search_phase_totals")[-1]
    assert total["ledger_matches"] is True
    assert total["top_level_candidate_checks"] == report.count_at_exit - used
    assert report.count_at_exit <= limit
    assert len({r["call_id"] for r in entries}) == len(entries)
    for r in entries:
        if r["parent_call_id"] is not None:
            assert r["parent_call_id"] < r["call_id"]


def test_refill_has_distinct_call_identity_and_no_duplicate_fee(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt, limit=100)
    rt.scheduler.run_numeric_stage_schedule(state, budget, pair_scan_slack_weight=0)
    entries = payloads(caplog)
    refill = [r for r in entries if r["refill"] and r["top_level"]]
    assert refill
    assert all(r["name"].endswith(":refill") for r in refill)
    assert payloads(caplog, "numeric_search_phase_totals")[-1]["ledger_matches"] is True


def test_instrumentation_preserves_schedule_counters_clock_calls_and_stop(rt):
    state1, state2 = state_for(rt), state_for(rt)
    b1, c1 = budget_for(rt)
    b2, c2 = budget_for(rt)
    before = rt.scheduler.run_numeric_stage_schedule.__wrapped__(state1, b1, pair_scan_slack_weight=0)
    after = rt.scheduler.run_numeric_stage_schedule(state2, b2, pair_scan_slack_weight=0)
    assert before.summary() == after.summary()
    assert c1.calls == c2.calls
    assert state1.complete_candidate_evaluation_count == state2.complete_candidate_evaluation_count
    assert state1.accepted_move_count == state2.accepted_move_count
    assert b1.stop_reason == b2.stop_reason


def test_refinement_rule_off_keeps_original_skip(rt, caplog):
    state = state_for(rt, early=False)
    budget, _ = budget_for(rt)
    rt.scheduler.run_numeric_stage_schedule(state, budget, pair_scan_slack_weight=0)
    entry = next(r for r in payloads(caplog) if r["name"] == "refinement")
    assert entry["entered"] is False
    assert entry["skip_reason"] == "not_applicable"
    assert entry["candidate_checks"] == 0


def test_changed_evaluation_diagnostic_failure_does_not_change_operator_return(rt, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    def fail(*a): raise RuntimeError("snapshot unavailable")
    monkeypatch.setattr(rt.diag, "evaluation_snapshot", fail)
    with trace_for(rt, state, budget) as trace: result = run_operator(rt, state, budget)
    assert result.budget.used_candidate_checks == 2
    assert trace.failures > 0


def test_not_applicable_is_not_a_completed_scan(rt, caplog):
    state = state_for(rt, early=False)
    budget, _ = budget_for(rt)
    rt.scheduler.run_numeric_stage_schedule(state, budget, pair_scan_slack_weight=0)
    entry = next(r for r in payloads(caplog) if r["name"] == "refinement")
    assert entry["scan_complete"] is False
    assert entry["not_applicable_at_exit"] is True
    assert entry["pending_resume"] is False


def test_already_complete_progress_is_explicit_skip(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    p = rt.operators.NumericOperatorProgress("whole")
    p.bind(state, (0, 2))
    p.complete = True
    # Restore only the actual early-return path; do not execute business scans.
    original = ast.parse((CORE / "_numeric_stage_operators.py").read_text())
    fn = next(n for n in original.body if isinstance(n, ast.FunctionDef) and n.name == "_drive_operator")
    namespace = dict(rt.operators.__dict__)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "operators.py", "exec"), namespace)
    old = rt.operators._drive_operator
    rt.operators._drive_operator = namespace["_drive_operator"]
    try:
        with trace_for(rt, state, budget): run_operator(rt, state, budget, progress=p)
    finally:
        rt.operators._drive_operator = old
    entry = payloads(caplog)[0]
    assert entry["entered"] is False
    assert entry["skip_reason"] == "already_complete_for_generation"
    assert entry["scan_complete"] is True


def test_replay_children_without_pending_split_are_not_waiting(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget):
        rt.operators.run_replay_slice(state, budget, candidate_checks=4,
            time_slice_seconds=10, progress=rt.operators.NumericSplitProgress(), pair_scan_slack_weight=0)
    children = [r for r in payloads(caplog) if r["parent_call_id"] is not None]
    assert len(children) == 4
    assert all(not r["pending_resume"] and not r["entered"] for r in children)


def test_accounting_missing_interval_not_reported_as_matching(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget):
        run_operator(rt, state, budget)
        budget.consume_candidate_check()  # deliberately unobserved work
    total = payloads(caplog, "numeric_search_phase_totals")[-1]
    assert total["ledger_matches"] is False
    assert total["actual_candidate_checks"] == 3
    assert total["top_level_candidate_checks"] == 2


def test_unavailable_batch_counters_stay_null(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget, batch=NS()): run_operator(rt, state, budget)
    entry = payloads(caplog)[0]
    assert entry["batch"] is None and entry["batch_status"] == "unavailable"
    assert entry["diagnostic_failures"] > 0


def test_global_deadline_during_work_is_not_a_phase_timeout(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, clock = budget_for(rt)
    def drive(state, budget, phase, progress, slack, bridges):
        progress.bind(state, (slack, bridges))
        clock.now = budget.search_deadline_monotonic
        assert not phase.allows_new_work()
    monkeypatch.setattr(rt.operators, "_drive_operator", drive)
    with trace_for(rt, state, budget): run_operator(rt, state, budget)
    entry = payloads(caplog)[0]
    assert entry["exit_reason"] == "global_stop"
    assert entry["global_stop_reason"] == "search_time_limit_reached"
    assert budget.allows_finalization()


def test_zero_scope_time_does_not_claim_entered(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget): run_operator(rt, state, budget, seconds=0)
    entry = payloads(caplog)[0]
    assert entry["entered"] is False and entry["candidate_checks"] == 0
    assert entry["skip_reason"] == "time_slice_exhausted"


def test_actual_refinement_cleanup_calls_recorded(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget):
        rt.refinement.run_refinement_slice(state, budget, candidate_checks=2,
            time_slice_seconds=10, progress=rt.refinement.NumericRefinementProgress())
    entry = payloads(caplog)[0]
    assert entry["cleanup_calls"] >= 1
    assert entry["cleanup_completed"] is True
    assert entry["candidate_checks"] == 2


def test_actual_refinement_cleanup_error_not_swallowed(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    failure = RuntimeError("pool cleanup")
    class BadPool(rt.Pool):
        def release(self): raise failure
    monkeypatch.setattr(rt.legacy, "NumericCandidateBatchWorkspace", BadPool)
    with pytest.raises(RuntimeError) as caught:
        with trace_for(rt, state, budget):
            rt.refinement.run_refinement_slice(state, budget, candidate_checks=2,
                time_slice_seconds=10, progress=rt.refinement.NumericRefinementProgress())
    assert caught.value is failure
    entry = payloads(caplog)[0]
    assert entry["cleanup_completed"] is False
    assert entry["candidate_checks"] == 2
    assert entry["batch_status"] == "partial"


def _load_entry(rt, path, name, state, checkpoint, calls):
    syntax = ast.parse((CORE / path).read_text(encoding="utf-8"))
    fn = next(n for n in syntax.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(__package__=rt.package, perf_counter=lambda: 0.0,
        NumericSearchState=NS(start=lambda *a: state),
        NumericSearchCheckpoint=NS(capture=lambda *a: checkpoint),
        _validate_search_inputs=lambda *a: None,
        SolveRuntimeBudget=rt.budget_module.SolveRuntimeBudget, NumericValueError=ValueError,
        SearchStopReason=rt.contracts.SearchStopReason, logger=logging.getLogger(rt.diag.logger.name))
    def basic(state, budget, **kwargs):
        calls.append("basic")
        budget.stop_reason = rt.contracts.SearchStopReason.LOCAL_SEARCH_COMPLETE
    namespace.update(_run_numeric_local_search=basic,
        improve_numeric_whole_chain=lambda *a, **k: calls.append("whole"),
        improve_numeric_real_node_relocation=lambda *a, **k: calls.append("node"),
        improve_numeric_controlled_split=lambda *a, **k: calls.append("split"))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), namespace)
    return namespace[name], namespace


@pytest.mark.parametrize("name,expected", [
    ("run_numeric_first_search_prefix", ["whole", "node"]),
    ("run_numeric_search_with_split_replay", ["basic", "split"]),
])
def test_legacy_entries_keep_coverage_and_return_contract(rt, caplog, name, expected):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    checkpoint, calls = object(), []
    entry, _ = _load_entry(rt, "_numeric_search.py", name, state, checkpoint, calls)
    result = entry(state.task, state.program, state.quality, object(), budget, pair_scan_slack_weight=0)
    assert result == (state, checkpoint)
    assert calls == expected
    assert payloads(caplog) == []


def test_production_entry_preserves_return_and_existing_summaries(rt, caplog):
    state = state_for(rt)
    budget, _ = budget_for(rt, limit=10)
    checkpoint = object()
    entry, namespace = _load_entry(rt, "_numeric_refinement.py", "run_numeric_serial_search", state, checkpoint, [])
    class Counters:
        def __init__(self):
            for key in rt.diag._BATCH_COUNTERS: setattr(self, key, {})
            self.numeric_batch_prepare_seconds = 0.0
        def snapshot(self): return {key: getattr(self, key) for key in rt.diag._BATCH_COUNTERS}
    namespace.update(NumericRefinementDiagnostics=Counters, _SERIAL_BATCH_SIZE=8)
    assert entry(state.task, state.program, state.quality, object(), budget,
                 pair_scan_slack_weight=0) == (state, checkpoint)
    assert payloads(caplog)
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("numeric_refinement_summary ") for m in messages)
    assert any(m.startswith("numeric_search_budget_summary ") for m in messages)


def test_schema_fields_present_for_entered_skipped_and_failed(rt, caplog, monkeypatch):
    state = state_for(rt)
    budget, _ = budget_for(rt)
    with trace_for(rt, state, budget):
        run_operator(rt, state, budget)
        run_operator(rt, state, budget, checks=0, name="zero")
        old = rt.operators._drive_operator
        def fail(*a): raise RuntimeError("failure")
        monkeypatch.setattr(rt.operators, "_drive_operator", fail)
        with pytest.raises(RuntimeError): run_operator(rt, state, budget, name="error")
        monkeypatch.setattr(rt.operators, "_drive_operator", old)
    required = {"schema", "schedule_id", "schedule_version", "allocations", "call_id", "parent_call_id",
        "entered", "skip_reason", "before", "after", "candidate_checks", "complete_evaluations",
        "accepted_moves", "global_stop_reason", "scan_complete", "coverage_binding", "pending_resume",
        "pending_replay", "batch", "cleanup_completed", "error_type", "requested_checks", "granted_checks"}
    assert all(required <= set(value) for value in payloads(caplog))
