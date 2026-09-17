"""Public immutable result carriers; construction never executes an audit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from ..core.contracts import (
    CoreAuditReport,
    CoreAuditStatus,
    CoreCandidateSnapshot,
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    SearchStopReason,
    SolveStatus,
    audited_release_status,
    fingerprint,
    freeze_scalars,
    freeze_tuple,
    has_integrity_errors,
    require_decimal,
    require_enum,
    require_int,
    require_text,
)
from ..core.model import SchedulePlan
from ..core.resource_facts import PlanDerivedFacts

if TYPE_CHECKING:
    from ..core.evaluation import PlanEvaluation


@dataclass(frozen=True, slots=True)
class RunManifest:
    algorithm_version: str
    code_revision: str
    request_fingerprint: str
    problem_fingerprint: str | None
    rule_set_fingerprint: str | None
    policy_fingerprint: str | None
    stop_reason: SearchStopReason
    search_was_truncated: bool
    optimality_proven: bool
    counters: Mapping[str, int]
    diagnostic_codes: tuple[str, ...]
    stage_duration_seconds: Mapping[str, Decimal]
    trace_fingerprint: str
    deterministic_run_fingerprint: str = field(init=False)

    def __post_init__(self):
        for name in (
            "algorithm_version",
            "code_revision",
            "request_fingerprint",
            "trace_fingerprint",
        ):
            require_text(getattr(self, name), name)
        for name in ("problem_fingerprint", "rule_set_fingerprint", "policy_fingerprint"):
            require_text(getattr(self, name), name, allow_none=True)
        require_enum(self.stop_reason, SearchStopReason, "stop_reason")
        if (
            type(self.search_was_truncated) is not bool
            or self.search_was_truncated != self.stop_reason.search_was_truncated
        ):
            raise ValueError("search truncation must agree with the stop reason")
        if self.optimality_proven is not False:
            raise ValueError("this algorithm does not prove optimality")
        counters = freeze_scalars(self.counters)
        for name, value in counters.items():
            require_int(value, name)
        object.__setattr__(self, "counters", counters)
        durations = freeze_scalars(self.stage_duration_seconds)
        for name, value in durations.items():
            require_decimal(value, name, nonnegative=True)
        object.__setattr__(self, "stage_duration_seconds", durations)
        codes = freeze_tuple(self.diagnostic_codes, str, "diagnostic_codes")
        for code in codes:
            require_text(code, "diagnostic code")
        object.__setattr__(self, "diagnostic_codes", codes)
        payload = tuple(
            (item.name, getattr(self, item.name))
            for item in fields(self)
            if item.name not in {"stage_duration_seconds", "deterministic_run_fingerprint"}
        )
        object.__setattr__(self, "deterministic_run_fingerprint", fingerprint(payload))


class ResultAuditStatus(str, Enum):
    NOT_RUN = "not_run"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIME_LIMIT = "time_limit"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ResultAuditReport:
    status: ResultAuditStatus
    passed: bool
    failure_codes: tuple[str, ...]
    plan_fingerprint: str | None
    resource_fingerprint: str | None
    draft_fingerprint: str | None
    report_fingerprint: str

    def __post_init__(self):
        require_enum(self.status, ResultAuditStatus, "status")
        if type(self.passed) is not bool:
            raise ValueError("passed must be boolean")
        require_text(self.report_fingerprint, "report_fingerprint")
        for name in ("plan_fingerprint", "resource_fingerprint", "draft_fingerprint"):
            require_text(getattr(self, name), name, allow_none=True)
        codes = freeze_tuple(self.failure_codes, str, "failure_codes")
        for code in codes:
            require_text(code, "failure code")
        object.__setattr__(self, "failure_codes", codes)
        if self.status is not ResultAuditStatus.COMPLETED:
            if self.passed or self.plan_fingerprint is not None or self.resource_fingerprint:
                raise ValueError("unfinished result audit cannot claim success or audited facts")
            if self.status is ResultAuditStatus.NOT_RUN and (codes or self.draft_fingerprint):
                raise ValueError("an audit that did not run cannot contain audit findings")
        elif self.passed:
            if codes or any(
                value is None
                for value in (
                    self.plan_fingerprint,
                    self.resource_fingerprint,
                    self.draft_fingerprint,
                )
            ):
                raise ValueError("passed result audit needs all identities and no failures")
        elif not codes:
            raise ValueError("completed failed result audit must explain its failure")


@dataclass(frozen=True, slots=True)
class SchedulingRelease:
    plan: SchedulePlan
    evaluation: PlanEvaluation
    resource_facts: PlanDerivedFacts
    core_release_fingerprint: str
    release_fingerprint: str

    def __post_init__(self):
        from ..core.evaluation import PlanEvaluation

        if not isinstance(self.plan, SchedulePlan) or not isinstance(
            self.resource_facts, PlanDerivedFacts
        ):
            raise ValueError("release requires a typed plan and audited resource facts")
        if not isinstance(self.evaluation, PlanEvaluation):
            raise ValueError("release requires PlanEvaluation")
        require_text(self.core_release_fingerprint, "core_release_fingerprint")
        require_text(self.release_fingerprint, "release_fingerprint")


@dataclass(frozen=True, slots=True)
class SchedulingResult:
    contract_version: str
    request_id: str
    status: SolveStatus
    stop_reason: SearchStopReason
    release: SchedulingRelease | None
    diagnostic_candidate: CoreCandidateSnapshot | None
    core_audit: CoreAuditReport
    audit_report: ResultAuditReport
    issues: tuple[DiagnosticIssue, ...]
    run_manifest: RunManifest
    result_fingerprint: str = field(init=False)

    def __post_init__(self):
        require_enum(self.status, SolveStatus, "status")
        require_enum(self.stop_reason, SearchStopReason, "stop_reason")
        for name, expected_type in (
            ("core_audit", CoreAuditReport),
            ("audit_report", ResultAuditReport),
            ("run_manifest", RunManifest),
        ):
            if not isinstance(getattr(self, name), expected_type):
                raise ValueError(f"{name} has an invalid type")
        preflight = (
            self.release is None
            and self.diagnostic_candidate is None
            and self.core_audit.status is CoreAuditStatus.NOT_RUN
            and self.audit_report.status is ResultAuditStatus.NOT_RUN
        )
        for name in ("contract_version", "request_id"):
            value = getattr(self, name)
            if preflight:
                if not isinstance(value, str):
                    raise ValueError(f"{name} must preserve the request's text value")
            else:
                require_text(value, name)
        if self.diagnostic_candidate is not None and not isinstance(
            self.diagnostic_candidate, CoreCandidateSnapshot
        ):
            raise ValueError("diagnostic_candidate must be a core snapshot or None")
        issues = freeze_tuple(self.issues, DiagnosticIssue, "issues")
        phase_order = tuple(DiagnosticPhase)
        issues = tuple(sorted(issues, key=lambda issue: phase_order.index(issue.phase)))
        object.__setattr__(self, "issues", issues)
        if self.run_manifest.stop_reason is not self.stop_reason:
            raise ValueError("result and manifest stop reasons must agree")
        if self.run_manifest.diagnostic_codes != tuple(issue.code for issue in issues):
            raise ValueError("manifest diagnostic codes must match the ordered result issues")
        publishable = self.status.publishable
        if publishable != (self.release is not None):
            raise ValueError("only publishable statuses must have a release")
        if self.stop_reason is SearchStopReason.USER_CANCELLED:
            if self.status is not SolveStatus.CANCELLED:
                raise ValueError("user cancellation requires cancelled status")
        elif self.status is SolveStatus.CANCELLED:
            raise ValueError("cancelled status requires user cancellation")
        if (
            self.stop_reason is SearchStopReason.SYSTEM_ERROR
            and self.status is not SolveStatus.FAILED
        ):
            raise ValueError("system error requires failed status")
        if self.stop_reason is SearchStopReason.INPUT_INVALID:
            if (
                self.status is not SolveStatus.FAILED
                or self.core_audit.status is not CoreAuditStatus.NOT_RUN
                or self.audit_report.status is not ResultAuditStatus.NOT_RUN
                or self.diagnostic_candidate is not None
                or not any(
                    issue.severity is DiagnosticSeverity.ERROR and issue.field_path is not None
                    for issue in issues
                )
            ):
                raise ValueError("input failure needs diagnostics and two audits that did not run")
        if self.status is SolveStatus.NO_COMPLETE_PLAN and self.diagnostic_candidate is not None:
            raise ValueError("no-complete-plan status cannot claim a complete diagnostic candidate")
        if publishable:
            self._validate_release(issues)
        payload = (
            ("contract_version", self.contract_version),
            ("request_id", self.request_id),
            ("status", self.status),
            ("stop_reason", self.stop_reason),
            (
                "release_fingerprint",
                None if self.release is None else self.release.release_fingerprint,
            ),
            ("core_audit_fingerprint", self.core_audit.report_fingerprint),
            ("result_audit_fingerprint", self.audit_report.report_fingerprint),
            (
                "diagnostic_candidate_fingerprint",
                None
                if self.diagnostic_candidate is None
                else fingerprint(self.diagnostic_candidate),
            ),
            ("issues", issues),
            ("deterministic_run_fingerprint", self.run_manifest.deterministic_run_fingerprint),
        )
        object.__setattr__(self, "result_fingerprint", fingerprint(payload))

    def _validate_release(self, issues: tuple[DiagnosticIssue, ...]) -> None:
        if not isinstance(self.release, SchedulingRelease):
            raise ValueError("release must be SchedulingRelease")
        if self.stop_reason in {
            SearchStopReason.USER_CANCELLED,
            SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
            SearchStopReason.INPUT_INVALID,
            SearchStopReason.SYSTEM_ERROR,
        }:
            raise ValueError("this stop reason never permits release")
        if (
            self.core_audit.status is not CoreAuditStatus.COMPLETED
            or not self.core_audit.writeback_eligible
            or self.audit_report.status is not ResultAuditStatus.COMPLETED
            or not self.audit_report.passed
            or has_integrity_errors(issues)
        ):
            raise ValueError("release requires integrity audits, zero writeback blockers and no fatal issues")
        if any(
            value is None
            for value in (
                self.run_manifest.problem_fingerprint,
                self.run_manifest.rule_set_fingerprint,
                self.run_manifest.policy_fingerprint,
            )
        ):
            raise ValueError("release requires completed problem, rule-set and policy identities")
        if not (
            self.release.resource_facts.facts_fingerprint
            == self.core_audit.derived_resource_fingerprint
            == self.audit_report.resource_fingerprint
        ):
            raise ValueError("release and both audits must preserve the same resource identity")
        if (
            fingerprint(self.release.plan) != self.audit_report.plan_fingerprint
            or fingerprint(self.release.evaluation)
            != self.core_audit.audited_evaluation_fingerprint
            or (
                self.diagnostic_candidate is not None
                and self.diagnostic_candidate.plan != self.release.plan
            )
        ):
            raise ValueError("public plan and evaluation must match their audited identities")
        violations = self.release.evaluation.violations
        if self.status is not audited_release_status(
            violations, audit_passed=self.core_audit.passed
        ):
            raise ValueError("publishable status must reflect the audited violations")

    @property
    def confirmation_required(self) -> bool:
        return self.status is SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION
