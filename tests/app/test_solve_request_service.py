"""Small real public pipelines plus bounded preparation, failure and signing probes."""

from dataclasses import fields, replace
from decimal import Decimal

import pytest

from apsgo_scheduler.api.request import PeriodInput, RuleDefinitionSpec, fingerprint_public_request
from apsgo_scheduler.api.result import ResultAuditStatus, SchedulingResult
from apsgo_scheduler.app import service
from apsgo_scheduler.core.contracts import (
    CoreAuditStatus,
    DiagnosticPhase,
    RuleScope,
    SearchStopReason,
    SolveStatus,
    fingerprint,
)
from tests.app.test_input_normalizer import make_order, make_request, make_spec

D = Decimal


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Cancellation:
    cancelled = False

    def is_cancelled(self):
        return self.cancelled


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(service, "monotonic", value)
    return value


def observe_core(monkeypatch):
    calls = []
    original = service.core.solve

    def observed(*args):
        result = original(*args)
        calls.append((args, result))
        return result

    monkeypatch.setattr(service.core, "solve", observed)
    return calls


def test_real_public_pipeline_uses_one_core_call_and_preserves_audited_materials(
    clock, monkeypatch
):
    request = make_request()
    calls = observe_core(monkeypatch)
    result = service.solve_request(request)
    assert len(calls) == 1
    (problem, rules, policy, runtime), solved = calls[0]
    assert result.status is SolveStatus.SUCCESS
    assert result.core_audit is solved.core_audit
    assert result.audit_report.status is ResultAuditStatus.COMPLETED and result.audit_report.passed
    assert result.release.plan is solved.release.canonical_plan
    assert result.release.evaluation is solved.release.audited_evaluation
    assert result.release.resource_facts is solved.release.resource_facts
    assert result.release.core_release_fingerprint == solved.release.release_fingerprint
    assert result.diagnostic_candidate is solved.diagnostic_candidate
    assert runtime.started_at_monotonic == 100.0 and runtime.clock is clock
    assert result.run_manifest.request_fingerprint == fingerprint_public_request(request)
    assert result.run_manifest.problem_fingerprint == problem.input_fingerprint
    assert result.run_manifest.rule_set_fingerprint == rules.fingerprint
    assert result.run_manifest.policy_fingerprint == fingerprint(policy)
    assert result.run_manifest.trace_fingerprint == fingerprint(solved.trace)
    assert result.run_manifest.counters == {
        field.name: getattr(solved.metrics, field.name)
        for field in fields(solved.metrics)
        if field.name != "stage_duration_seconds"
    }
    assert result.run_manifest.algorithm_version == "path-cover-local-search-v1"
    assert result.run_manifest.code_revision == "unversioned"
    assert result.run_manifest.optimality_proven is False
    assert result.run_manifest.stage_duration_seconds["service_to_result_audit_seconds"] == 0
    assert "result_sealing" in result.run_manifest.stage_duration_seconds


def test_request_hashing_time_is_charged_to_original_entry_and_observed_total(clock, monkeypatch):
    original = service.fingerprint_public_request
    calls = observe_core(monkeypatch)

    def slower(request):
        clock.now += 1.5
        return original(request)

    monkeypatch.setattr(service, "fingerprint_public_request", slower)
    result = service.solve_request(make_request())
    runtime = calls[0][0][-1]
    assert runtime.started_at_monotonic == 100.0
    assert runtime.search_deadline_monotonic == 110.0
    assert runtime.final_deadline_monotonic == 112.0
    assert result.run_manifest.stage_duration_seconds["service_to_result_audit_seconds"] == D("1.5")


def test_same_request_is_deterministic_despite_stage_duration_changes(clock, monkeypatch):
    request = make_request()
    first = service.solve_request(request)
    tick = iter(range(10000))
    monkeypatch.setattr(service, "perf_counter", lambda: next(tick) / 10)
    second = service.solve_request(request)
    assert first.run_manifest.stage_duration_seconds != second.run_manifest.stage_duration_seconds
    assert (
        first.run_manifest.deterministic_run_fingerprint
        == second.run_manifest.deterministic_run_fingerprint
    )
    assert first.result_fingerprint == second.result_fingerprint


@pytest.mark.parametrize("invalid", (None, {}, object()))
def test_wrong_request_type_is_explicit_argument_error(clock, invalid):
    with pytest.raises(ValueError, match="SchedulingRequest"):
        service.solve_request(invalid)


