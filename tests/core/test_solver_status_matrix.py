"""Core status and release signing depend on audited facts, not search optimism."""

from dataclasses import fields, replace
from decimal import Decimal, Inexact, localcontext
from types import SimpleNamespace

import pytest

from apsgo_scheduler.core import controlled_split, final_audit, resource_facts, solver
from apsgo_scheduler.core.contracts import (
    AuditedCoreRelease,
    CoreAuditStatus,
    CoreCandidateSnapshot,
    SearchStopReason,
    SolveMetrics,
    SolverResult,
    SolveStatus,
    fingerprint,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, MaterialRole, SchedulePlan, SearchState
from tests.core.audit.test_final_audit_without_cache import evaluation_context
from tests.core.graph.test_construction_order import node, rules
from tests.core.rules.test_continuous_narrow_steel_weight import rule as narrow_rule
from tests.core.rules.test_process_rule_set import priority_rule, width_rule
from tests.core.rules.test_temperature_overlap import rule as temperature_rule
from tests.core.rules.test_thickness_transition import rule as thickness_rule
from tests.core.search.test_controlled_order_split import split_case, suppress_replay
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.search.test_whole_chain_neighborhood import weight_rule
from tests.core.test_solver_orchestration import resign_problem, solver_case

D = Decimal
UNDERWEIGHT = "chain_weight_below_minimum"


def audited_case(*, chains=None, **changes):
    args = solver_case(**changes)
    problem, rule_set, _, runtime = args
    plan = SchedulePlan(
        (Chain("kept-chain-id", problem.nodes, problem.period_order[0]),)
        if chains is None
        else tuple(chains)
    )
    state = SearchState(plan, evaluate_plan(plan, rule_set, evaluation_context(problem)))
    runtime.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
    audit = final_audit.audit_core_without_search_cache(
        CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
        problem,
        rule_set,
        runtime,
    )
    return args, dict(
        state=state,
        metrics=SolveMetrics(final_chain_count=len(plan.chains)),
        audit=audit,
    )


def assert_failed_closed(result, status, reason):
    assert result.status is status and result.stop_reason is reason
    assert result.release is None and result.core_result_fingerprint


def report_identity(args, state, audit):
    """Independently reproduce the already frozen audit identity contract."""
    return fingerprint(
        dict(
            problem_fingerprint=args[0].input_fingerprint,
            rule_set_fingerprint=args[1].fingerprint,
            plan_fingerprint=fingerprint(state.current_plan),
            search_evaluation_fingerprint=fingerprint(state.current_evaluation),
            report={
                field.name: getattr(audit.report, field.name)
                for field in fields(audit.report)
                if field.name != "report_fingerprint"
            },
            issues=audit.issues,
        )
    )


@pytest.mark.parametrize(
    "weights,allowed,status",
    (
        (("100",), (), SolveStatus.SUCCESS),
        (("70",), (UNDERWEIGHT,), SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION),
        (("70",), (), SolveStatus.COMPLETE_NOT_PUBLISHABLE),
        (("110", "110"), (UNDERWEIGHT,), SolveStatus.COMPLETE_NOT_PUBLISHABLE),
    ),
)
def test_status_distinguishes_success_allowed_underweight_and_business_prohibition(
    weights, allowed, status
):
    active = replace(
        rules(),
        rules=(weight_rule(minimum="100", maximum="200"),),
        allowed_final_deviation_codes=frozenset(allowed),
    )
    args, options = audited_case(
        nodes=tuple(node(str(index), weight=weight) for index, weight in enumerate(weights)),
        rule_set=active,
    )
    result = solver.build_core_solver_result(*args, **options)
    assert result.status is status
    assert result.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert result.core_audit.status is CoreAuditStatus.COMPLETED
    assert result.diagnostic_candidate.plan is options["state"].current_plan
    assert (result.release is not None) is options["audit"].report.passed


@pytest.mark.parametrize(
    "reason",
    (
        SearchStopReason.LOCAL_SEARCH_COMPLETE,
        SearchStopReason.CANDIDATE_LIMIT_REACHED,
        SearchStopReason.SEARCH_TIME_LIMIT_REACHED,
    ),
)
def test_search_truncation_does_not_erase_a_completed_passed_audit(reason):
    args, options = audited_case()
    args[-1].stop_reason = reason
    result = solver.build_core_solver_result(*args, **options)
    assert result.status is SolveStatus.SUCCESS and result.release is not None
    assert result.stop_reason is reason and result.core_audit.passed


@pytest.mark.parametrize("complete", (False, True))
@pytest.mark.parametrize(
    "reason,empty_status,complete_status",
    (
        (
            SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
            SolveStatus.NO_COMPLETE_PLAN,
            SolveStatus.COMPLETE_NOT_PUBLISHABLE,
        ),
        (SearchStopReason.USER_CANCELLED, SolveStatus.CANCELLED, SolveStatus.CANCELLED),
        (SearchStopReason.INPUT_INVALID, SolveStatus.FAILED, SolveStatus.FAILED),
        (SearchStopReason.SYSTEM_ERROR, SolveStatus.FAILED, SolveStatus.FAILED),
    ),
)
def test_terminal_status_never_releases_even_with_an_earlier_passed_audit(
    complete, reason, empty_status, complete_status
):
    args, options = audited_case()
    args[-1].stop_reason = reason
    if not complete:
        options = dict(state=None, metrics=SolveMetrics())
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, complete_status if complete else empty_status, reason)
    assert (result.diagnostic_candidate is not None) is complete


