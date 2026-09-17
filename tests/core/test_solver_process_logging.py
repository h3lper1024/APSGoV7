"""Live process logs reuse accepted facts without changing the search workload."""

import logging
from dataclasses import fields, replace
from decimal import Decimal
from unittest.mock import patch

import pytest

from apsgo_scheduler.app import service
from apsgo_scheduler.core import (
    controlled_split,
    final_audit,
    initial_solution,
    neighborhoods,
    solver,
)
from apsgo_scheduler.core.contracts import CoreAuditStatus, RuleScope, SearchStopReason, SolveStatus
from apsgo_scheduler.core.model import SearchState
from tests.app.test_input_normalizer import make_request
from tests.core.graph.test_construction_order import node
from tests.core.search.test_complete_candidate_lifecycle import Stop, attempt, merged, setup
from tests.core.test_quality_key import SeverityRule, ruleset
from tests.core.test_solver_determinism import result_payload
from tests.core.test_solver_orchestration import solver_case, width_split_solver_case


@pytest.fixture(autouse=True)
def capture_process_logs(caplog, monkeypatch):
    parent = logging.getLogger("apsgo_scheduler")
    monkeypatch.setattr(parent, "handlers", [])
    monkeypatch.setattr(parent, "propagate", True)
    caplog.set_level(logging.INFO, logger=parent.name)


def records(caplog, event, stage=None, logger=None):
    return [
        record
        for record in caplog.records
        if getattr(record, "solver_event", None) == event
        and (stage is None or record.solver_details.get("stage") == stage)
        and (logger is None or record.name == logger)
    ]


def test_stage_starts_and_initial_quality_are_visible_before_work_returns(monkeypatch, caplog):
    values = width_split_solver_case()
    original_graph = solver.build_construction_dag
    original_initial = solver.construct_initial_plan
    original_local = solver.run_local_search
    original_audit = solver.audit_core_without_search_cache
    captured = {}

    def graph(*args, **kwargs):
        assert records(caplog, "solver_stage_started", "construction_graph")
        assert records(caplog, "solver_stage_finished", "input_validation")
        assert not records(caplog, "solver_stage_finished", "construction_graph")
        return original_graph(*args, **kwargs)

    def initial(*args):
        assert records(caplog, "solver_stage_started", "initial_solution")
        captured["initial"] = original_initial(*args)
        return captured["initial"]

    def local(state, context):
        (record,) = records(caplog, "solver_stage_finished", "initial_solution")
        assert record.solver_details["chain_count"] == len(state.current_plan.chains)
        assert tuple(value for _, value in record.solver_details["quality"]) == (
            captured["initial"].candidate.search_evaluation.quality_key
        )
        assert record.solver_details["complete_candidate_evaluation_count"] == 0
        assert records(caplog, "solver_stage_started", "local_search")
        return original_local(state, context)

    def audit(*args):
        assert records(caplog, "solver_stage_finished", "width_optimization")
        assert records(caplog, "solver_stage_started", "core_audit")
        return original_audit(*args)

    monkeypatch.setattr(solver, "build_construction_dag", graph)
    monkeypatch.setattr(solver, "construct_initial_plan", initial)
    monkeypatch.setattr(solver, "run_local_search", local)
    monkeypatch.setattr(solver, "audit_core_without_search_cache", audit)
    with patch.object(
        initial_solution, "evaluate_plan", wraps=initial_solution.evaluate_plan
    ) as evaluated:
        result = solver.solve(*values)
    assert result.status is SolveStatus.SUCCESS
    evaluated.assert_called_once()
    for stage, duration in result.metrics.stage_duration_seconds.items():
        (record,) = records(caplog, "solver_stage_finished", stage)
        assert record.solver_details["stage_seconds"] == duration
        assert record.solver_details["status"] == "completed"
        assert any("\u4e00" <= char <= "\u9fff" for char in record.solver_details["stage_name"])
    (replay_start,) = records(caplog, "solver_stage_started", "local_search_replay")
    (replay_end,) = records(caplog, "solver_stage_finished", "local_search_replay")
    assert replay_start.solver_details["replay"] is replay_end.solver_details["replay"] is True
    assert replay_end.solver_details["parent_stage"] == "controlled_split_and_replay"
    assert replay_end.solver_details["stage_seconds"] >= 0
    assert "local_search_replay" not in result.metrics.stage_duration_seconds
    accepted = records(caplog, "solver_move_accepted")
    assert len(accepted) == result.metrics.accepted_move_count == len(result.trace)
    assert [record.solver_details for record in accepted] == [
        {part.name: getattr(trace, part.name) for part in fields(trace)} for trace in result.trace
    ]


def test_acceptance_log_observes_both_committed_state_and_published_trace(caplog, monkeypatch):
    state, context = setup()
    observed = []

    class Observer(logging.Handler):
        def emit(self, record):
            if getattr(record, "solver_event", None) == "solver_move_accepted":
                observed.append(
                    (
                        state.accepted_move_count,
                        context.accepted_move_traces,
                        state.current_evaluation.quality_key,
                    )
                )

    monkeypatch.setattr(neighborhoods.logger, "handlers", [Observer()])
    assert context.factory.budget.consume_candidate_check()
    assert attempt(state, context, merged(state))
    assert observed == [(1, context.accepted_move_traces, state.current_evaluation.quality_key)]
    assert not attempt(state, context, state.current_plan.chains)
    assert len(records(caplog, "solver_move_accepted")) == 1


@pytest.mark.parametrize("mode", ("cancel", "commit_error"))
def test_uncommitted_candidate_never_logs_acceptance(mode, monkeypatch, caplog):
    state, context = setup()
    candidate = merged(state)
    if mode == "cancel":
        token = Stop()
        context.factory.budget.cancellation = token
        original = neighborhoods._evaluate_candidate_plan

        def evaluate_then_cancel(*args):
            evaluated = original(*args)
            token.active = True
            return evaluated

        monkeypatch.setattr(neighborhoods, "_evaluate_candidate_plan", evaluate_then_cancel)
        assert not attempt(state, context, candidate)
    else:

        def broken(*args, **kwargs):
            raise RuntimeError("commit failure")

        monkeypatch.setattr(SearchState, "commit_accepted", broken)
        with pytest.raises(RuntimeError, match="commit failure"):
            attempt(state, context, candidate)
    assert state.accepted_move_count == 0 and context.accepted_move_traces == ()
    assert not records(caplog, "solver_move_accepted")


@pytest.mark.parametrize("termination", ("cancel", "time"))
def test_interrupted_graph_and_unentered_phases_are_not_reported_completed(
    termination, monkeypatch, caplog
):
    token, now = Stop(), [1.0]
    values = solver_case(cancellation=token, clock=lambda: now[0])
    original = solver.build_construction_dag

    def interrupted(*args, **kwargs):
        if termination == "cancel":
            token.active = True
        else:
            now[0] = values[-1].search_deadline_monotonic
        return original(*args, **kwargs)

    monkeypatch.setattr(solver, "build_construction_dag", interrupted)
    result = solver.solve(*values)
    (record,) = records(caplog, "solver_stage_finished", "construction_graph")
    assert record.solver_details["status"] == (
        "cancelled" if termination == "cancel" else "truncated"
    )
    assert record.solver_details["complete"] is False
    assert records(caplog, "solver_stage_skipped", "minimum_path_cover")
    assert not records(caplog, "solver_stage_started", "minimum_path_cover")
    assert "minimum_path_cover" not in result.metrics.stage_duration_seconds
    assert result.diagnostic_candidate is None


def test_candidate_limit_and_missing_width_objective_have_distinct_records(caplog):
    limited = solver.solve(*width_split_solver_case(limit=0))
    assert limited.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert any(
        record.solver_details["status"] == "truncated"
        for record in records(caplog, "solver_stage_finished", "controlled_split_and_replay")
    )
    caplog.clear()
    completed = solver.solve(*solver_case())
    (record,) = records(caplog, "solver_stage_skipped", "width_optimization")
    assert record.solver_details["reason"] == "objective_not_enabled"
    assert "width_optimization" not in completed.metrics.stage_duration_seconds
    (replay,) = records(caplog, "solver_stage_skipped", "local_search_replay")
    assert replay.solver_details["reason"] == "no_accepted_split"


