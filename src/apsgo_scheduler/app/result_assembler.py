"""Map trusted core materials to an internal draft, then seal a checked public result."""

from dataclasses import dataclass, field, fields, replace

from ..api.request import SchedulingRequest, fingerprint_public_request
from ..api.result import (
    ResultAuditReport,
    ResultAuditStatus,
    RunManifest,
    SchedulingRelease,
    SchedulingResult,
)
from ..core.budget import SolveRuntimeBudget
from ..core.contracts import (
    CoreAuditReport,
    CoreCandidateSnapshot,
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    SearchStopReason,
    SolveMetrics,
    SolverResult,
    SolveStatus,
    fingerprint,
    freeze_tuple,
    require_enum,
    require_text,
)
from ..core.evaluation import PlanEvaluation
from ..core.model import SchedulePlan
from ..core.resource_facts import PlanDerivedFacts


def _values(value, excluded=()):
    return {
        item.name: getattr(value, item.name) for item in fields(value) if item.name not in excluded
    }


@dataclass(frozen=True, slots=True)
class DraftSchedulingRelease:
    plan: SchedulePlan
    evaluation: PlanEvaluation
    resource_facts: PlanDerivedFacts
    core_release_fingerprint: str

    def __post_init__(self):
        for name, expected in (
            ("plan", SchedulePlan),
            ("evaluation", PlanEvaluation),
            ("resource_facts", PlanDerivedFacts),
        ):
            if not isinstance(getattr(self, name), expected):
                raise ValueError(f"draft release {name} must be {expected.__name__}")
        require_text(self.core_release_fingerprint, "core_release_fingerprint")


@dataclass(frozen=True, slots=True)
class DraftSchedulingResult:
    contract_version: str
    request_id: str
    proposed_status: SolveStatus
    stop_reason: SearchStopReason
    proposed_release: DraftSchedulingRelease
    diagnostic_candidate: CoreCandidateSnapshot
    core_audit: CoreAuditReport
    metrics: SolveMetrics
    issues: tuple[DiagnosticIssue, ...]
    run_manifest: RunManifest
    draft_fingerprint: str = field(init=False)

    def __post_init__(self):
        require_text(self.contract_version, "contract_version")
        require_text(self.request_id, "request_id")
        require_enum(self.proposed_status, SolveStatus, "proposed_status")
        require_enum(self.stop_reason, SearchStopReason, "stop_reason")
        for name, expected in (
            ("proposed_release", DraftSchedulingRelease),
            ("diagnostic_candidate", CoreCandidateSnapshot),
            ("core_audit", CoreAuditReport),
            ("metrics", SolveMetrics),
            ("run_manifest", RunManifest),
        ):
            if not isinstance(getattr(self, name), expected):
                raise ValueError(f"draft {name} must be {expected.__name__}")
        object.__setattr__(self, "issues", freeze_tuple(self.issues, DiagnosticIssue, "issues"))
        object.__setattr__(self, "draft_fingerprint", fingerprint_draft_result(self))


def fingerprint_draft_result(draft: DraftSchedulingResult) -> str:
    if not isinstance(draft, DraftSchedulingResult):
        raise ValueError("draft must be DraftSchedulingResult")
    values = _values(draft, ("draft_fingerprint",))
    values["metrics"] = _values(draft.metrics, ("stage_duration_seconds",))
    values["run_manifest"] = draft.run_manifest.deterministic_run_fingerprint
    return fingerprint(values)


