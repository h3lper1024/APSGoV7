"""The second audit checks mappings, never reruns scheduling or resource derivation."""

import inspect
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, Inexact, localcontext

import pytest

from apsgo_scheduler.api.request import (
    OrderInput,
    PeriodInput,
    QualityCriterionSpec,
    RuleDefinitionSpec,
    VirtualPrototypeInput,
    fingerprint_public_request,
)
from apsgo_scheduler.api.result import ResultAuditStatus, RunManifest
from apsgo_scheduler.app import contract_audit
from apsgo_scheduler.app.contract_audit import audit_result_contract
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.result_assembler import assemble_draft_scheduling_result
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core import evaluation, final_audit, resource_facts, solver
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import ControlledSplitMode, SearchStopReason, fingerprint
from apsgo_scheduler.core.model import MaterialRole
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet
from tests.app.test_input_normalizer import make_request, make_spec
from tests.core.search.test_controlled_order_split import parent, split_case
from tests.core.search.test_single_real_node_relocation import Stop

D = Decimal


def values(item, omitted=()):
    return {
        field.name: getattr(item, field.name) for field in fields(item) if field.name not in omitted
    }


def rich_case(*, cancellation=None, clock=lambda: 1.0):
    _, previous = split_case(
        parents=(
            parent(),
            parent("borrowed", weight="10", source="P1", rule_attributes={"grade_class": "OTHER"}),
        ),
        anchor=True,
    )
    old = previous.factory.cache
    definitions = tuple(
        RuleDefinitionSpec(
            item.rule_id,
            type(item).__name__,
            item.name,
            item.scope,
            item.enabled,
            item.version,
            item.parameters,
        )
        for item in old.rule_set.rules
    )
    criteria = tuple(
        QualityCriterionSpec(
            item.criterion_id,
            item.metric_key,
            item.direction.value,
            item.aggregation.value,
            item.numeric_projection.value,
        )
        for item in old.rule_set.quality_spec
    )
    spec = make_spec(rules=definitions, quality_spec=criteria)
    inputs = tuple(
        OrderInput(**{field.name: getattr(item, field.name) for field in fields(OrderInput)})
        for item in old.problem.nodes
    )
    inputs = (replace(inputs[0], material_role=MaterialRole.ACTUAL_TRANSITION), *inputs[1:])
    request = make_request(
        rule_set_spec=spec,
        orders=inputs,
        periods=tuple(
            PeriodInput(name, index) for index, name in enumerate(old.problem.period_order)
        ),
        virtual_prototypes=tuple(
            VirtualPrototypeInput(**values(item)) for item in old.problem.virtual_prototypes
        ),
        policy=replace(previous.policy, candidate_check_limit=1000),
    )
    rules = load_rule_set(spec)
    problem = normalize_input(request, rules)
    runtime = SolveRuntimeBudget.from_policy(request.policy, 0.0, cancellation, clock=clock)
    core = solver.solve(problem, rules, request.policy, runtime)
    assert core.release is not None, core.issues
    manifest = RunManifest(
        algorithm_version="path-cover-local-search-v1",
        code_revision="unversioned",
        request_fingerprint=fingerprint_public_request(request),
        problem_fingerprint=core.problem_fingerprint,
        rule_set_fingerprint=core.rule_set_fingerprint,
        policy_fingerprint=core.policy_fingerprint,
        stop_reason=core.stop_reason,
        search_was_truncated=core.stop_reason.search_was_truncated,
        optimality_proven=False,
        counters=values(core.metrics, ("stage_duration_seconds",)),
        diagnostic_codes=tuple(issue.code for issue in core.issues),
        stage_duration_seconds=core.metrics.stage_duration_seconds,
        trace_fingerprint=fingerprint(core.trace),
    )
    draft = assemble_draft_scheduling_result(request, core, manifest)
    return draft, request, problem, core, runtime


def altered_facts(draft, **changes):
    original = draft.proposed_release.resource_facts
    copied = replace(original, **changes)
    copied = replace(copied, facts_fingerprint=fingerprint(values(copied, ("facts_fingerprint",))))
    return replace(draft, proposed_release=replace(draft.proposed_release, resource_facts=copied))


