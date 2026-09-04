"""Pure draft mapping and final signing retain the core's sole audited materials."""

from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apsgo_scheduler.api.request import RuleDefinitionSpec, fingerprint_public_request
from apsgo_scheduler.api.result import (
    ResultAuditReport,
    ResultAuditStatus,
    RunManifest,
    SchedulingRelease,
    SchedulingResult,
)
from apsgo_scheduler.app import result_assembler
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.result_assembler import (
    DraftSchedulingRelease,
    DraftSchedulingResult,
    assemble_draft_scheduling_result,
    fingerprint_draft_result,
    seal_scheduling_result,
)
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core import evaluation, final_audit, resource_facts, solver
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import (
    CoreCandidateSnapshot,
    RuleScope,
    SearchStopReason,
    SolveStatus,
    fingerprint,
)
from apsgo_scheduler.core.model import SearchState
from tests.app.test_input_normalizer import make_request, make_spec

D = Decimal


def make_manifest(request, core_result, **changes):
    values = dict(
        algorithm_version="path-cover-local-search-v1",
        code_revision="unversioned",
        request_fingerprint=fingerprint_public_request(request),
        problem_fingerprint=core_result.problem_fingerprint,
        rule_set_fingerprint=core_result.rule_set_fingerprint,
        policy_fingerprint=core_result.policy_fingerprint,
        stop_reason=core_result.stop_reason,
        search_was_truncated=core_result.stop_reason.search_was_truncated,
        optimality_proven=False,
        counters={
            item.name: getattr(core_result.metrics, item.name)
            for item in fields(core_result.metrics)
            if item.name != "stage_duration_seconds"
        },
        diagnostic_codes=tuple(issue.code for issue in core_result.issues),
        stage_duration_seconds=core_result.metrics.stage_duration_seconds,
        trace_fingerprint=fingerprint(core_result.trace),
    )
    return RunManifest(**(values | changes))


def draft_case(request=None, *, clock=lambda: 1.0, cancellation=None):
    request = make_request() if request is None else request
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    runtime = SolveRuntimeBudget.from_policy(request.policy, 0.0, cancellation, clock=clock)
    core_result = solver.solve(problem, rules, request.policy, runtime)
    assert core_result.release is not None
    manifest = make_manifest(request, core_result)
    draft = assemble_draft_scheduling_result(request, core_result, manifest)
    return request, problem, core_result, runtime, draft


def passed_report(draft, **changes):
    """Consistently signed report values isolate the sealer from the separate auditor tests."""
    values = (
        dict(
            status=ResultAuditStatus.COMPLETED,
            passed=True,
            failure_codes=(),
            plan_fingerprint=fingerprint(draft.proposed_release.plan),
            resource_fingerprint=draft.proposed_release.resource_facts.facts_fingerprint,
            draft_fingerprint=draft.draft_fingerprint,
        )
        | changes
    )
    return ResultAuditReport(**values, report_fingerprint=fingerprint(values))


def test_assembly_and_sealing_never_rederive_facts_or_replay_rules_and_preserve_references(
    monkeypatch,
):
    request, _, core, runtime, draft = draft_case()
    for module in (resource_facts, final_audit):
        monkeypatch.setattr(
            module, "_derive_audited_resource_facts", lambda *a: pytest.fail("fact derivation")
        )
    for module in (evaluation, final_audit):
        monkeypatch.setattr(module, "evaluate_plan", lambda *a: pytest.fail("rule replay"))
    copied = assemble_draft_scheduling_result(request, core, draft.run_manifest)
    assert copied.proposed_release.plan is core.release.canonical_plan
    assert copied.proposed_release.evaluation is core.release.audited_evaluation
    assert copied.proposed_release.resource_facts is core.release.resource_facts
    assert copied.proposed_release.core_release_fingerprint == core.release.release_fingerprint
    assert copied.diagnostic_candidate is core.diagnostic_candidate
    assert copied.core_audit is core.core_audit and copied.metrics is core.metrics
    assert copied.run_manifest is draft.run_manifest
    result = seal_scheduling_result(copied, passed_report(copied), runtime)
    assert result.status is SolveStatus.SUCCESS
    assert result.release.plan is core.release.canonical_plan
    assert result.release.evaluation is core.release.audited_evaluation
    assert result.release.resource_facts is core.release.resource_facts
    assert result.diagnostic_candidate is core.diagnostic_candidate
    assert result.release.core_release_fingerprint == core.release.release_fingerprint


