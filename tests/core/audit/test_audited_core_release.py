"""Audit materials are immutable; release signing belongs to function nineteen."""

from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apsgo_scheduler.core import final_audit, resource_facts
from apsgo_scheduler.core.contracts import (
    AuditedCoreRelease,
    CoreAuditReport,
    CoreAuditStatus,
    RuleScope,
    SearchStopReason,
    fingerprint,
)
from apsgo_scheduler.core.rules.base import Rule, RuleContribution, RuleDisposition, RuleViolation
from tests.core.audit.test_final_audit_without_cache import assert_unfinished, audit_case
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.search.test_whole_chain_neighborhood import weight_rule

D = Decimal
UNDERWEIGHT = "chain_weight_below_minimum"


def test_completed_audit_materials_are_frozen_but_do_not_sign_a_release(monkeypatch):
    case = audit_case()
    monkeypatch.setattr(
        AuditedCoreRelease, "__init__", lambda *a, **k: pytest.fail("premature release signing")
    )
    result = final_audit.audit_core_without_search_cache(*case)
    assert result.report.status is CoreAuditStatus.COMPLETED and result.report.passed
    assert result.audited_evaluation == case[0].search_evaluation
    assert result.resource_facts is not None and result.issues == ()
    assert {field.name for field in fields(result)} == {
        "report",
        "audited_evaluation",
        "resource_facts",
        "issues",
    }
    with pytest.raises(FrozenInstanceError):
        result.resource_facts = None
    assert not hasattr(result, "release")


@pytest.mark.parametrize(
    "weight,allowed,passed",
    (
        ("100", (), True),
        ("70", (UNDERWEIGHT,), True),
        ("70", (), False),
        ("210", (UNDERWEIGHT,), False),
        ("210", (UNDERWEIGHT, "chain_weight_above_maximum"), False),
    ),
)
def test_only_actual_underweight_with_explicit_permission_is_publishable_material(
    weight, allowed, passed
):
    candidate, problem, rules, runtime = audit_case(
        nodes=(node("real", weight=weight),),
        rule_items=(weight_rule(minimum="100", maximum="200"),),
    )
    rules = replace(rules, allowed_final_deviation_codes=frozenset(allowed))
    result = final_audit.audit_core_without_search_cache(candidate, problem, rules, runtime)
    assert result.report.status is CoreAuditStatus.COMPLETED
    assert result.report.passed is passed and result.report.search_evaluation_matches
    assert result.audited_evaluation is not None and result.resource_facts is not None
    assert (
        result.report.audited_split_count,
        result.report.audited_same_period_split_count,
        result.report.audited_future_borrow_return_count,
    ) == (0, 0, 0)
    if not passed:
        assert result.issues
    if weight == "70":
        assert result.audited_evaluation.metrics["underweight_chain_count"] == 1
        assert result.audited_evaluation.metrics["underweight_total_gap"] == D(30)


class FakeDeviationRule(Rule):
    supported_scope = RuleScope.CHAIN

    def required_fields(self):
        return ()

    def evaluate(self, subject, context):
        return RuleContribution(
            (
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    self.parameters["reason"],
                    "Fabricated deviation, not an actual chain-weight lower-bound result.",
                    RuleDisposition(self.parameters["disposition"]),
                    D(1),
                ),
            ),
            (),
        )


@pytest.mark.parametrize(
    "reason,disposition",
    (
        ("unapproved_deviation", RuleDisposition.ALLOWED_FINAL_DEVIATION),
        (UNDERWEIGHT, RuleDisposition.ALLOWED_FINAL_DEVIATION),
        (UNDERWEIGHT, RuleDisposition.PROHIBITED),
    ),
)
def test_custom_rules_cannot_impersonate_the_only_allowed_deviation(reason, disposition):
    fake = FakeDeviationRule(
        "weight",
        "fake",
        RuleScope.CHAIN,
        True,
        "1",
        {"reason": reason, "disposition": disposition.value},
    )
    candidate, problem, rules, runtime = audit_case(rule_items=(fake,))
    rules = replace(rules, allowed_final_deviation_codes=frozenset((reason,)))
    outcome = final_audit.audit_core_without_search_cache(candidate, problem, rules, runtime)
    assert outcome.report.status is CoreAuditStatus.COMPLETED
    assert not outcome.report.passed and outcome.report.search_evaluation_matches
    assert outcome.audited_evaluation is not None and outcome.resource_facts is not None
    assert outcome.issues