def test_wrong_cancellation_shape_is_explicit_argument_error(clock):
    with pytest.raises(ValueError, match="is_cancelled"):
        service.solve_request(make_request(), object())


def test_bad_rule_and_input_are_aggregated_after_identifier_normalization(clock, monkeypatch):
    specification = make_spec()
    specification = make_spec(rules=(replace(specification.rules[0], rule_type="UnknownRule"),))
    request = make_request(
        request_id=" ",
        rule_set_spec=specification,
        orders=(make_order(source_period=" P0 ", weight=D("0")),),
        periods=(PeriodInput(" P0 ", 0),),
    )
    calls = observe_core(monkeypatch)
    result = service.solve_request(request)
    assert calls == [] and result.status is SolveStatus.FAILED
    assert result.stop_reason is SearchStopReason.INPUT_INVALID
    assert result.request_id == " "
    assert result.core_audit.status is CoreAuditStatus.NOT_RUN
    assert result.audit_report.status is ResultAuditStatus.NOT_RUN
    assert result.diagnostic_candidate is result.release is None
    assert [item.code for item in result.issues] == [
        "empty_identity",
        "non_positive_weight",
        "unknown_enabled_rule",
    ]
    assert all(item.field_path for item in result.issues)
    assert result.run_manifest.problem_fingerprint is None
    assert result.run_manifest.rule_set_fingerprint is None
    assert result.run_manifest.policy_fingerprint == fingerprint(request.policy)


def test_semantically_invalid_typed_policy_still_collects_safe_input_errors(clock, monkeypatch):
    request = make_request(orders=(make_order(weight=D("0")),))
    object.__setattr__(request.policy, "candidate_check_limit", -1)
    calls = observe_core(monkeypatch)
    result = service.solve_request(request)
    assert calls == [] and result.stop_reason is SearchStopReason.INPUT_INVALID
    assert {item.code for item in result.issues} == {"invalid_solver_policy", "non_positive_weight"}
    assert result.run_manifest.policy_fingerprint is None
    assert result.run_manifest.rule_set_fingerprint == request.rule_set_spec.fingerprint


@pytest.mark.parametrize("dimension", ("request_id", "contract_version"))
@pytest.mark.parametrize("interrupt", ("cancel", "hard_deadline"))
def test_invalid_empty_identity_can_still_report_preflight_stop(
    clock, monkeypatch, dimension, interrupt
):
    request = replace(make_request(), **{dimension: " "})
    cancellation = Cancellation()
    if interrupt == "cancel":
        cancellation.cancelled = True
    else:
        original = service.fingerprint_public_request

        def elapsed(value):
            clock.now = 112.0
            return original(value)

        monkeypatch.setattr(service, "fingerprint_public_request", elapsed)
    result = service.solve_request(request, cancellation)
    assert getattr(result, dimension) == " " and result.release is None
    assert result.core_audit.status is CoreAuditStatus.NOT_RUN
    assert result.audit_report.status is ResultAuditStatus.NOT_RUN
    assert result.status is (
        SolveStatus.CANCELLED if interrupt == "cancel" else SolveStatus.NO_COMPLETE_PLAN
    )


@pytest.mark.parametrize(
    "stage",
    (
        "load_rule_set",
        "normalize_input",
        "core",
        "assemble_draft_scheduling_result",
        "audit_result_contract",
        "seal_scheduling_result",
    ),
)
@pytest.mark.parametrize("interrupt", ("cancel", "hard_deadline"))
def test_each_stage_observes_same_cancellation_and_hard_deadline(
    clock, monkeypatch, stage, interrupt
):
    cancellation = Cancellation()
    calls = observe_core(monkeypatch)
    owner, name = (service.core, "solve") if stage == "core" else (service, stage)
    original = getattr(owner, name)

    def stop_after(*args, **kwargs):
        result = original(*args, **kwargs)
        if interrupt == "cancel":
            cancellation.cancelled = True
        else:
            clock.now = 112.0
        return result

    monkeypatch.setattr(owner, name, stop_after)
    result = service.solve_request(make_request(), cancellation)
    assert result.release is None
    assert result.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if interrupt == "cancel"
        else SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
    )
    assert result.status is (
        SolveStatus.CANCELLED
        if interrupt == "cancel"
        else SolveStatus.COMPLETE_NOT_PUBLISHABLE
        if calls
        else SolveStatus.NO_COMPLETE_PLAN
    )
    if calls:
        assert result.diagnostic_candidate is calls[0][1].diagnostic_candidate
    else:
        assert result.diagnostic_candidate is None