def test_draft_and_release_types_are_internal_frozen_values():
    *_, draft = draft_case()
    assert isinstance(draft, DraftSchedulingResult)
    assert isinstance(draft.proposed_release, DraftSchedulingRelease)
    assert not hasattr(draft, "release") and not hasattr(
        draft.proposed_release, "release_fingerprint"
    )
    for value, name in ((draft, "request_id"), (draft.proposed_release, "plan")):
        with pytest.raises(FrozenInstanceError):
            setattr(value, name, None)
    assert not hasattr(draft, "__dict__")
    issues = list(draft.issues)
    other = replace(draft, issues=issues)
    issues.append(object())
    assert other.issues == draft.issues


@pytest.mark.parametrize(
    "field", ("plan", "evaluation", "resource_facts", "core_release_fingerprint")
)
def test_draft_release_rejects_invalid_types(field):
    *_, draft = draft_case()
    with pytest.raises(ValueError):
        replace(draft.proposed_release, **{field: object()})


@pytest.mark.parametrize(
    "field",
    ("proposed_release", "diagnostic_candidate", "core_audit", "metrics", "run_manifest", "issues"),
)
def test_draft_result_rejects_invalid_types(field):
    *_, draft = draft_case()
    with pytest.raises(ValueError):
        replace(draft, **{field: object()})


@pytest.mark.parametrize("index", (0, 1, 2))
def test_assembly_rejects_wrong_argument_types(index):
    request, _, core, _, draft = draft_case()
    args = [request, core, draft.run_manifest]
    args[index] = object()
    with pytest.raises(ValueError):
        assemble_draft_scheduling_result(*args)


def test_assembly_refuses_a_complete_core_result_without_release():
    request, _, core, _, draft = draft_case()
    failed = replace(core, status=SolveStatus.COMPLETE_NOT_PUBLISHABLE, release=None)
    with pytest.raises(ValueError, match="publishable"):
        assemble_draft_scheduling_result(request, failed, draft.run_manifest)


@pytest.mark.parametrize(
    "field",
    (
        "request_fingerprint",
        "problem_fingerprint",
        "rule_set_fingerprint",
        "policy_fingerprint",
        "trace_fingerprint",
        "counters",
        "diagnostic_codes",
        "stop_reason",
    ),
)
def test_assembly_rejects_manifest_not_bound_to_exact_request_and_core(field):
    request, _, core, _, draft = draft_case()
    value = "other"
    if field == "counters":
        value = dict(draft.run_manifest.counters) | {"candidate_check_count": 42}
    elif field == "diagnostic_codes":
        value = ("unrelated",)
    elif field == "stop_reason":
        value = SearchStopReason.CANDIDATE_LIMIT_REACHED
    changes = {field: value}
    if field == "stop_reason":
        changes["search_was_truncated"] = True
    with pytest.raises(ValueError, match="manifest"):
        assemble_draft_scheduling_result(request, core, replace(draft.run_manifest, **changes))


def test_draft_identity_excludes_nested_durations_but_binds_the_original_snapshot():
    *_, runtime, draft = draft_case()
    payload = {
        item.name: getattr(draft, item.name)
        for item in fields(draft)
        if item.name != "draft_fingerprint"
    }
    payload["metrics"] = {
        item.name: getattr(draft.metrics, item.name)
        for item in fields(draft.metrics)
        if item.name != "stage_duration_seconds"
    }
    payload["run_manifest"] = draft.run_manifest.deterministic_run_fingerprint
    assert draft.draft_fingerprint == fingerprint(payload) == fingerprint_draft_result(draft)
    changed = replace(
        draft,
        metrics=replace(draft.metrics, stage_duration_seconds={"changed": D(999)}),
        run_manifest=replace(draft.run_manifest, stage_duration_seconds={"other": D(888)}),
    )
    assert changed.draft_fingerprint == draft.draft_fingerprint
    report = passed_report(draft)
    first = seal_scheduling_result(draft, report, runtime)
    second = seal_scheduling_result(changed, report, runtime)
    assert first.release.release_fingerprint == second.release.release_fingerprint
    assert first.result_fingerprint == second.result_fingerprint
    snapshot = CoreCandidateSnapshot(
        draft.diagnostic_candidate.plan,
        replace(draft.diagnostic_candidate.search_evaluation, quality_key=(99, D(99))),
    )
    assert (
        replace(draft, diagnostic_candidate=snapshot).draft_fingerprint != draft.draft_fingerprint
    )