def test_report_fingerprint_binds_all_audit_materials_without_time_or_self_reference():
    candidate, problem, rules, runtime = audit_case()
    first = final_audit.audit_core_without_search_cache(candidate, problem, rules, runtime)
    report = first.report
    values = {
        field.name: getattr(report, field.name)
        for field in fields(report)
        if field.name != "report_fingerprint"
    }
    payload = dict(
        problem_fingerprint=problem.input_fingerprint,
        rule_set_fingerprint=rules.fingerprint,
        plan_fingerprint=fingerprint(candidate.plan),
        search_evaluation_fingerprint=fingerprint(candidate.search_evaluation),
        report=values,
        issues=first.issues,
    )
    assert report.report_fingerprint == fingerprint(payload)
    runtime.candidate_check_count = runtime.candidate_check_limit
    runtime.stop_reason = SearchStopReason.CANDIDATE_LIMIT_REACHED
    runtime.clock = lambda: runtime.search_deadline_monotonic
    second = final_audit.audit_core_without_search_cache(candidate, problem, rules, runtime)
    assert second.report.passed and second.report.report_fingerprint == report.report_fingerprint
    assert runtime.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert runtime.candidate_check_count == runtime.candidate_check_limit
    for key in payload:
        assert fingerprint(payload | {key: "modified"}) != report.report_fingerprint


@pytest.mark.parametrize("identity", ("problem", "rules"))
def test_report_identity_changes_when_an_upstream_fingerprint_changes(identity):
    candidate, problem, rules, runtime = audit_case()
    first = final_audit.audit_core_without_search_cache(candidate, problem, rules, runtime)
    if identity == "problem":
        problem = replace(problem, input_fingerprint="another-input-identity")
    else:
        rules = replace(rules, fingerprint="another-rule-identity")
    second = final_audit.audit_core_without_search_cache(candidate, problem, rules, runtime)
    assert second.report.passed
    assert second.resource_facts == first.resource_facts
    assert second.report.report_fingerprint != first.report.report_fingerprint


@pytest.mark.parametrize("point", ("facts", "resource_fingerprint", "report"))
@pytest.mark.parametrize("termination", ("cancel", "time"))
def test_cancellation_or_deadline_after_materials_discards_every_partial_output(
    point, termination, monkeypatch
):
    signal = SimpleNamespace(cancelled=False, expired=False)
    runtime = budget(cancellation=SimpleNamespace(is_cancelled=lambda: signal.cancelled))
    runtime.clock = lambda: runtime.final_deadline_monotonic if signal.expired else 0.0
    case = audit_case(runtime=runtime)
    reached = []

    def abort():
        reached.append(point)
        signal.cancelled = termination == "cancel"
        signal.expired = termination == "time"

    if point == "facts":
        original = final_audit._derive_audited_resource_facts

        def derive(*args):
            facts = original(*args)
            assert facts is not None
            abort()
            return facts

        monkeypatch.setattr(final_audit, "_derive_audited_resource_facts", derive)
    elif point == "resource_fingerprint":
        original = resource_facts.fingerprint

        def fingerprint_ready(value):
            result = original(value)
            abort()
            return result

        monkeypatch.setattr(resource_facts, "fingerprint", fingerprint_ready)
    else:
        original = CoreAuditReport.__post_init__

        def report_ready(report):
            original(report)
            if report.status is CoreAuditStatus.COMPLETED:
                abort()

        monkeypatch.setattr(CoreAuditReport, "__post_init__", report_ready)
    result = final_audit.audit_core_without_search_cache(*case)
    assert reached == [point]
    assert_unfinished(
        result, CoreAuditStatus.CANCELLED if termination == "cancel" else CoreAuditStatus.TIME_LIMIT
    )
    assert runtime.candidate_check_count == 0


def test_fact_derivation_failure_is_an_unfinished_error_not_empty_success(monkeypatch):
    case = audit_case()

    def fail(*args):
        raise RuntimeError("deliberate fact derivation failure")

    monkeypatch.setattr(final_audit, "_derive_audited_resource_facts", fail)
    assert_unfinished(final_audit.audit_core_without_search_cache(*case), CoreAuditStatus.ERROR)


def test_completed_outcome_keeps_the_single_derived_facts_object(monkeypatch):
    case = audit_case()
    original = final_audit._derive_audited_resource_facts
    generated = []

    def derive(*args):
        value = original(*args)
        generated.append(value)
        return value

    monkeypatch.setattr(final_audit, "_derive_audited_resource_facts", derive)
    result = final_audit.audit_core_without_search_cache(*case)
    assert result.report.passed and len(generated) == 1
    assert result.resource_facts is generated[0]
