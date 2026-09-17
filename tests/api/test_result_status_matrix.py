"""Public result states expose release eligibility without executing an audit."""

from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal

import pytest

from apsgo_scheduler.api.result import (
    ResultAuditReport,
    ResultAuditStatus,
    RunManifest,
    SchedulingRelease,
    SchedulingResult,
)
from apsgo_scheduler.core.contracts import (
    CoreAuditReport,
    CoreAuditStatus,
    CoreCandidateSnapshot,
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
    SearchStopReason,
    SolveStatus,
    canonical_json,
    fingerprint,
)
from apsgo_scheduler.core.evaluation import PlanEvaluation
from apsgo_scheduler.core.model import Chain, MaterialRole, Node, SchedulePlan
from apsgo_scheduler.core.resource_facts import NodeAssignmentFact, PlanDerivedFacts
from apsgo_scheduler.core.rules.base import RuleDisposition, RuleViolation


def plan():
    node = Node(
        "node",
        "order",
        "resource",
        "period",
        Decimal("100"),
        None,
        None,
        None,
        None,
        "",
        MaterialRole.NORMAL_REAL,
        {},
    )
    return SchedulePlan((Chain("chain", (node,), "period"),))


def release():
    assignment = NodeAssignmentFact(
        "node",
        "order",
        "resource",
        MaterialRole.NORMAL_REAL,
        "chain",
        "period",
        0,
        Decimal("100"),
    )
    facts = PlanDerivedFacts(
        (assignment,),
        (),
        (),
        (),
        (),
        Decimal("100"),
        Decimal("100"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        "resources",
    )
    return SchedulingRelease(
        plan(), PlanEvaluation((), (), {}, ()), facts, "core-release", "public-release"
    )


def core_audit(*, completed=True, **changes):
    values = {
        "status": CoreAuditStatus.COMPLETED if completed else CoreAuditStatus.NOT_RUN,
        "passed": completed,
        "integrity_passed": completed,
        "writeback_blocking_violation_count": 0 if completed else None,
        "audited_evaluation_fingerprint": fingerprint(release().evaluation) if completed else None,
        "invariant_failure_codes": (),
        "action_authorization_failure_codes": (),
        "derived_resource_fingerprint": "resources" if completed else None,
        "audited_split_count": 0 if completed else None,
        "audited_same_period_split_count": 0 if completed else None,
        "audited_future_borrow_return_count": 0 if completed else None,
        "search_evaluation_matches": True if completed else None,
        "report_fingerprint": "core-audit",
    }
    return CoreAuditReport(**(values | changes))


def result_audit(*, completed=True, **changes):
    values = {
        "status": ResultAuditStatus.COMPLETED if completed else ResultAuditStatus.NOT_RUN,
        "passed": completed,
        "failure_codes": (),
        "plan_fingerprint": fingerprint(plan()) if completed else None,
        "resource_fingerprint": "resources" if completed else None,
        "draft_fingerprint": "draft" if completed else None,
        "report_fingerprint": "result-audit",
    }
    return ResultAuditReport(**(values | changes))


def manifest(**changes):
    stop = changes.get("stop_reason", SearchStopReason.LOCAL_SEARCH_COMPLETE)
    values = {
        "algorithm_version": "path-cover-local-search-v1",
        "code_revision": "revision",
        "request_fingerprint": "request",
        "problem_fingerprint": "problem",
        "rule_set_fingerprint": "rules",
        "policy_fingerprint": "policy",
        "stop_reason": stop,
        "search_was_truncated": stop.search_was_truncated,
        "optimality_proven": False,
        "counters": {"candidate_check_count": 0},
        "diagnostic_codes": (),
        "stage_duration_seconds": {"search": Decimal("0.2")},
        "trace_fingerprint": "trace",
    }
    return RunManifest(**(values | changes))


def result(**changes):
    values = {
        "contract_version": "1",
        "request_id": "request-id",
        "status": SolveStatus.SUCCESS,
        "stop_reason": SearchStopReason.LOCAL_SEARCH_COMPLETE,
        "release": release(),
        "diagnostic_candidate": None,
        "core_audit": core_audit(),
        "audit_report": result_audit(),
        "issues": (),
        "run_manifest": manifest(),
    }
    return SchedulingResult(**(values | changes))


def issue(code="invalid_request", phase=DiagnosticPhase.REQUEST_VALIDATION):
    return DiagnosticIssue(
        code, phase, "orders", None, "请求字段不符合契约", DiagnosticSeverity.ERROR
    )


def preflight_failure(**changes):
    values = {
        "status": SolveStatus.FAILED,
        "stop_reason": SearchStopReason.INPUT_INVALID,
        "release": None,
        "core_audit": core_audit(completed=False),
        "audit_report": result_audit(completed=False),
        "issues": (issue(),),
        "run_manifest": manifest(
            stop_reason=SearchStopReason.INPUT_INVALID,
            problem_fingerprint=None,
            rule_set_fingerprint=None,
            policy_fingerprint=None,
            diagnostic_codes=("invalid_request",),
        ),
    }
    return result(**(values | changes))


@pytest.mark.parametrize(
    "status,confirmation",
    [(SolveStatus.SUCCESS, False), (SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION, True)],
)
def test_publishable_statuses_require_release_and_derive_confirmation(status, confirmation):
    material = release()
    if confirmation:
        material = replace(material, evaluation=allowed_evaluation())
    actual = result(
        status=status,
        release=material,
        core_audit=core_audit(audited_evaluation_fingerprint=fingerprint(material.evaluation)),
    )
    assert actual.release is not None
    assert actual.confirmation_required is confirmation
    assert "confirmation_required" not in {item.name for item in fields(SchedulingResult)}
    with pytest.raises(ValueError):
        replace(actual, release=None)
    with pytest.raises(FrozenInstanceError):
        actual.status = SolveStatus.FAILED


@pytest.mark.parametrize(
    "status,stop,candidate",
    [
        (SolveStatus.COMPLETE_NOT_PUBLISHABLE, SearchStopReason.LOCAL_SEARCH_COMPLETE, True),
        (SolveStatus.NO_COMPLETE_PLAN, SearchStopReason.SEARCH_TIME_LIMIT_REACHED, False),
        (SolveStatus.CANCELLED, SearchStopReason.USER_CANCELLED, True),
        (SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR, True),
        (SolveStatus.FAILED, SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED, True),
        (SolveStatus.NO_COMPLETE_PLAN, SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED, False),
    ],
)
def test_unpublishable_status_matrix(status, stop, candidate):
    actual = result(
        status=status,
        stop_reason=stop,
        release=None,
        diagnostic_candidate=CoreCandidateSnapshot(plan(), PlanEvaluation((), (), {}, ()))
        if candidate
        else None,
        core_audit=core_audit(completed=False),
        audit_report=result_audit(completed=False),
        run_manifest=manifest(stop_reason=stop),
    )
    assert actual.release is None
    assert not actual.confirmation_required
    with pytest.raises(ValueError):
        replace(actual, release=release())


def test_input_failure_has_request_identity_without_unfinished_stage_identities():
    actual = preflight_failure()
    assert actual.status is SolveStatus.FAILED
    assert actual.run_manifest.request_fingerprint == "request"
    assert actual.core_audit.status is CoreAuditStatus.NOT_RUN
    assert actual.audit_report.status is ResultAuditStatus.NOT_RUN
    assert actual.run_manifest.problem_fingerprint is None
    assert actual.run_manifest.rule_set_fingerprint is None
    assert actual.run_manifest.policy_fingerprint is None
    assert actual.release is None
    for change in (
        {"issues": ()},
        {"core_audit": core_audit()},
        {"audit_report": result_audit()},
        {"status": SolveStatus.NO_COMPLETE_PLAN},
        {"diagnostic_candidate": CoreCandidateSnapshot(plan(), PlanEvaluation((), (), {}, ()))},
    ):
        with pytest.raises(ValueError):
            replace(actual, **change)
    warning = replace(issue(), severity=DiagnosticSeverity.WARNING)
    with pytest.raises(ValueError):
        replace(actual, issues=(warning,))
    with pytest.raises(ValueError):
        replace(actual, issues=(replace(issue(), field_path=None),))


def test_preflight_failure_retains_completed_stage_fingerprints():
    original = preflight_failure()
    actual = replace(
        original,
        run_manifest=replace(original.run_manifest, policy_fingerprint="completed-policy"),
    )
    assert actual.run_manifest.policy_fingerprint == "completed-policy"
    assert actual.run_manifest.problem_fingerprint is None
    assert actual.run_manifest.rule_set_fingerprint is None


@pytest.mark.parametrize("name", ["request_id", "contract_version"])
def test_preflight_failure_preserves_invalid_text_identity_for_field_diagnostics(name):
    actual = preflight_failure(**{name: ""})
    assert getattr(actual, name) == ""
    assert actual.issues[0].field_path is not None
    assert actual.run_manifest.request_fingerprint
    with pytest.raises(ValueError):
        result(**{name: ""})
    with pytest.raises(ValueError):
        preflight_failure(**{name: None})


def test_system_error_always_requires_failed_status():
    for status in (SolveStatus.NO_COMPLETE_PLAN, SolveStatus.COMPLETE_NOT_PUBLISHABLE):
        with pytest.raises(ValueError):
            result(
                status=status,
                stop_reason=SearchStopReason.SYSTEM_ERROR,
                release=None,
                run_manifest=manifest(stop_reason=SearchStopReason.SYSTEM_ERROR),
            )


def test_assembly_failure_can_follow_a_completed_successful_core_audit():
    diagnostic = issue("assembly_failed", DiagnosticPhase.RESULT_ASSEMBLY)
    actual = result(
        status=SolveStatus.FAILED,
        stop_reason=SearchStopReason.SYSTEM_ERROR,
        release=None,
        issues=(diagnostic,),
        audit_report=result_audit(completed=False),
        run_manifest=manifest(
            stop_reason=SearchStopReason.SYSTEM_ERROR,
            diagnostic_codes=("assembly_failed",),
        ),
    )
    assert actual.core_audit.passed
    assert actual.audit_report.status is ResultAuditStatus.NOT_RUN
    assert actual.release is None


def test_complete_unpublishable_result_may_omit_optional_diagnostic_snapshot():
    actual = result(
        status=SolveStatus.COMPLETE_NOT_PUBLISHABLE,
        release=None,
        core_audit=core_audit(completed=False),
        audit_report=result_audit(completed=False),
    )
    assert actual.diagnostic_candidate is None
    assert actual.release is None


def test_input_failure_does_not_pollute_unrun_result_audit_with_input_errors():
    with pytest.raises(ValueError):
        result_audit(completed=False, failure_codes=("invalid_request",))
    for key in ("plan_fingerprint", "resource_fingerprint", "draft_fingerprint"):
        with pytest.raises(ValueError):
            result_audit(completed=False, **{key: "invented"})


@pytest.mark.parametrize(
    "stop",
    [SearchStopReason.CANDIDATE_LIMIT_REACHED, SearchStopReason.SEARCH_TIME_LIMIT_REACHED],
)
def test_search_budget_exhaustion_can_publish_after_both_audits(stop):
    actual = result(stop_reason=stop, run_manifest=manifest(stop_reason=stop))
    assert actual.release is not None
    assert actual.run_manifest.search_was_truncated
    assert actual.stop_reason is stop


@pytest.mark.parametrize(
    "stop",
    [
        SearchStopReason.USER_CANCELLED,
        SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
        SearchStopReason.INPUT_INVALID,
        SearchStopReason.SYSTEM_ERROR,
    ],
)
def test_terminal_reasons_never_release_even_with_passing_audits(stop):
    with pytest.raises(ValueError):
        result(stop_reason=stop, run_manifest=manifest(stop_reason=stop))


def test_publishing_requires_both_passing_audits_and_consistent_resource_identity():
    for changes in (
        {"core_audit": core_audit(completed=False)},
        {"audit_report": result_audit(completed=False)},
        {"core_audit": core_audit(passed=False, integrity_passed=False,
                                  writeback_blocking_violation_count=None,
                                  invariant_failure_codes=("coverage",))},
        {"audit_report": result_audit(passed=False, failure_codes=("resource_mismatch",))},
        {"core_audit": core_audit(derived_resource_fingerprint="different")},
        {"audit_report": result_audit(resource_fingerprint="different")},
    ):
        with pytest.raises(ValueError):
            result(**changes)
    with pytest.raises(ValueError):
        result(issues=(issue(),), run_manifest=manifest(diagnostic_codes=("invalid_request",)))
    for name in ("problem_fingerprint", "rule_set_fingerprint", "policy_fingerprint"):
        with pytest.raises(ValueError):
            result(run_manifest=manifest(**{name: None}))


@pytest.mark.parametrize("status", list(ResultAuditStatus))
def test_audit_status_distinguishes_not_run_from_failure_and_completion(status):
    if status is ResultAuditStatus.COMPLETED:
        assert result_audit().passed
        failed = result_audit(passed=False, failure_codes=("mismatch",))
        assert not failed.passed
        with pytest.raises(ValueError):
            result_audit(passed=False)
    else:
        actual = result_audit(completed=False, status=status)
        assert actual.status is status
        assert not actual.passed
        with pytest.raises(ValueError):
            replace(actual, passed=True)
        with pytest.raises(ValueError):
            replace(actual, resource_fingerprint="not-audited")


def test_result_audit_rejects_claimed_pass_with_missing_identities_or_failure_codes():
    for name in ("plan_fingerprint", "resource_fingerprint", "draft_fingerprint"):
        with pytest.raises(ValueError):
            result_audit(**{name: None})
    for changes in ({"failure_codes": ("failed",)}, {"passed": 1}, {"status": "completed"}):
        with pytest.raises(ValueError):
            result_audit(**changes)


def test_manifest_copies_and_freezes_maps_and_sequences():
    counters = {"candidate_check_count": 0}
    durations = {"search": Decimal("1.0")}
    codes = ["warning"]
    actual = manifest(counters=counters, stage_duration_seconds=durations, diagnostic_codes=codes)
    counters["candidate_check_count"] = 77
    durations["search"] = Decimal("9")
    codes.append("later")
    assert actual.counters["candidate_check_count"] == 0
    assert actual.stage_duration_seconds["search"] == Decimal("1")
    assert actual.diagnostic_codes == ("warning",)
    with pytest.raises(TypeError):
        actual.counters["candidate_check_count"] = 1
    with pytest.raises(FrozenInstanceError):
        actual.trace_fingerprint = "other"


def test_actual_durations_do_not_change_deterministic_manifest_or_result_identities():
    first = manifest()
    second = replace(first, stage_duration_seconds={"search": Decimal("999")})
    assert first.deterministic_run_fingerprint == second.deterministic_run_fingerprint
    assert canonical_json(first) != canonical_json(second)
    assert (
        result(run_manifest=first).result_fingerprint
        == result(run_manifest=second).result_fingerprint
    )
    assert (
        replace(first, counters={"candidate_check_count": 1}).deterministic_run_fingerprint
        != first.deterministic_run_fingerprint
    )
    assert (
        replace(first, trace_fingerprint="changed").deterministic_run_fingerprint
        != first.deterministic_run_fingerprint
    )


def test_manifest_mapping_order_does_not_change_identity():
    first = manifest(counters={"first": 1, "second": 2})
    second = manifest(counters={"second": 2, "first": 1})
    assert canonical_json(first) == canonical_json(second)
    assert first.deterministic_run_fingerprint == second.deterministic_run_fingerprint


def test_diagnostics_sort_by_phase_and_preserve_discovery_order_within_phase():
    issues = [issue("later", DiagnosticPhase.RULE_LOADING), issue("first"), issue("second")]
    actual = preflight_failure(
        issues=issues,
        run_manifest=manifest(
            stop_reason=SearchStopReason.INPUT_INVALID,
            diagnostic_codes=("first", "second", "later"),
        ),
    )
    issues.clear()
    assert tuple(item.code for item in actual.issues) == ("first", "second", "later")
    with pytest.raises(ValueError):
        replace(actual, run_manifest=replace(actual.run_manifest, diagnostic_codes=("first",)))


def test_stop_reasons_and_complete_candidate_shapes_cannot_contradict_status():
    with pytest.raises(ValueError):
        result(run_manifest=manifest(stop_reason=SearchStopReason.CANDIDATE_LIMIT_REACHED))
    with pytest.raises(ValueError):
        result(
            status=SolveStatus.NO_COMPLETE_PLAN,
            release=None,
            diagnostic_candidate=CoreCandidateSnapshot(plan(), PlanEvaluation((), (), {}, ())),
        )
    with pytest.raises(ValueError):
        result(status=SolveStatus.CANCELLED, release=None)
    with pytest.raises(ValueError):
        preflight_failure(stop_reason=None)


@pytest.mark.parametrize(
    "changes",
    [
        {"optimality_proven": True},
        {"optimality_proven": 0},
        {"search_was_truncated": True},
        {"search_was_truncated": 0},
        {"counters": {"checks": -1}},
        {"counters": {"checks": True}},
        {"counters": {"checks": {"nested": 1}}},
        {"stage_duration_seconds": {"search": Decimal("NaN")}},
        {"stage_duration_seconds": {"search": Decimal("-1")}},
        {"stage_duration_seconds": {"search": 1}},
        {"problem_fingerprint": ""},
        {"diagnostic_codes": [""]},
    ],
)
def test_manifest_rejects_inconsistent_or_nonimmutable_values(changes):
    with pytest.raises(ValueError):
        manifest(**changes)


def test_typed_result_fields_and_release_payload_are_not_interchangeable():
    for name in ("core_audit", "audit_report", "run_manifest", "diagnostic_candidate", "release"):
        with pytest.raises(ValueError):
            result(**{name: object()})
    original = release()
    for changes in (
        {"plan": object()},
        {"resource_facts": object()},
        {"evaluation": None},
        {"evaluation": object()},
        {"evaluation": {"quality_key": ()}},
        {"release_fingerprint": ""},
        {"core_release_fingerprint": ""},
    ):
        with pytest.raises(ValueError):
            replace(original, **changes)


def test_public_fields_preserve_design_and_exclude_application_drafts():
    import apsgo_scheduler.api.result as module

    assert {item.name for item in fields(SchedulingResult)} == {
        "contract_version",
        "request_id",
        "status",
        "stop_reason",
        "release",
        "diagnostic_candidate",
        "core_audit",
        "audit_report",
        "issues",
        "run_manifest",
        "result_fingerprint",
    }
    assert {item.name for item in fields(RunManifest)} == {
        "algorithm_version",
        "code_revision",
        "request_fingerprint",
        "problem_fingerprint",
        "rule_set_fingerprint",
        "policy_fingerprint",
        "stop_reason",
        "search_was_truncated",
        "optimality_proven",
        "counters",
        "diagnostic_codes",
        "stage_duration_seconds",
        "trace_fingerprint",
        "deterministic_run_fingerprint",
    }
    assert {item.name for item in fields(SchedulingRelease)} == {
        "plan",
        "evaluation",
        "resource_facts",
        "core_release_fingerprint",
        "release_fingerprint",
    }
    assert {item.name for item in fields(ResultAuditReport)} == {
        "status",
        "passed",
        "failure_codes",
        "plan_fingerprint",
        "resource_fingerprint",
        "draft_fingerprint",
        "report_fingerprint",
    }
    assert not hasattr(module, "DraftSchedulingRelease")
    assert not hasattr(module, "DraftSchedulingResult")


def allowed_evaluation():
    violation = RuleViolation(
        "weight",
        RuleScope.CHAIN,
        "chain",
        "chain_weight_below_minimum",
        "链重不足，属于允许偏差。",
        RuleDisposition.ALLOWED_FINAL_DEVIATION,
        Decimal(50),
    )
    return PlanEvaluation((), (violation,), {"underweight_chain_count": 1}, (0, Decimal(0), 1))


@pytest.mark.parametrize("name", ("request_id", "contract_version"))
@pytest.mark.parametrize(
    "status,stop",
    (
        (SolveStatus.CANCELLED, SearchStopReason.USER_CANCELLED),
        (SolveStatus.NO_COMPLETE_PLAN, SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED),
        (SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR),
    ),
)
def test_early_nonrelease_outcome_preserves_invalid_raw_text_even_when_late_cancelled(
    name, status, stop
):
    original = preflight_failure(**{name: " \t"})
    actual = replace(
        original,
        status=status,
        stop_reason=stop,
        run_manifest=replace(
            original.run_manifest, stop_reason=stop, search_was_truncated=stop.search_was_truncated
        ),
    )
    assert getattr(actual, name) == " \t" and actual.release is None
    for change in (
        {"core_audit": core_audit()},
        {"audit_report": result_audit()},
        {"diagnostic_candidate": CoreCandidateSnapshot(plan(), release().evaluation)},
    ):
        with pytest.raises(ValueError):
            replace(actual, **change)


@pytest.mark.parametrize("part", ("plan", "evaluation", "diagnostic_plan"))
def test_public_release_rejects_replaced_material_that_no_longer_matches_audit(part):
    actual = result()
    changed_plan = replace(plan(), chains=(replace(plan().chains[0], chain_id="different"),))
    if part == "plan":
        changes = {"release": replace(actual.release, plan=changed_plan)}
    elif part == "evaluation":
        changes = {
            "release": replace(
                actual.release, evaluation=replace(actual.release.evaluation, quality_key=(9,))
            )
        }
    else:
        changes = {
            "diagnostic_candidate": CoreCandidateSnapshot(changed_plan, actual.release.evaluation)
        }
    with pytest.raises(ValueError, match="audited identities"):
        replace(actual, **changes)


@pytest.mark.parametrize(
    "status", (SolveStatus.SUCCESS, SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION)
)
def test_release_status_cannot_mislabel_whether_allowed_deviation_exists(status):
    evaluation = allowed_evaluation() if status is SolveStatus.SUCCESS else release().evaluation
    with pytest.raises(ValueError, match="status"):
        result(
            status=status,
            release=replace(release(), evaluation=evaluation),
            core_audit=core_audit(audited_evaluation_fingerprint=fingerprint(evaluation)),
        )


@pytest.mark.parametrize(
    "change",
    (
        {"reason_code": "other"},
        {"disposition": RuleDisposition.PROHIBITED},
        {"scope": RuleScope.EDGE},
    ),
)
def test_reported_pass_cannot_publish_other_or_prohibited_deviations(change):
    original = allowed_evaluation()
    evaluation = replace(original, violations=(replace(original.violations[0], **change),))
    with pytest.raises(ValueError, match="status"):
        result(
            status=SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION,
            release=replace(release(), evaluation=evaluation),
            core_audit=core_audit(audited_evaluation_fingerprint=fingerprint(evaluation)),
        )


def test_result_identity_binds_the_optional_diagnostic_snapshot():
    original = result(status=SolveStatus.COMPLETE_NOT_PUBLISHABLE, release=None)
    first = replace(
        original, diagnostic_candidate=CoreCandidateSnapshot(plan(), release().evaluation)
    )
    changed_evaluation = replace(release().evaluation, quality_key=(99,))
    second = replace(first, diagnostic_candidate=CoreCandidateSnapshot(plan(), changed_evaluation))
    assert (
        len({original.result_fingerprint, first.result_fingerprint, second.result_fingerprint}) == 3
    )
    assert first.core_audit is second.core_audit and first.run_manifest is second.run_manifest