def assert_failed(report, code):
    assert report.status is ResultAuditStatus.COMPLETED and not report.passed
    assert code in report.failure_codes
    assert report.plan_fingerprint and report.resource_fingerprint and report.draft_fingerprint
    assert report.report_fingerprint == fingerprint(values(report, ("report_fingerprint",)))


def test_real_all_five_fact_types_pass_without_evaluation_normalization_or_derivation(monkeypatch):
    args = rich_case()
    draft, request, problem, core, runtime = args
    facts = core.release.resource_facts
    assert all(
        (
            facts.assignments,
            facts.future_borrows,
            facts.virtual_generations,
            facts.split_partitions,
            facts.actual_transitions,
        )
    )
    before = fingerprint(args[:-1])

    def forbidden(*args, **kwargs):
        raise AssertionError("the second audit must only check existing values")

    monkeypatch.setattr(evaluation, "evaluate_plan", forbidden)
    monkeypatch.setattr(final_audit, "audit_core_without_search_cache", forbidden)
    monkeypatch.setattr(resource_facts, "_derive_audited_resource_facts", forbidden)
    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", forbidden)
    report = audit_result_contract(*args)
    assert report.status is ResultAuditStatus.COMPLETED and report.passed, report
    assert report.failure_codes == ()
    assert report.plan_fingerprint == core.release.plan_fingerprint
    assert report.resource_fingerprint == facts.facts_fingerprint
    assert report.draft_fingerprint == draft.draft_fingerprint
    assert draft.proposed_release.resource_facts is facts
    assert report.report_fingerprint == fingerprint(values(report, ("report_fingerprint",)))
    assert fingerprint((draft, request, problem, core)) == before
    assert runtime.candidate_check_count == core.metrics.candidate_check_count
    with pytest.raises(FrozenInstanceError):
        report.passed = False


def test_equal_fact_copy_is_rejected_even_when_values_and_fingerprint_match():
    draft, *remaining = rich_case()
    copied = altered_facts(draft)
    assert copied.proposed_release.resource_facts == draft.proposed_release.resource_facts
    report = audit_result_contract(copied, *remaining)
    assert_failed(report, "resource_reference_mismatch")


@pytest.mark.parametrize(
    "collection,field,value,code",
    [
        ("assignments", "position", 99, "node_assignment_mismatch"),
        ("assignments", "source_resource_id", "other", "node_assignment_mismatch"),
        ("assignments", "weight", D(11), "node_assignment_mismatch"),
        ("future_borrows", "source_period", "P2", "future_borrow_mapping_mismatch"),
        ("future_borrows", "weight", D(11), "future_borrow_mapping_mismatch"),
        ("virtual_generations", "prototype_id", "other", "virtual_generation_mapping_mismatch"),
        ("virtual_generations", "accepted_sequence", 99, "virtual_generation_mapping_mismatch"),
        ("actual_transitions", "source_order_id", "other", "actual_transition_mapping_mismatch"),
        ("actual_transitions", "assigned_period", "P2", "actual_transition_mapping_mismatch"),
    ],
)
def test_specific_fact_fields_are_independently_compared(collection, field, value, code):
    draft, *remaining = rich_case()
    rows = getattr(draft.proposed_release.resource_facts, collection)
    changed = (replace(rows[0], **{field: value}), *rows[1:])
    report = audit_result_contract(altered_facts(draft, **{collection: changed}), *remaining)
    assert_failed(report, code)


@pytest.mark.parametrize(
    "collection,code",
    [
        ("assignments", "node_assignment_mismatch"),
        ("future_borrows", "future_borrow_mapping_mismatch"),
        ("virtual_generations", "virtual_generation_mapping_mismatch"),
        ("actual_transitions", "actual_transition_mapping_mismatch"),
        ("split_partitions", "split_partition_mapping_mismatch"),
    ],
)
@pytest.mark.parametrize("change", ("missing", "duplicate"))
def test_missing_and_duplicate_fact_rows_do_not_disappear_in_set_comparison(
    collection, code, change
):
    draft, *remaining = rich_case()
    rows = getattr(draft.proposed_release.resource_facts, collection)
    changed = rows[1:] if change == "missing" else (*rows, rows[0])
    assert_failed(
        audit_result_contract(altered_facts(draft, **{collection: changed}), *remaining), code
    )


@pytest.mark.parametrize(
    "field",
    (
        "input_real_weight",
        "scheduled_real_weight",
        "generated_virtual_weight",
        "future_pool_weight",
        "borrowed_future_weight",
    ),
)
def test_every_total_is_checked_against_exact_existing_node_values(field):
    draft, *remaining = rich_case()
    facts = draft.proposed_release.resource_facts
    changed = altered_facts(draft, **{field: getattr(facts, field) + D(1)})
    assert_failed(audit_result_contract(changed, *remaining), "resource_totals_mismatch")


@pytest.mark.parametrize(
    "field,value",
    (
        ("authorization_decision_fingerprint", "other"),
        ("authorization_rule_id", "other"),
        ("reason_code", "other"),
        ("origin_assigned_period", "P1"),
        ("accepted_source_sequence", 2),
        ("partition_fingerprint", "other"),
    ),
)
def test_partition_metadata_must_match_existing_piece_lineage(field, value):
    draft, *remaining = rich_case()
    fact = draft.proposed_release.resource_facts.split_partitions[0]
    changes = {field: value}
    if field == "origin_assigned_period":
        changes["split_mode"] = ControlledSplitMode.FUTURE_BORROW_RETURN
    changed = altered_facts(draft, split_partitions=(replace(fact, **changes),))
    assert_failed(audit_result_contract(changed, *remaining), "split_partition_mapping_mismatch")


@pytest.mark.parametrize(
    "kind,code",
    (
        ("request", "request_identity_mismatch"),
        ("metrics", "metrics_mismatch"),
        ("trace", "manifest_binding_mismatch"),
        ("core_release", "core_release_binding_mismatch"),
        ("plan", "plan_mapping_mismatch"),
        ("evaluation", "evaluation_mapping_mismatch"),
        ("snapshot", "diagnostic_candidate_mismatch"),
        ("core_result", "core_result_identity_mismatch"),
    ),
)
def test_semantic_bindings_are_checked_even_with_a_fresh_valid_draft_hash(kind, code):
    draft, request, problem, core, runtime = rich_case()
    if kind == "request":
        draft = replace(draft, request_id="other")
    elif kind == "metrics":
        draft = replace(draft, metrics=replace(draft.metrics, candidate_check_count=99))
    elif kind == "trace":
        draft = replace(draft, run_manifest=replace(draft.run_manifest, trace_fingerprint="other"))
    elif kind == "core_release":
        draft = replace(
            draft,
            proposed_release=replace(draft.proposed_release, core_release_fingerprint="other"),
        )
    elif kind == "plan":
        plan = draft.proposed_release.plan
        draft = replace(
            draft,
            proposed_release=replace(
                draft.proposed_release, plan=replace(plan, chains=tuple(reversed(plan.chains)))
            ),
        )
    elif kind == "evaluation":
        evaluation = draft.proposed_release.evaluation
        draft = replace(
            draft,
            proposed_release=replace(
                draft.proposed_release,
                evaluation=replace(evaluation, metrics={**evaluation.metrics, "extra": 1}),
            ),
        )
    elif kind == "snapshot":
        draft = replace(draft, diagnostic_candidate=replace(draft.diagnostic_candidate))
    else:
        core = replace(core, core_result_fingerprint="other")
    assert_failed(audit_result_contract(draft, request, problem, core, runtime), code)


def test_unrelated_request_changed_physical_values_cannot_reuse_the_original_problem():
    draft, request, problem, core, runtime = rich_case()
    order = replace(request.orders[0], weight=request.orders[0].weight + D(1))
    request = replace(request, orders=(order, *request.orders[1:]))
    draft = replace(
        draft,
        run_manifest=replace(
            draft.run_manifest, request_fingerprint=fingerprint_public_request(request)
        ),
    )
    assert_failed(
        audit_result_contract(draft, request, problem, core, runtime),
        "request_problem_mapping_mismatch",
    )