def test_release_identity_is_acyclic_and_covers_only_proposed_material_and_passed_report():
    *_, runtime, draft = draft_case()
    report = passed_report(draft)
    result = seal_scheduling_result(draft, report, runtime)
    values = {
        item.name: getattr(draft.proposed_release, item.name)
        for item in fields(draft.proposed_release)
    }
    expected = fingerprint(values | {"result_audit_fingerprint": report.report_fingerprint})
    assert result.release.release_fingerprint == expected
    assert result.audit_report is report
    assert "release_fingerprint" not in values and "result_fingerprint" not in values


@pytest.mark.parametrize(
    "field", ("draft_fingerprint", "plan_fingerprint", "resource_fingerprint", "report_fingerprint")
)
def test_sealing_rejects_a_passed_report_with_wrong_identity_binding(field):
    *_, runtime, draft = draft_case()
    report = (
        passed_report(draft, **{field: "changed"})
        if field != "report_fingerprint"
        else replace(passed_report(draft), report_fingerprint="changed")
    )
    with pytest.raises(ValueError, match="bound"):
        seal_scheduling_result(draft, report, runtime)


@pytest.mark.parametrize(
    "change",
    (
        {"passed": False, "failure_codes": ("invalid",)},
        {
            "status": ResultAuditStatus.NOT_RUN,
            "passed": False,
            "plan_fingerprint": None,
            "resource_fingerprint": None,
            "draft_fingerprint": None,
        },
    ),
)
def test_sealing_requires_a_completed_passing_second_audit(change):
    *_, runtime, draft = draft_case()
    with pytest.raises(ValueError, match="bound"):
        seal_scheduling_result(draft, passed_report(draft, **change), runtime)


def test_sealing_rejects_changed_or_unsigned_draft():
    *_, runtime, draft = draft_case()
    report = passed_report(draft)
    changed = replace(draft, request_id="another-request")
    with pytest.raises(ValueError, match="bound"):
        seal_scheduling_result(changed, report, runtime)
    object.__setattr__(draft, "draft_fingerprint", "unsigned")
    with pytest.raises(ValueError, match="bound"):
        seal_scheduling_result(draft, report, runtime)


@pytest.mark.parametrize("index", (0, 1, 2))
def test_sealing_rejects_wrong_argument_types(index):
    *_, runtime, draft = draft_case()
    args = [draft, passed_report(draft), runtime]
    args[index] = object()
    with pytest.raises(ValueError):
        seal_scheduling_result(*args)


@pytest.mark.parametrize("point", ("before", "draft_check", "release_hash", "release", "result"))
@pytest.mark.parametrize("termination", ("cancel", "time"))
def test_cancellation_or_hard_deadline_at_each_sealing_boundary_keeps_no_release(
    point, termination, monkeypatch
):
    signal = SimpleNamespace(cancelled=False, expired=False)
    *_, runtime, draft = draft_case(
        clock=lambda: 1000.0 if signal.expired else 1.0,
        cancellation=SimpleNamespace(is_cancelled=lambda: signal.cancelled),
    )
    report = passed_report(draft)

    def interrupt():
        setattr(signal, "cancelled" if termination == "cancel" else "expired", True)

    if point == "before":
        interrupt()
    elif point in {"release", "result"}:
        target = SchedulingRelease if point == "release" else SchedulingResult
        original = target.__init__

        def after_value(self, *args, **kwargs):
            original(self, *args, **kwargs)
            interrupt()

        monkeypatch.setattr(target, "__init__", after_value)
    else:
        name = "fingerprint_draft_result" if point == "draft_check" else "fingerprint"
        original = getattr(result_assembler, name)

        def after_hash(value):
            answer = original(value)
            if (
                point == "draft_check"
                or isinstance(value, dict)
                and "result_audit_fingerprint" in value
            ):
                interrupt()
            return answer

        monkeypatch.setattr(result_assembler, name, after_hash)
    result = seal_scheduling_result(draft, report, runtime)
    assert result.release is None and result.diagnostic_candidate is draft.diagnostic_candidate
    assert result.status is (
        SolveStatus.CANCELLED if termination == "cancel" else SolveStatus.COMPLETE_NOT_PUBLISHABLE
    )
    assert result.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if termination == "cancel"
        else SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
    )
    assert result.core_audit is draft.core_audit and result.audit_report is report
    assert result.run_manifest.search_was_truncated and not result.run_manifest.optimality_proven
    assert result.run_manifest.diagnostic_codes == tuple(issue.code for issue in result.issues)