def test_search_deadline_before_core_does_not_start_core(clock, monkeypatch):
    calls = observe_core(monkeypatch)
    original = service.normalize_input

    def normalize(*args):
        result = original(*args)
        clock.now = 110.0
        return result

    monkeypatch.setattr(service, "normalize_input", normalize)
    result = service.solve_request(make_request())
    assert calls == []
    assert result.status is SolveStatus.NO_COMPLETE_PLAN
    assert result.stop_reason is SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    assert result.run_manifest.problem_fingerprint is not None


def test_late_cancel_after_final_public_value_refresh_never_leaks_release(clock, monkeypatch):
    cancellation = Cancellation()
    original = service.replace

    def refreshed(value, **changes):
        result = original(value, **changes)
        if isinstance(value, SchedulingResult):
            cancellation.cancelled = True
        return result

    monkeypatch.setattr(service, "replace", refreshed)
    result = service.solve_request(make_request(), cancellation)
    assert result.status is SolveStatus.CANCELLED and result.release is None
    assert result.audit_report.passed


@pytest.mark.parametrize(
    "stage",
    (
        "load_rule_set",
        "normalize_input",
        "core",
        "assemble_draft_scheduling_result",
        "audit_result_contract",
        "seal_scheduling_result",
    ),
)
def test_unexpected_exceptions_are_logged_and_return_explicit_failure(
    clock, monkeypatch, caplog, stage
):
    owner, name = (service.core, "solve") if stage == "core" else (service, stage)

    def broken(*args, **kwargs):
        raise RuntimeError("observable failure")

    monkeypatch.setattr(owner, name, broken)
    result = service.solve_request(make_request())
    assert (
        result.status is SolveStatus.FAILED and result.stop_reason is SearchStopReason.SYSTEM_ERROR
    )
    assert result.release is None
    assert any(
        item.code == "service_exception" and "observable failure" in item.message
        for item in result.issues
    )
    assert any(record.exc_info is not None for record in caplog.records)
    if stage == "audit_result_contract":
        assert result.audit_report.status is ResultAuditStatus.ERROR
    if stage in (
        "assemble_draft_scheduling_result",
        "audit_result_contract",
        "seal_scheduling_result",
    ):
        assert result.diagnostic_candidate is not None and result.core_audit.passed


@pytest.mark.parametrize("stage", ("load_rule_set", "assemble_draft_scheduling_result"))
def test_explicit_cancellation_wins_when_a_stage_also_raises(clock, monkeypatch, stage):
    cancellation = Cancellation()

    def cancelled_failure(*args):
        cancellation.cancelled = True
        raise RuntimeError("cancelled failure")

    monkeypatch.setattr(service, stage, cancelled_failure)
    result = service.solve_request(make_request(), cancellation)
    assert result.status is SolveStatus.CANCELLED
    assert result.stop_reason is SearchStopReason.USER_CANCELLED and result.release is None
    assert any(item.code == "service_exception" for item in result.issues)
    assert (result.diagnostic_candidate is not None) == (
        stage == "assemble_draft_scheduling_result"
    )


def test_cancellation_during_failed_result_construction_is_checked_once_afterward(
    clock, monkeypatch
):
    cancellation = Cancellation()
    original = service.SchedulingResult
    statuses = []

    def failed_stage(*args):
        raise RuntimeError("stage failure")

    def result_then_cancel(**values):
        result = original(**values)
        statuses.append(result.status)
        if result.status is SolveStatus.FAILED:
            cancellation.cancelled = True
        return result

    monkeypatch.setattr(service, "load_rule_set", failed_stage)
    monkeypatch.setattr(service, "SchedulingResult", result_then_cancel)
    result = service.solve_request(make_request(), cancellation)
    assert result.status is SolveStatus.CANCELLED
    assert result.stop_reason is SearchStopReason.USER_CANCELLED and result.release is None
    assert statuses == [SolveStatus.FAILED, SolveStatus.CANCELLED]
    assert any(item.code == "service_exception" for item in result.issues)


@pytest.mark.parametrize(
    "stage",
    (
        "load_rule_set",
        "normalize_input",
        "core",
        "assemble_draft_scheduling_result",
        "audit_result_contract",
        "seal_scheduling_result",
    ),
)
def test_invalid_internal_return_type_is_contained_without_partial_publication(
    clock, monkeypatch, stage
):
    owner, name = (service.core, "solve") if stage == "core" else (service, stage)
    monkeypatch.setattr(owner, name, lambda *args, **kwargs: object())
    result = service.solve_request(make_request())
    assert result.status is SolveStatus.FAILED and result.release is None
    assert result.stop_reason is SearchStopReason.SYSTEM_ERROR
    assert any(item.code == "service_exception" for item in result.issues)