@pytest.mark.parametrize(
    "reason", (SearchStopReason.SEARCH_TIME_LIMIT_REACHED, SearchStopReason.CANDIDATE_LIMIT_REACHED)
)
def test_no_complete_plan_has_no_audit_or_release(reason):
    args = solver_case()
    args[-1].stop_reason = reason
    result = solver.build_core_solver_result(*args, state=None, metrics=SolveMetrics())
    assert_failed_closed(result, SolveStatus.NO_COMPLETE_PLAN, reason)
    assert result.diagnostic_candidate is None
    assert result.core_audit.status is CoreAuditStatus.NOT_RUN
    assert result.core_audit.search_evaluation_matches is None


@pytest.mark.parametrize("failure", ("structure", "search_evaluation"))
def test_real_audit_integrity_failures_are_failed_not_business_infeasibility(failure):
    args, options = audited_case(nodes=(node("a"), node("b")))
    state = options["state"]
    if failure == "structure":
        state.current_plan = SchedulePlan((Chain("kept-chain-id", (args[0].nodes[0],), "period"),))
        state.current_evaluation = evaluate_plan(
            state.current_plan, args[1], evaluation_context(args[0])
        )
    else:
        state.current_evaluation = replace(state.current_evaluation, quality_key=(99, D(99)))
    options["audit"] = final_audit.audit_core_without_search_cache(
        CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
        args[0],
        args[1],
        args[-1],
    )
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR)
    assert result.core_audit.status is CoreAuditStatus.COMPLETED and not result.core_audit.passed


@pytest.mark.parametrize(
    "owner,field",
    (
        ("state", "split_sequence"),
        ("state", "accepted_same_period_split_count"),
        ("state", "accepted_future_borrow_return_count"),
        ("metrics", "accepted_split_count"),
        ("metrics", "accepted_same_period_split_count"),
        ("metrics", "accepted_future_borrow_return_count"),
        ("report", "audited_split_count"),
        ("report", "audited_same_period_split_count"),
        ("report", "audited_future_borrow_return_count"),
    ),
)
def test_every_split_count_is_checked_across_all_three_owners(owner, field):
    args, options = audited_case()
    target = options["audit"].report if owner == "report" else options[owner]
    # Deliberately contaminate a previously valid carrier to test the signing boundary.
    object.__setattr__(target, field, 1)
    if owner == "report":
        object.__setattr__(
            target, "report_fingerprint", report_identity(args, options["state"], options["audit"])
        )
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR)


@pytest.mark.parametrize(
    "field",
    (
        "audited_split_count",
        "audited_same_period_split_count",
        "audited_future_borrow_return_count",
    ),
)
def test_missing_audited_split_count_is_not_assumed_to_be_zero(field):
    args, options = audited_case()
    object.__setattr__(options["audit"].report, field, None)
    object.__setattr__(
        options["audit"].report,
        "report_fingerprint",
        report_identity(args, options["state"], options["audit"]),
    )
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR)


@pytest.mark.parametrize(
    "tamper",
    (
        "plan",
        "search_evaluation",
        "evaluation",
        "facts",
        "report",
        "audit_issues",
        "foreign_audit",
        "problem",
        "rules",
    ),
)
def test_passed_flag_cannot_authorize_changed_audited_material(tamper):
    args, options = audited_case()
    state, audit = options["state"], options["audit"]
    if tamper == "plan":
        state.current_plan = SchedulePlan(
            (replace(state.current_plan.chains[0], chain_id="other"),)
        )
    elif tamper == "search_evaluation":
        state.current_evaluation = replace(state.current_evaluation, quality_key=(1, D(1)))
    elif tamper == "evaluation":
        options["audit"] = replace(
            audit, audited_evaluation=replace(audit.audited_evaluation, quality_key=(1, D(1)))
        )
    elif tamper == "facts":
        # Preserve the advertised identity while changing the underlying immutable facts.
        options["audit"] = replace(
            audit,
            resource_facts=replace(audit.resource_facts, input_real_weight=D(999)),
        )
    elif tamper == "report":
        options["audit"] = replace(audit, report=replace(audit.report, report_fingerprint="wrong"))
    elif tamper == "audit_issues":
        # A genuinely failed audit supplies a valid diagnostic but cannot be spliced into success.
        bad_state = replace(
            state, current_evaluation=replace(state.current_evaluation, quality_key=(1, D(1)))
        )
        failed = final_audit.audit_core_without_search_cache(
            CoreCandidateSnapshot(bad_state.current_plan, bad_state.current_evaluation),
            args[0],
            args[1],
            args[-1],
        )
        options["audit"] = replace(audit, issues=failed.issues)
    elif tamper == "foreign_audit":
        foreign_plan = SchedulePlan((replace(state.current_plan.chains[0], chain_id="other"),))
        foreign = CoreCandidateSnapshot(
            foreign_plan, evaluate_plan(foreign_plan, args[1], evaluation_context(args[0]))
        )
        options["audit"] = final_audit.audit_core_without_search_cache(
            foreign, args[0], args[1], args[-1]
        )
        assert options["audit"].report.passed
    elif tamper == "problem":
        changed = resign_problem(
            args[0], nodes=(replace(args[0].nodes[0], source_resource_id="other-resource"),)
        )
        args = (changed, *args[1:])
    else:
        args = (args[0], replace(args[1], version="2", fingerprint="other-rules"), *args[2:])
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR)
    expected_code = (
        "audited_evaluation_identity_mismatch"
        if tamper == "evaluation"
        else "audited_resource_identity_mismatch"
        if tamper == "facts"
        else "core_audit_binding_mismatch"
    )
    assert any(issue.code == expected_code for issue in result.issues)


@pytest.mark.parametrize("field", ("audited_evaluation", "resource_facts"))
def test_passed_report_with_missing_audit_material_cannot_release(field):
    args, options = audited_case()
    object.__setattr__(options["audit"], field, None)
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR)


def test_release_preserves_the_exact_audited_objects_and_has_one_aggregate_identity(monkeypatch):
    args, options = audited_case()
    audit, state = options["audit"], options["state"]
    for module in (resource_facts, final_audit):
        monkeypatch.setattr(
            module, "_derive_audited_resource_facts", lambda *a: pytest.fail("facts re-derived")
        )
    monkeypatch.setattr(
        solver, "audit_core_without_search_cache", lambda *a: pytest.fail("audit rerun in signing")
    )
    result = solver.build_core_solver_result(*args, **options)
    release = result.release
    assert release.canonical_plan is state.current_plan
    assert release.audited_evaluation is audit.audited_evaluation
    assert release.resource_facts is audit.resource_facts
    assert release.plan_fingerprint == fingerprint(state.current_plan)
    assert release.evaluation_fingerprint == fingerprint(audit.audited_evaluation)
    assert release.resource_fingerprint == audit.resource_facts.facts_fingerprint
    assert release.core_audit_fingerprint == audit.report.report_fingerprint
    assert release.release_fingerprint == fingerprint(
        dict(
            plan_fingerprint=release.plan_fingerprint,
            evaluation_fingerprint=release.evaluation_fingerprint,
            resource_fingerprint=release.resource_fingerprint,
            core_audit_fingerprint=release.core_audit_fingerprint,
        )
    )


@pytest.mark.parametrize("point", ("audit", "release", "result"))
@pytest.mark.parametrize("termination", ("cancel", "time"))
def test_termination_after_audit_or_signing_still_prevents_release(point, termination, monkeypatch):
    signal = SimpleNamespace(cancelled=False, expired=False)
    args, options = audited_case(
        cancellation=SimpleNamespace(is_cancelled=lambda: signal.cancelled),
        clock=lambda: 1000.0 if signal.expired else 1.0,
    )

    def interrupt():
        setattr(signal, "cancelled" if termination == "cancel" else "expired", True)

    if point == "audit":
        interrupt()
    else:
        target = AuditedCoreRelease if point == "release" else SolverResult
        original = target.__init__

        def stop_after_construction(self, *values, **keywords):
            original(self, *values, **keywords)
            interrupt()

        monkeypatch.setattr(target, "__init__", stop_after_construction)
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(
        result,
        SolveStatus.CANCELLED if termination == "cancel" else SolveStatus.COMPLETE_NOT_PUBLISHABLE,
        SearchStopReason.USER_CANCELLED
        if termination == "cancel"
        else SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED,
    )
    assert result.diagnostic_candidate.plan is options["state"].current_plan


def real_split_case(monkeypatch, *, origin="P2", anchor=False):
    state, context = split_case(origin=origin, anchor=anchor)
    with monkeypatch.context() as scope:
        suppress_replay(scope)
        controlled_split.run_controlled_order_split(state, context)
    problem = resign_problem(context.factory.cache.problem)
    active, runtime = context.factory.cache.rule_set, context.factory.budget
    policy = replace(
        context.policy, total_time_limit_seconds=D(110), finalization_reserve_seconds=D(10)
    )
    audit = final_audit.audit_core_without_search_cache(
        CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
        problem,
        active,
        runtime,
    )
    assert audit.report.passed and state.split_sequence == 1
    metrics = SolveMetrics(
        candidate_check_count=runtime.candidate_check_count,
        complete_candidate_evaluation_count=context.complete_candidate_evaluation_count,
        accepted_move_count=state.accepted_move_count,
        accepted_split_count=state.split_sequence,
        accepted_same_period_split_count=state.accepted_same_period_split_count,
        accepted_future_borrow_return_count=state.accepted_future_borrow_return_count,
        final_chain_count=len(state.current_plan.chains),
    )
    return (problem, active, policy, runtime), dict(
        state=state, metrics=metrics, trace=context.accepted_move_traces, audit=audit
    )


@pytest.mark.parametrize(
    "origin,anchor,counts", (("P2", False, (1, 1, 0)), ("P9", True, (1, 0, 1)))
)
def test_real_accepted_partition_is_signed_only_with_matching_mode_counts(
    origin, anchor, counts, monkeypatch
):
    args, options = real_split_case(monkeypatch, origin=origin, anchor=anchor)
    result = solver.build_core_solver_result(*args, **options)
    assert result.status is SolveStatus.SUCCESS and result.release is not None
    assert (
        result.metrics.accepted_split_count,
        result.metrics.accepted_same_period_split_count,
        result.metrics.accepted_future_borrow_return_count,
    ) == counts
    assert (
        result.core_audit.audited_split_count,
        result.core_audit.audited_same_period_split_count,
        result.core_audit.audited_future_borrow_return_count,
    ) == counts
    assert result.release.resource_facts is options["audit"].resource_facts
    assert len(result.release.resource_facts.split_partitions) == 1
    assert result.trace == options["trace"]


def test_actual_split_authorization_failure_is_failed_not_publishable(monkeypatch):
    args, options = real_split_case(monkeypatch)
    state = options["state"]
    state.current_plan = SchedulePlan(
        tuple(
            replace(
                chain,
                nodes=tuple(
                    replace(
                        item,
                        split_lineage=replace(
                            item.split_lineage, authorization_decision_fingerprint="wrong"
                        ),
                    )
                    if item.split_lineage is not None
                    else item
                    for item in chain.nodes
                ),
            )
            for chain in state.current_plan.chains
        )
    )
    options["audit"] = final_audit.audit_core_without_search_cache(
        CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
        args[0],
        args[1],
        args[-1],
    )
    assert options["audit"].report.action_authorization_failure_codes
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR)


def test_missing_stop_reason_is_a_contract_failure_not_invented_natural_completion():
    args, options = audited_case()
    args[-1].stop_reason = None
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR)
    assert result.issues


def test_complete_candidate_without_an_independent_audit_cannot_be_signed():
    args, options = audited_case()
    options["audit"] = None
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR)
    assert result.core_audit.status is CoreAuditStatus.NOT_RUN


def test_candidate_metric_must_equal_the_existing_runtime_counter():
    args, options = audited_case()
    options["metrics"] = replace(options["metrics"], candidate_check_count=1)
    result = solver.build_core_solver_result(*args, **options)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.SYSTEM_ERROR)
    assert any(issue.code == "candidate_count_mismatch" for issue in result.issues)


@pytest.mark.parametrize("argument", range(4))
def test_incorrect_core_argument_types_are_programming_errors(argument):
    args = list(solver_case())
    args[argument] = object()
    with pytest.raises(ValueError):
        solver.solve(*args)
    with pytest.raises(ValueError):
        solver.build_core_solver_result(*args, state=None, metrics=SolveMetrics())


@pytest.mark.parametrize("field", ("product_line_code", "process_code", "scenario"))
def test_core_domain_identity_must_match_without_silent_text_normalization(field, monkeypatch):
    problem, active, policy, runtime = solver_case()
    problem = resign_problem(problem, **{field: " " + getattr(problem, field)})
    monkeypatch.setattr(
        solver, "build_construction_dag", lambda *a, **k: pytest.fail("invalid graph")
    )
    result = solver.solve(problem, active, policy, runtime)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
    assert any(
        issue.code == "rule_set_domain_mismatch" and issue.field_path == field
        for issue in result.issues
    )


def test_core_detects_stale_semantic_problem_identity_before_construction(monkeypatch):
    problem, active, policy, runtime = solver_case()
    problem = replace(problem, nodes=(replace(problem.nodes[0], weight=D(21)),))
    monkeypatch.setattr(
        solver, "build_construction_dag", lambda *a, **k: pytest.fail("invalid graph")
    )
    result = solver.solve(problem, active, policy, runtime)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
    assert any(issue.code == "problem_fingerprint_mismatch" for issue in result.issues)


@pytest.mark.parametrize(
    "field",
    (
        "candidate_check_limit",
        "search_deadline_monotonic",
        "final_deadline_monotonic",
        "started_at_monotonic",
    ),
)
def test_shared_runtime_must_remain_bound_to_policy_and_original_start(field):
    args = solver_case()
    setattr(args[-1], field, getattr(args[-1], field) + 1)
    result = solver.solve(*args)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
    assert any(issue.code == "runtime_policy_mismatch" for issue in result.issues)


def test_typed_but_contaminated_policy_is_rejected_at_core_entry():
    args = solver_case()
    object.__setattr__(args[2], "numeric_semantics_key", "unknown")
    result = solver.solve(*args)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
    assert any(issue.code == "invalid_solver_policy_or_runtime" for issue in result.issues)


@pytest.mark.parametrize("role", (MaterialRole.NORMAL_REAL, MaterialRole.ACTUAL_TRANSITION))
@pytest.mark.parametrize("field", ("width", "grade_class"))
def test_enabled_required_fields_apply_to_every_real_input_role(role, field):
    item = replace(node("real", width=None if field == "width" else "1000"), material_role=role)
    active = width_rule() if field == "width" else narrow_rule()
    result = solver.solve(*solver_case(nodes=(item,), rule_items=(active,)))
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
    assert any(
        issue.code == "missing_required_field"
        and issue.field_path == field
        and issue.subject_id == "real"
        for issue in result.issues
    )


@pytest.mark.parametrize(
    "field,active",
    (
        ("thickness", thickness_rule()),
        ("min_temperature", temperature_rule()),
        ("max_temperature", temperature_rule()),
    ),
)
def test_other_enabled_physical_requirements_fail_before_search(field, active):
    result = solver.solve(
        *solver_case(nodes=(replace(node("real"), **{field: None}),), rule_items=(active,))
    )
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
    assert any(
        issue.code == "missing_required_field" and issue.field_path == field
        for issue in result.issues
    )


@pytest.mark.parametrize(
    "field", ("width", "thickness", "min_temperature", "max_temperature", "weight")
)
def test_real_calculation_projection_overflow_is_rejected_without_changing_original_decimal(field):
    changes = {field: D("1e1000")}
    if field == "min_temperature":
        changes["max_temperature"] = D("1e1001")
    item = replace(node("real"), **changes)
    args = solver_case(nodes=(item,))
    result = solver.solve(*args)
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
    assert getattr(args[0].nodes[0], field) is changes[field]
    assert any(
        issue.code == "non_finite_computation_value" and issue.field_path == field
        for issue in result.issues
    )


def test_large_but_finite_sort_weight_is_not_given_an_unconfigured_business_upper_bound():
    weight = D("1e100")
    item = replace(node("real"), weight=weight)
    result = solver.solve(*solver_case(nodes=(item,)))
    assert result.status is SolveStatus.SUCCESS and result.release is not None
    assert result.release.canonical_plan.chains[0].nodes[0].weight is weight
    assert result.release.resource_facts.input_real_weight == weight


@pytest.mark.parametrize("value", (True, "1", D(1)))
def test_node_priority_type_is_validated_by_the_existing_rule(value):
    result = solver.solve(
        *solver_case(nodes=(node("real", priority=value),), rule_items=(priority_rule(),))
    )
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
    assert any(
        issue.code == "invalid_construction_priority" and issue.subject_id == "real"
        for issue in result.issues
    )


def test_zero_priority_and_unread_boolean_attribute_remain_valid_and_unchanged():
    item = replace(
        node("real", priority=0), rule_attributes={"priority": 0, "optional_flag": False}
    )
    args = solver_case(nodes=(item,), rule_items=(priority_rule(),))
    result = solver.solve(*args)
    assert result.status is SolveStatus.SUCCESS
    assert result.release.canonical_plan.chains[0].nodes[0] is item
    assert dict(item.rule_attributes) == {"priority": 0, "optional_flag": False}


@pytest.mark.parametrize("field,active", (("width", width_rule()), ("thickness", thickness_rule())))
def test_virtual_prototypes_require_only_applicable_edge_dimensions(field, active):
    item = prototype("p", **{field: None})
    result = solver.solve(*solver_case(prototypes=(item,), rule_items=(active,)))
    assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
    assert any(
        issue.code == "missing_required_field"
        and issue.field_path == field
        and issue.subject_id == "p"
        for issue in result.issues
    )


def test_virtual_prototype_does_not_need_real_sources_temperature_or_grade_class():
    item = prototype("p", weight="1e1000", grade="", rule_attributes={})
    args = solver_case(
        nodes=(replace(node("real"), rule_attributes={"grade_class": "OTHER"}),),
        prototypes=(item,),
        rule_items=(temperature_rule(), narrow_rule()),
    )
    result = solver.solve(*args)
    assert result.status is SolveStatus.SUCCESS and result.release is not None
    assert args[0].virtual_prototypes[0] is item


def test_disabled_rule_does_not_reintroduce_its_input_requirement_or_priority_read():
    item = replace(
        node("real", width=None, priority=True), min_temperature=None, max_temperature=None
    )
    result = solver.solve(
        *solver_case(
            nodes=(item,),
            rule_items=(
                width_rule(enabled=False),
                priority_rule(enabled=False),
                temperature_rule(enabled=False),
            ),
        )
    )
    assert result.status is SolveStatus.SUCCESS and result.release is not None


@pytest.mark.parametrize(
    "weight,valid", (("100", True), ("100.000001", True), ("100.0000010001", False))
)
def test_atomic_chain_maximum_uses_exact_epsilon_independent_of_decimal_context(weight, valid):
    args = solver_case(nodes=(node("real", weight=weight),), rule_items=(weight_rule(),), limit=0)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        result = solver.solve(*args)
    if valid:
        assert result.status is SolveStatus.SUCCESS and result.release is not None
    else:
        assert_failed_closed(result, SolveStatus.FAILED, SearchStopReason.INPUT_INVALID)
        assert any(issue.code == "atomic_node_above_chain_maximum" for issue in result.issues)