def test_raw_whitespace_identities_are_compared_to_the_existing_normalized_problem():
    draft, request, problem, core, runtime = rich_case()
    orders = tuple(
        replace(
            item,
            **{
                name: f" {getattr(item, name)} "
                for name in ("node_id", "source_order_id", "source_resource_id", "source_period")
            },
        )
        for item in request.orders
    )
    request = replace(
        request,
        orders=orders,
        periods=tuple(replace(item, period_id=f" {item.period_id} ") for item in request.periods),
        virtual_prototypes=tuple(
            replace(item, prototype_id=f" {item.prototype_id} ")
            for item in request.virtual_prototypes
        ),
    )
    draft = replace(
        draft,
        run_manifest=replace(
            draft.run_manifest, request_fingerprint=fingerprint_public_request(request)
        ),
    )
    assert audit_result_contract(draft, request, problem, core, runtime).passed


def test_three_way_split_count_mismatch_is_separate_from_fact_mapping():
    draft, request, problem, core, runtime = rich_case()
    metrics = replace(draft.metrics, accepted_split_count=2, accepted_same_period_split_count=2)
    draft = replace(draft, metrics=metrics)
    assert_failed(
        audit_result_contract(draft, request, problem, core, runtime), "split_count_mismatch"
    )


def test_report_and_exact_weights_do_not_depend_on_ambient_decimal_precision():
    args = rich_case()
    expected = audit_result_contract(*args)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        actual = audit_result_contract(*args)
    assert actual == expected and actual.passed


@pytest.mark.parametrize(
    "where", ("before", "_request_binding", "_fact_mappings", "_split_mappings", "report_hash")
)
@pytest.mark.parametrize("reason", ("cancel", "deadline"))
def test_shared_budget_stops_before_during_and_after_checks_without_audited_identities(
    where, reason, monkeypatch
):
    token, now = Stop(), [1.0]
    args = rich_case(cancellation=token, clock=lambda: now[0])
    draft, request, problem, core, runtime = args
    before = fingerprint(args[:-1])

    def stop():
        if reason == "cancel":
            token.active = True
        else:
            now[0] = runtime.final_deadline_monotonic

    if where == "before":
        stop()
    elif where == "report_hash":
        original = contract_audit.fingerprint

        def hashing(value):
            result = original(value)
            if isinstance(value, dict) and value.get("status") is ResultAuditStatus.COMPLETED:
                stop()
            return result

        monkeypatch.setattr(contract_audit, "fingerprint", hashing)
    else:
        original = SolveRuntimeBudget.allows_finalization

        def checking(active):
            if inspect.currentframe().f_back.f_code.co_name == where:
                stop()
            return original(active)

        monkeypatch.setattr(SolveRuntimeBudget, "allows_finalization", checking)
    report = audit_result_contract(*args)
    assert report.status is (
        ResultAuditStatus.CANCELLED if reason == "cancel" else ResultAuditStatus.TIME_LIMIT
    )
    assert not report.passed
    assert report.plan_fingerprint is report.resource_fingerprint is None
    assert report.draft_fingerprint == draft.draft_fingerprint
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if reason == "cancel"
        else SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED
    )
    assert runtime.candidate_check_count == core.metrics.candidate_check_count
    assert fingerprint((draft, request, problem, core)) == before


def test_mapping_exception_is_error_not_an_unexplained_normal_stop(monkeypatch, caplog):
    args = rich_case()

    def broken(*args):
        raise RuntimeError("deliberate mapping fault")

    monkeypatch.setattr(contract_audit, "_fact_mappings", broken)
    report = audit_result_contract(*args)
    assert report.status is ResultAuditStatus.ERROR and not report.passed
    assert report.plan_fingerprint is report.resource_fingerprint is None
    assert report.failure_codes == ("result_audit_error",)
    assert args[-1].stop_reason is SearchStopReason.SYSTEM_ERROR
    assert "deliberate mapping fault" in caplog.text


def test_invalid_cancellation_value_is_an_error_not_a_cancellation():
    args = rich_case()

    class BadToken:
        def is_cancelled(self):
            return "yes"

    args[-1].cancellation = BadToken()
    report = audit_result_contract(*args)
    assert report.status is ResultAuditStatus.ERROR
    assert report.failure_codes == ("result_audit_error",)
    assert report.plan_fingerprint is report.resource_fingerprint is None


@pytest.mark.parametrize("index", range(5))
def test_invalid_argument_types_are_programming_errors(index):
    args = list(rich_case())
    args[index] = object()
    with pytest.raises(ValueError, match="result audit requires"):
        audit_result_contract(*args)