def test_unexpected_signing_exception_is_not_misreported_as_a_search_outcome(monkeypatch):
    *_, runtime, draft = draft_case()

    def fail(*args, **kwargs):
        raise RuntimeError("unexpected sealing failure")

    monkeypatch.setattr(SchedulingRelease, "__init__", fail)
    with pytest.raises(RuntimeError, match="unexpected sealing"):
        seal_scheduling_result(draft, passed_report(draft), runtime)


def test_real_underweight_release_passes_both_layers_and_retains_confirmation_requirement():
    from apsgo_scheduler.app.contract_audit import audit_result_contract

    definition = RuleDefinitionSpec(
        "weight",
        "ChainWeightRangeRule",
        "链重",
        RuleScope.CHAIN,
        True,
        "1",
        {"min_weight": D(100), "max_weight": D(200), "target_weight": D(100)},
    )
    request = make_request(
        rule_set_spec=make_spec(
            rules=(definition,),
            allowed_final_deviation_codes=frozenset({"chain_weight_below_minimum"}),
        )
    )
    request, problem, core, runtime, draft = draft_case(request)
    report = audit_result_contract(draft, request, problem, core, runtime)
    assert report.passed
    result = seal_scheduling_result(draft, report, runtime)
    assert result.status is SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION
    assert result.confirmation_required and result.release is not None
    assert result.release.resource_facts is core.release.resource_facts


def test_audited_equivalent_search_record_order_is_not_rejected_by_public_mapping():
    from apsgo_scheduler.app.contract_audit import audit_result_contract

    definition = RuleDefinitionSpec(
        "weight",
        "ChainWeightRangeRule",
        "链重",
        RuleScope.CHAIN,
        True,
        "1",
        {"min_weight": D(0), "max_weight": D(10), "target_weight": D(10)},
    )
    request, problem, core, runtime, _ = draft_case(
        make_request(rule_set_spec=make_spec(rules=(definition,)))
    )
    rules = load_rule_set(request.rule_set_spec)
    original = core.diagnostic_candidate.search_evaluation
    assert len(original.chain_evaluations) == 2
    reordered = replace(original, chain_evaluations=tuple(reversed(original.chain_evaluations)))
    state = SearchState(core.diagnostic_candidate.plan, reordered)
    audit = final_audit.audit_core_without_search_cache(
        CoreCandidateSnapshot(state.current_plan, reordered), problem, rules, runtime
    )
    assert audit.report.passed and audit.report.search_evaluation_matches
    assert reordered != audit.audited_evaluation
    core = solver.build_core_solver_result(
        problem,
        rules,
        request.policy,
        runtime,
        state=state,
        metrics=core.metrics,
        trace=core.trace,
        audit=audit,
    )
    draft = assemble_draft_scheduling_result(request, core, make_manifest(request, core))
    report = audit_result_contract(draft, request, problem, core, runtime)
    assert report.passed
    result = seal_scheduling_result(draft, report, runtime)
    assert result.status is SolveStatus.SUCCESS
    assert result.diagnostic_candidate.search_evaluation is reordered
    assert result.release.evaluation is audit.audited_evaluation