@pytest.mark.parametrize(
    "target,stage",
    (
        ("build_construction_dag", "construction_graph"),
        ("run_local_search", "local_search"),
        ("run_controlled_order_split", "controlled_split_and_replay"),
        ("run_width_optimization", "width_optimization"),
    ),
)
def test_core_exception_logs_stack_and_skips_remaining_phases(target, stage, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("process logging probe")

    monkeypatch.setattr(solver, target, broken)
    result = solver.solve(*width_split_solver_case())
    assert result.status is SolveStatus.FAILED
    (record,) = records(caplog, "solver_stage_failed", stage)
    assert record.exc_info[0] is RuntimeError
    assert record.solver_details["status"] == "error"
    assert record.solver_details["stage_seconds"] >= 0
    assert not records(caplog, "solver_stage_finished", stage)
    assert records(caplog, "solver_stage_skipped", "core_audit")


def test_replay_exception_keeps_replay_identity_and_the_accepted_split(monkeypatch, caplog):
    def broken(*args):
        raise RuntimeError("replay failed")

    monkeypatch.setattr(controlled_split, "run_local_search", broken)
    result = solver.solve(*width_split_solver_case())
    (record,) = records(caplog, "solver_stage_failed", "local_search_replay")
    assert record.exc_info[0] is RuntimeError and record.solver_details["replay"] is True
    assert result.metrics.accepted_split_count == result.metrics.accepted_move_count == 1
    assert len(records(caplog, "solver_move_accepted")) == 1
    assert result.release is None and result.diagnostic_candidate is not None


def test_internal_audit_exception_keeps_stack_at_the_catch_site(monkeypatch, caplog):
    def broken(*args):
        raise RuntimeError("audit probe")

    monkeypatch.setattr(final_audit, "_audit_structure", broken)
    result = solver.solve(*solver_case())
    assert result.core_audit.status is CoreAuditStatus.ERROR and result.release is None
    (record,) = records(caplog, "solver_stage_exception", "core_audit")
    assert record.exc_info[0] is RuntimeError and record.name == final_audit.__name__
    (finished,) = records(caplog, "solver_stage_finished", "core_audit")
    assert finished.solver_details["status"] == "error"
    assert finished.solver_details["audit_passed"] is False


def test_business_audit_failure_is_completed_inspection_not_an_exception(caplog):
    rule = SeverityRule("severity", "severity", RuleScope.CHAIN, True, "1", {})
    member = replace(node("real"), rule_attributes={"severity": Decimal(1)})
    result = solver.solve(*solver_case(nodes=(member,), rule_set=ruleset((rule,))))
    assert result.status is SolveStatus.PUBLISHABLE_WITH_VIOLATIONS
    assert result.core_audit.status is CoreAuditStatus.COMPLETED
    assert result.core_audit.integrity_passed and result.release is not None
    assert result.core_audit.writeback_blocking_violation_count == 0
    (record,) = records(caplog, "solver_stage_finished", "core_audit")
    assert record.solver_details["status"] == "failed"
    assert record.solver_details["audit_status"] == "completed"
    assert record.solver_details["audit_passed"] is False
    assert not record.exc_info
    assert not records(caplog, "solver_stage_exception")


def test_fixed_workload_and_clock_reads_match_with_disabled_enabled_and_broken_logs(monkeypatch):
    parent = logging.getLogger("apsgo_scheduler")
    outcomes, observations = [], []

    class BrokenHandler(logging.Handler):
        attempts = 0

        def emit(self, record):
            self.attempts += 1
            raise OSError("diagnostic disk failure")

    broken = BrokenHandler()
    for mode in ("disabled", "enabled", "broken"):
        reads = [0, 0]

        def clock():
            reads[0] += 1
            return 1.0

        class Token:
            def is_cancelled(self):
                reads[1] += 1
                return False

        values = width_split_solver_case(clock=clock, cancellation=Token())
        monkeypatch.setattr(parent, "handlers", [broken] if mode == "broken" else [])
        parent.setLevel(logging.WARNING if mode == "disabled" else logging.INFO)
        with patch.object(
            initial_solution, "evaluate_plan", wraps=initial_solution.evaluate_plan
        ) as initial:
            with patch.object(
                neighborhoods,
                "_evaluate_candidate_plan",
                wraps=neighborhoods._evaluate_candidate_plan,
            ) as candidates:
                with patch.object(
                    final_audit, "evaluate_plan", wraps=final_audit.evaluate_plan
                ) as audited:
                    result = solver.solve(*values)
        outcomes.append(result_payload(result))
        observations.append(
            (tuple(reads), initial.call_count, candidates.call_count, audited.call_count)
        )
    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert observations[0] == observations[1] == observations[2]
    assert observations[0][1] == observations[0][3] == 1
    assert broken.attempts > 0


def test_public_service_logs_both_audits_live_and_skips_unavailable_work(monkeypatch, caplog):
    monkeypatch.setattr(service, "monotonic", lambda: 100.0)
    original = service.audit_result_contract

    def audit(*args):
        assert records(caplog, "solver_stage_started", "result_contract_audit")
        assert records(caplog, "solver_stage_finished", "core_audit")
        assert not records(caplog, "solver_stage_finished", "result_contract_audit")
        return original(*args)

    monkeypatch.setattr(service, "audit_result_contract", audit)
    result = service.solve_request(make_request())
    assert result.release is not None
    for stage in (
        "request_preparation",
        "rule_loading",
        "input_normalization",
        "core_solve",
        "result_assembly",
        "result_contract_audit",
        "result_sealing",
    ):
        (record,) = records(caplog, "solver_stage_finished", stage, service.__name__)
        assert (
            record.solver_details["stage_seconds"]
            == result.run_manifest.stage_duration_seconds[stage]
        )
        assert record.solver_details["request_id"] == result.request_id
    caplog.clear()
    request = make_request()
    invalid = replace(request, orders=(replace(request.orders[0], weight=Decimal(-1)),))
    failed = service.solve_request(invalid)
    assert failed.status is SolveStatus.FAILED
    assert records(caplog, "solver_stage_skipped", "core_solve", service.__name__)
    assert not records(caplog, "solver_stage_started", "core_solve", service.__name__)
