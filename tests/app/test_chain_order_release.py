"""The public boundary preserves the audited production order and seventh objective."""

from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.api.request import PeriodInput, QualityCriterionSpec, RuleDefinitionSpec
from apsgo_scheduler.app import service
from apsgo_scheduler.app.contract_audit import audit_result_contract
from apsgo_scheduler.app.result_assembler import seal_scheduling_result
from apsgo_scheduler.core.contracts import SolveStatus, fingerprint, sum_decimals
from apsgo_scheduler.core.rules.base import PlanRuleSubject, RuleEvaluationContext
from tests.app.test_input_normalizer import make_order, make_request, make_spec
from tests.app.test_result_contract_audit import assert_failed
from tests.core.rules.test_inter_chain_width_gap import METRIC, with_gap
from tests.core.test_quality_key import ruleset

D = Decimal


def released_case(monkeypatch, *, cross_period=False):
    active = with_gap(ruleset())
    definitions = tuple(
        RuleDefinitionSpec(
            item.rule_id,
            type(item).__name__,
            item.name,
            item.scope,
            item.enabled,
            item.version,
            dict(item.parameters) | {"max_weight": D(1000), "target_weight": D(1000)}
            if item.rule_id == "weight"
            else item.parameters,
        )
        for item in active.rules
    )
    quality = tuple(
        QualityCriterionSpec(
            item.criterion_id,
            item.metric_key,
            item.direction.value,
            item.aggregation.value,
            item.numeric_projection.value,
        )
        for item in active.quality_spec
    )
    spec = make_spec(rules=definitions, quality_spec=quality)
    periods = ("Z-first", "M-empty", "A-last") if cross_period else ("P0",)
    request = make_request(
        rule_set_spec=spec,
        orders=tuple(
            make_order(
                index,
                width=D(width),
                weight=D(800),
                source_period=periods[-1] if cross_period and index == 1 else periods[0],
            )
            for index, width in enumerate((1000, 1500, 1200))
        ),
        periods=tuple(PeriodInput(period, index) for index, period in enumerate(periods)),
        virtual_prototypes=(),
    )
    captured = []

    def observe(*args):
        captured.append(args)
        return audit_result_contract(*args)

    with monkeypatch.context() as scope:
        scope.setattr(service, "audit_result_contract", observe)
        result = service.solve_request(request)
    assert result.status is SolveStatus.SUCCESS, result.issues
    assert result.core_audit.passed and result.audit_report.passed
    assert len(captured) == 1
    return result, captured[0], active.rules[-1]


@pytest.mark.parametrize("cross_period", (False, True))
def test_public_release_and_boundary_details_keep_the_same_audited_sequence(
    cross_period, monkeypatch
):
    result, args, rule = released_case(monkeypatch, cross_period=cross_period)
    draft, _, problem, core, _ = args
    release = result.release
    assert release.plan is core.release.canonical_plan is draft.proposed_release.plan
    assert release.evaluation is core.release.audited_evaluation
    assert release.resource_facts is core.release.resource_facts
    assert len(release.plan.chains) == 3
    ids = tuple(chain.chain_id for chain in release.plan.chains)
    assert tuple(item.chain_id for item in release.evaluation.chain_evaluations) == ids
    context = RuleEvaluationContext(
        problem.period_order, {p: i for i, p in enumerate(problem.period_order)}, ()
    )
    pairs = rule.boundary_contributions(PlanRuleSubject("plan", release.plan, None), context)
    assert tuple((left, right) for left, right, _ in pairs) == tuple(zip(ids, ids[1:]))
    assert len(pairs) == 2
    total = sum_decimals(item.value for _, _, item in pairs)
    assert total == release.evaluation.metrics[METRIC] == release.evaluation.quality_key[6]
    assert release.evaluation.quality_key[:6] == (0, D(0), 0, D(0), 3, D(0))
    if cross_period:
        assert tuple(chain.assigned_period for chain in release.plan.chains) == (
            "Z-first",
            "Z-first",
            "A-last",
        )


@pytest.mark.parametrize("change", ("equal_gap_order", "raw_metric", "quality"))
def test_resigned_public_draft_cannot_reorder_or_change_seventh_objective(change, monkeypatch):
    result, args, rule = released_case(monkeypatch)
    draft, request, problem, core, runtime = args
    original = fingerprint(draft)
    proposed = draft.proposed_release
    if change == "equal_gap_order":
        plan = replace(proposed.plan, chains=tuple(reversed(proposed.plan.chains)))
        context = RuleEvaluationContext(problem.period_order, {"P0": 0}, ())
        assert (
            sum_decimals(
                item.value
                for _, _, item in rule.boundary_contributions(
                    PlanRuleSubject("plan", plan, None), context
                )
            )
            == proposed.evaluation.metrics[METRIC]
        )
        changed = replace(proposed, plan=plan)
    else:
        evaluation = proposed.evaluation
        if change == "raw_metric":
            evaluation = replace(
                evaluation,
                metrics=dict(evaluation.metrics) | {METRIC: evaluation.metrics[METRIC] + D(1)},
            )
        else:
            evaluation = replace(
                evaluation,
                quality_key=(*evaluation.quality_key[:6], evaluation.quality_key[6] + D(1)),
            )
        changed = replace(proposed, evaluation=evaluation)
    altered = replace(draft, proposed_release=changed)
    assert altered.draft_fingerprint != draft.draft_fingerprint
    report = audit_result_contract(altered, request, problem, core, runtime)
    assert_failed(
        report,
        "plan_mapping_mismatch" if change == "equal_gap_order" else "evaluation_mapping_mismatch",
    )
    with pytest.raises(ValueError, match="audit"):
        seal_scheduling_result(altered, report, runtime)
    assert fingerprint(draft) == original and result.release.plan is proposed.plan