@pytest.mark.parametrize("value", (None, 1, "true"))
def test_invalid_token_return_is_not_disguised_as_cancellation(clock, value):
    class BadToken:
        def is_cancelled(self):
            return value

    result = service.solve_request(make_request(), BadToken())
    assert result.status is SolveStatus.FAILED
    assert result.stop_reason is SearchStopReason.SYSTEM_ERROR


def test_first_clock_exception_is_logged_and_has_a_request_identity(monkeypatch, caplog):
    def broken():
        raise RuntimeError("clock failure")

    monkeypatch.setattr(service, "monotonic", broken)
    request = make_request()
    result = service.solve_request(request)
    assert result.status is SolveStatus.FAILED and result.release is None
    assert result.run_manifest.request_fingerprint == fingerprint_public_request(request)
    assert result.core_audit.status is CoreAuditStatus.NOT_RUN
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), True, D("100")))
def test_invalid_entry_clock_is_a_system_error_not_a_policy_error(monkeypatch, value):
    monkeypatch.setattr(service, "monotonic", lambda: value)
    result = service.solve_request(make_request())
    assert result.status is SolveStatus.FAILED
    assert result.stop_reason is SearchStopReason.SYSTEM_ERROR
    assert [item.code for item in result.issues] == ["service_exception"]
    assert result.run_manifest.policy_fingerprint is None


@pytest.mark.parametrize("value", (float("nan"), 99.0))
def test_invalid_observed_total_does_not_poison_failure_manifest(clock, monkeypatch, value):
    original = service.audit_result_contract

    def broken_clock_after_audit(*args):
        report = original(*args)
        clock.now = value
        return report

    monkeypatch.setattr(service, "audit_result_contract", broken_clock_after_audit)
    result = service.solve_request(make_request())
    assert result.status is SolveStatus.FAILED and result.release is None
    assert result.stop_reason is SearchStopReason.SYSTEM_ERROR
    assert result.audit_report.passed and result.diagnostic_candidate is not None
    assert "service_to_result_audit_seconds" not in result.run_manifest.stage_duration_seconds


@pytest.mark.parametrize("kind", ("allowed_underweight", "prohibited"))
def test_real_business_status_is_preserved_without_inventing_publishability(clock, kind):
    if kind == "allowed_underweight":
        rule = RuleDefinitionSpec(
            "weight",
            "ChainWeightRangeRule",
            "链重",
            RuleScope.CHAIN,
            True,
            "1",
            {"min_weight": D("100"), "max_weight": D("200"), "target_weight": D("150")},
        )
        specification = make_spec(
            rules=(rule,), allowed_final_deviation_codes={"chain_weight_below_minimum"}
        )
    else:
        rule = RuleDefinitionSpec(
            "narrow",
            "ContinuousNarrowSteelWeightRule",
            "窄材",
            RuleScope.CHAIN,
            True,
            "1",
            {"grade_class": "IF钢", "width_upper_exclusive": D("1400"), "max_real_weight": D("5")},
        )
        specification = make_spec(rules=(rule,))
    result = service.solve_request(make_request(rule_set_spec=specification))
    if kind == "allowed_underweight":
        assert result.status is SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION
        assert result.release is not None and result.confirmation_required
    else:
        assert result.status is SolveStatus.COMPLETE_NOT_PUBLISHABLE
        assert result.release is None and result.diagnostic_candidate is not None
        assert result.audit_report.status is ResultAuditStatus.NOT_RUN


def test_corrupted_draft_is_rejected_by_actual_contract_audit(clock, monkeypatch):
    original = service.assemble_draft_scheduling_result

    def corrupted(*args):
        draft = original(*args)
        return replace(draft, request_id="another-request")

    monkeypatch.setattr(service, "assemble_draft_scheduling_result", corrupted)
    result = service.solve_request(make_request())
    assert (
        result.status is SolveStatus.FAILED and result.stop_reason is SearchStopReason.SYSTEM_ERROR
    )
    assert result.release is None and result.core_audit.passed
    assert result.audit_report.status is ResultAuditStatus.COMPLETED
    assert not result.audit_report.passed
    assert any(
        item.code == "request_identity_mismatch" and item.phase is DiagnosticPhase.RESULT_AUDIT
        for item in result.issues
    )
