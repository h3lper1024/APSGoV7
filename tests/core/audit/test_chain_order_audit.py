"""Bind enabled chain-order metrics to the same immutable, ordered final plan."""

from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from apsgo_scheduler.core import chain_order, final_audit
from apsgo_scheduler.core.contracts import (
    CoreAuditStatus,
    SearchStopReason,
    fingerprint,
    sum_weights,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain
from apsgo_scheduler.core.rules.base import PlanRuleSubject
from tests.core.audit.test_final_audit_without_cache import audit_case, evaluation_context
from tests.core.graph.test_construction_order import node
from tests.core.rules.test_inter_chain_width_gap import METRIC, rule, with_gap
from tests.core.test_quality_key import ruleset

D = Decimal


def ordered_case(mode="objective", *, widths=("1000", "1500", "1200"), periods=None):
    base = ruleset()
    active = (
        with_gap(base)
        if mode == "objective"
        else replace(
            base,
            rules=base.rules + (() if mode == "absent" else (rule(enabled=mode != "disabled"),)),
            fingerprint=f"chain-order-audit-{mode}",
        )
    )
    periods = ("period",) * len(widths) if periods is None else periods
    nodes = tuple(
        replace(node(f"node-{index}", width=width, weight="800"), source_period=period)
        for index, (width, period) in enumerate(zip(widths, periods))
    )
    chains = tuple(
        Chain(f"chain-{index}", (item,), period)
        for index, (item, period) in enumerate(zip(nodes, periods))
    )
    return audit_case(
        nodes=nodes,
        chains=chains,
        rule_set=active,
        period_order=tuple(dict.fromkeys(periods)),
    )


@pytest.mark.parametrize("mode", ("objective", "diagnostic", "disabled", "absent"))
def test_normal_audit_preserves_plan_and_evaluation_order_without_sorting(mode, monkeypatch):
    args = ordered_case(mode)
    candidate, _, _, runtime = args
    before = fingerprint(candidate)

    def forbidden(*_):
        raise AssertionError("final audit must not regroup or sort the candidate")

    monkeypatch.setattr(chain_order, "stable_group_plan", forbidden)
    outcome = final_audit.audit_core_without_search_cache(*args)
    assert outcome.report.status is CoreAuditStatus.COMPLETED and outcome.report.passed
    assert outcome.report.search_evaluation_matches
    assert outcome.audited_evaluation == candidate.search_evaluation
    assert tuple(item.chain_id for item in outcome.audited_evaluation.chain_evaluations) == tuple(
        chain.chain_id for chain in candidate.plan.chains
    )
    assert fingerprint(candidate) == before
    assert runtime.candidate_check_count == 0
    assert (METRIC in outcome.audited_evaluation.metrics) is (mode in {"objective", "diagnostic"})


@pytest.mark.parametrize("mode", ("objective", "diagnostic", "disabled", "absent"))
@pytest.mark.parametrize("changed_part", ("plan", "chain_evaluations"))
def test_equal_total_cannot_hide_order_mismatch_when_width_rule_is_enabled(mode, changed_part):
    candidate, problem, active, runtime = ordered_case(mode)
    if changed_part == "plan":
        changed = replace(
            candidate, plan=replace(candidate.plan, chains=candidate.plan.chains[::-1])
        )
    else:
        changed = replace(
            candidate,
            search_evaluation=replace(
                candidate.search_evaluation,
                chain_evaluations=candidate.search_evaluation.chain_evaluations[::-1],
            ),
        )
    before = fingerprint(changed)
    outcome = final_audit.audit_core_without_search_cache(changed, problem, active, runtime)
    assert outcome.report.status is CoreAuditStatus.COMPLETED
    if mode in {"objective", "diagnostic"}:
        assert not outcome.report.passed and outcome.report.search_evaluation_matches is False
        assert {item.code for item in outcome.issues} == {"search_evaluation_chain_order_mismatch"}
        assert outcome.audited_evaluation.metrics[METRIC] == D(800)
        assert outcome.audited_evaluation.quality_key == changed.search_evaluation.quality_key
    else:
        assert outcome.report.passed and outcome.report.search_evaluation_matches
    assert fingerprint(changed) == before


@pytest.mark.parametrize("part", ("raw_metric", "quality", "missing_metric"))
def test_seventh_metric_and_quality_are_compared_independently(part):
    candidate, problem, active, runtime = ordered_case()
    original = candidate.search_evaluation
    assert len(original.quality_key) == 7
    if part == "quality":
        changed = replace(original, quality_key=(*original.quality_key[:-1], D(0)))
        code = "search_evaluation_quality_mismatch"
    else:
        metrics = dict(original.metrics)
        if part == "missing_metric":
            del metrics[METRIC]
        else:
            metrics[METRIC] = D(0)
        changed = replace(original, metrics=metrics)
        code = "search_evaluation_metric_mismatch"
    outcome = final_audit.audit_core_without_search_cache(
        replace(candidate, search_evaluation=changed), problem, active, runtime
    )
    assert outcome.report.status is CoreAuditStatus.COMPLETED and not outcome.report.passed
    assert outcome.report.search_evaluation_matches is False
    assert {item.code for item in outcome.issues} == {code}
    assert outcome.audited_evaluation == original


@pytest.mark.parametrize("mode", ("objective", "diagnostic", "disabled", "absent"))
def test_period_order_failure_is_explicit_retained_and_never_repaired(mode):
    candidate, problem, active, runtime = ordered_case(
        mode, periods=("Z-first", "A-next", "B-last")
    )
    changed = replace(candidate, plan=replace(candidate.plan, chains=candidate.plan.chains[::-1]))
    before = fingerprint(changed)
    outcome = final_audit.audit_core_without_search_cache(changed, problem, active, runtime)
    if mode in {"objective", "diagnostic"}:
        assert outcome.report.status is CoreAuditStatus.ERROR and not outcome.report.passed
        assert "chain_period_order_invalid" in outcome.report.invariant_failure_codes
        issues = [item for item in outcome.issues if item.code == "chain_period_order_invalid"]
        assert [item.subject_id for item in issues] == ["chain-1", "chain-0"]
        assert all(item.field_path == "chains.assigned_period" for item in issues)
        assert "core_audit_error" in {item.code for item in outcome.issues}
        assert outcome.audited_evaluation is outcome.resource_facts is None
        assert runtime.stop_reason is SearchStopReason.SYSTEM_ERROR
    else:
        assert outcome.report.passed
        assert "chain_period_order_invalid" not in outcome.report.invariant_failure_codes
    assert fingerprint(changed) == before


@pytest.mark.parametrize(
    "widths", (("1000",), ("1000.00000000000000000001", "1000", "1000.00000000000000000003"))
)
def test_boundary_identities_and_exact_sum_come_from_the_same_audited_plan(widths):
    candidate, problem, active, runtime = ordered_case(widths=widths)
    before = fingerprint(candidate)
    with localcontext() as arithmetic:
        arithmetic.prec = 2
        outcome = final_audit.audit_core_without_search_cache(candidate, problem, active, runtime)
        boundaries = rule().boundary_contributions(
            PlanRuleSubject("plan", candidate.plan, None), evaluation_context(problem)
        )
        total = sum_weights(item.value for _, _, item in boundaries)
    assert outcome.report.passed
    ids = tuple(chain.chain_id for chain in candidate.plan.chains)
    assert tuple((left, right) for left, right, _ in boundaries) == tuple(zip(ids, ids[1:]))
    assert (
        total
        == outcome.audited_evaluation.metrics[METRIC]
        == outcome.audited_evaluation.quality_key[-1]
    )
    assert total == (D(0) if len(widths) == 1 else D("0.00000000000000000004"))
    assert outcome.audited_evaluation == evaluate_plan(
        candidate.plan, active, evaluation_context(problem)
    )
    assert fingerprint(candidate) == before