def assemble_draft_scheduling_result(
    request: SchedulingRequest, core_result: SolverResult, manifest: RunManifest
) -> DraftSchedulingResult:
    """Only copy references and verify upstream identities; do not rerun any evaluation."""
    if not isinstance(request, SchedulingRequest) or not isinstance(core_result, SolverResult):
        raise ValueError("assembly requires SchedulingRequest and SolverResult")
    if not isinstance(manifest, RunManifest):
        raise ValueError("assembly requires RunManifest")
    release = core_result.release
    if release is None or core_result.status not in {
        SolveStatus.SUCCESS,
        SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION,
    }:
        raise ValueError("only a publishable audited core result can form a release draft")
    if (
        manifest.request_fingerprint != fingerprint_public_request(request)
        or manifest.problem_fingerprint != core_result.problem_fingerprint
        or manifest.rule_set_fingerprint != core_result.rule_set_fingerprint
        or manifest.rule_set_fingerprint != request.rule_set_spec.fingerprint
        or manifest.policy_fingerprint != core_result.policy_fingerprint
        or manifest.policy_fingerprint != fingerprint(request.policy)
        or manifest.stop_reason is not core_result.stop_reason
        or manifest.counters != _values(core_result.metrics, ("stage_duration_seconds",))
        or manifest.trace_fingerprint != fingerprint(core_result.trace)
        or manifest.diagnostic_codes != tuple(issue.code for issue in core_result.issues)
    ):
        raise ValueError("manifest does not describe this request and exact core result")
    return DraftSchedulingResult(
        request.contract_version,
        request.request_id,
        core_result.status,
        core_result.stop_reason,
        DraftSchedulingRelease(
            release.canonical_plan,
            release.audited_evaluation,
            release.resource_facts,
            release.release_fingerprint,
        ),
        core_result.diagnostic_candidate,
        core_result.core_audit,
        core_result.metrics,
        core_result.issues,
        manifest,
    )


def seal_scheduling_result(
    draft: DraftSchedulingResult, audit_report: ResultAuditReport, runtime: SolveRuntimeBudget
) -> SchedulingResult:
    """Sign only the checked draft; cancellation or hard timeout also wins after hashing."""
    if (
        not isinstance(draft, DraftSchedulingResult)
        or not isinstance(audit_report, ResultAuditReport)
        or not isinstance(runtime, SolveRuntimeBudget)
    ):
        raise ValueError("sealing requires a typed draft, result audit and shared runtime")
    proposed = draft.proposed_release

    def interrupted():
        stop = runtime.stop_reason
        if stop not in {
            SearchStopReason.USER_CANCELLED,
            SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
        }:
            raise ValueError("finalization stopped without cancellation or hard deadline")
        cancelled = stop is SearchStopReason.USER_CANCELLED
        issue = DiagnosticIssue(
            "result_sealing_cancelled" if cancelled else "result_sealing_time_limit",
            DiagnosticPhase.RESULT_ASSEMBLY,
            "release",
            None,
            "结果签发时已取消，不发布结果。" if cancelled else "结果签发已到最终截止，不发布结果。",
            DiagnosticSeverity.ERROR,
        )
        issues = tuple(
            sorted(
                (*draft.issues, issue), key=lambda item: tuple(DiagnosticPhase).index(item.phase)
            )
        )
        manifest = replace(
            draft.run_manifest,
            stop_reason=stop,
            search_was_truncated=stop.search_was_truncated,
            diagnostic_codes=tuple(item.code for item in issues),
        )
        return SchedulingResult(
            draft.contract_version,
            draft.request_id,
            SolveStatus.CANCELLED if cancelled else SolveStatus.COMPLETE_NOT_PUBLISHABLE,
            stop,
            None,
            draft.diagnostic_candidate,
            draft.core_audit,
            audit_report,
            issues,
            manifest,
        )

    if not runtime.allows_finalization():
        return interrupted()
    if (
        runtime.stop_reason is not draft.stop_reason
        or draft.draft_fingerprint != fingerprint_draft_result(draft)
        or audit_report.status is not ResultAuditStatus.COMPLETED
        or not audit_report.passed
        or audit_report.draft_fingerprint != draft.draft_fingerprint
        or audit_report.plan_fingerprint != fingerprint(proposed.plan)
        or audit_report.resource_fingerprint != proposed.resource_facts.facts_fingerprint
        or audit_report.report_fingerprint
        != fingerprint(_values(audit_report, ("report_fingerprint",)))
    ):
        raise ValueError("completed result audit is not bound to this draft and its materials")
    if not runtime.allows_finalization():
        return interrupted()
    release_values = _values(proposed)
    release = SchedulingRelease(
        **release_values,
        release_fingerprint=fingerprint(
            {**release_values, "result_audit_fingerprint": audit_report.report_fingerprint}
        ),
    )
    result = SchedulingResult(
        draft.contract_version,
        draft.request_id,
        draft.proposed_status,
        draft.stop_reason,
        release,
        draft.diagnostic_candidate,
        draft.core_audit,
        audit_report,
        draft.issues,
        draft.run_manifest,
    )
    return result if runtime.allows_finalization() else interrupted()
