"""One lexicographic comparison, generic declared metrics, no hidden borrowing penalty."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, Inexact, localcontext

import pytest

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    MetricContribution,
    NumericProjection,
    QualityAggregation,
    QualityCriterion,
    QualityDirection,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    RuleViolation,
)
from apsgo_scheduler.core.rules.concrete import ChainWeightRangeRule, HighSurfaceRunCountRule
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet

D = Decimal


def criterion(
    key,
    *,
    aggregation=QualityAggregation.NAMED_VALUE,
    projection=NumericProjection.EXACT_DECIMAL,
    direction=QualityDirection.MINIMIZE,
    identity=None,
):
    return QualityCriterion(identity or key, key, direction, aggregation, projection)


def ruleset(extra=(), criteria=None):
    rules = (
        ChainWeightRangeRule(
            "weight",
            "weight",
            RuleScope.CHAIN,
            True,
            "1",
            {
                "min_weight": D(700),
                "max_weight": D(2000),
                "target_weight": D(2000),
            },
        ),
    ) + extra
    first = (
        criterion("prohibited_violation_count"),
        criterion(
            "prohibited_violation_severity",
            projection=NumericProjection.REFERENCE_FLOAT_ROUND_6,
        ),
    )
    remaining = (
        (
            criterion("underweight_chain_count", aggregation=QualityAggregation.SUM),
            criterion(
                "underweight_total_gap",
                aggregation=QualityAggregation.SUM,
                projection=NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM,
            ),
            criterion("chain_count"),
            criterion(
                "generated_virtual_weight", projection=NumericProjection.REFERENCE_FLOAT_ROUND_6
            ),
        )
        if criteria is None
        else criteria
    )
    return ProcessRuleSet(
        "synthetic",
        "coating",
        "month",
        "1",
        rules,
        first + remaining,
        frozenset({"chain_weight_below_minimum"}),
        "config",
    )


def node(name, weight=800, *, source="first", virtual=False, attrs=None):
    return Node(
        name,
        None if virtual else name,
        None if virtual else name,
        None if virtual else source,
        D(weight),
        D(1000),
        D(1),
        None,
        None,
        "SPHC",
        MaterialRole.GENERATED_VIRTUAL if virtual else MaterialRole.NORMAL_REAL,
        attrs or {},
        VirtualLineage("p", VirtualPurpose.WEIGHT_FILL, None, 1) if virtual else None,
    )


def plan(*chains):
    return SchedulePlan(tuple(Chain(f"c{i}", nodes, "first") for i, nodes in enumerate(chains)))


def evaluate(current, rules=None):
    return evaluate_plan(
        current,
        rules or ruleset(),
        RuleEvaluationContext(("first", "later"), {"first": 0, "later": 1}, ("p",)),
    )


@dataclass(frozen=True, slots=True)
class SeverityRule(Rule):
    supported_scope = RuleScope.CHAIN

    def required_fields(self):
        return ()

    def evaluate(self, subject, context):
        return RuleContribution(
            tuple(
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    n.node_id,
                    "synthetic",
                    "synthetic",
                    RuleDisposition.PROHIBITED,
                    n.rule_attributes["severity"],
                )
                for n in subject.chain.nodes
                if "severity" in n.rule_attributes
            ),
            (),
        )


@pytest.mark.parametrize("level", range(6))
def test_all_six_priorities_decide_by_the_first_changed_component(level):
    active = ruleset((SeverityRule("severity", "severity", RuleScope.CHAIN, True, "1", {}),))
    cases = (
        (
            plan(
                (node("a", 400, attrs={"severity": D(1)}), node("b", 400, attrs={"severity": D(1)}))
            ),
            plan((node("a", 100, attrs={"severity": D(1000)}),)),
        ),
        (
            plan((node("a", attrs={"severity": D(5)}),)),
            plan((node("a", 100, attrs={"severity": D(4)}),)),
        ),
        (plan((node("a", 600),), (node("b", 600),)), plan((node("a", 100),))),
        (plan((node("a", 600),)), plan((node("a", 650),))),
        (plan((node("a", 700),), (node("b", 700),)), plan((node("a", 1400),))),
        (
            plan((node("a"), node("v", 20, virtual=True))),
            plan((node("a"), node("v", 10, virtual=True))),
        ),
    )
    before, candidate = (evaluate(p, active) for p in cases[level])
    assert len(before.quality_key) == len(candidate.quality_key) == 6
    assert before.quality_key[:level] == candidate.quality_key[:level]
    assert candidate.quality_key[level] < before.quality_key[level]
    assert candidate.quality_key < before.quality_key
    assert not before.quality_key < candidate.quality_key


def test_borrowing_is_reported_but_never_breaks_a_tie_or_rejects_improvement():
    a, b, c = node("a", 700), node("b", 100, source="later"), node("c", 800, source="later")
    borrowed = evaluate(
        SchedulePlan((Chain("early", (a, b), "first"), Chain("late", (c,), "later")))
    )
    local = evaluate(SchedulePlan((Chain("early", (a,), "first"), Chain("late", (b, c), "later"))))
    assert borrowed.metrics["borrowed_future_weight"] == 100
    assert local.metrics["borrowed_future_weight"] == 0
    assert borrowed.quality_key == local.quality_key
    assert not local.quality_key < borrowed.quality_key
    # The frozen reference seventh component WOULD prefer local; its removal is intentional.
    assert local.quality_key + (0,) < borrowed.quality_key + (100,)
    merged = evaluate(plan((a, b, c)))
    assert merged.quality_key < local.quality_key
    assert merged.metrics["borrowed_future_weight"] == 900 > local.metrics["borrowed_future_weight"]


def test_target_weight_is_diagnostic_and_cannot_break_a_tie():
    a, b = evaluate(plan((node("a", 700),))), evaluate(plan((node("a", 1500),)))
    assert a.metrics["chain_target_weight_deviation"] != b.metrics["chain_target_weight_deviation"]
    assert a.quality_key == b.quality_key
    assert not b.quality_key < a.quality_key


def test_named_value_sum_max_and_count_do_not_overwrite_raw_maximum():
    surface = HighSurfaceRunCountRule(
        "surface",
        "surface",
        RuleScope.CHAIN,
        True,
        "1",
        {
            "surface_grades": ("FC",),
            "max_run_count": 10,
        },
    )
    key = surface.metric_keys()[0]
    criteria = tuple(
        criterion(key, aggregation=agg, identity=agg.value)
        for agg in (
            QualityAggregation.NAMED_VALUE,
            QualityAggregation.SUM,
            QualityAggregation.MAXIMUM,
            QualityAggregation.COUNT,
        )
    )
    current = plan(
        *(
            tuple(node(f"{count}-{i}", 100, attrs={"surface_grade": "FC"}) for i in range(count))
            for count in (3, 5)
        )
    )
    result = evaluate(current, ruleset((surface,), criteria))
    assert result.metrics[key] == 5
    assert [chain.metrics[key] for chain in result.chain_evaluations] == [3, 5]
    assert result.quality_key[2:] == (5, 8, 5, 2)


@dataclass(frozen=True, slots=True)
class SignedMetricRule(Rule):
    supported_scope = RuleScope.NODE

    def required_fields(self):
        return ()

    def metric_keys(self):
        return ("signed",)

    def evaluate(self, subject, context):
        values = subject.node.rule_attributes
        return RuleContribution(
            (),
            (() if "metric" not in values else (MetricContribution("signed", values["metric"]),)),
        )


def test_signed_decimal_aggregation_and_maximizing_are_exact_under_hostile_context():
    active = ruleset(
        (SignedMetricRule("signed", "signed", RuleScope.NODE, True, "1", {}),),
        (criterion("signed", direction=QualityDirection.MAXIMIZE),),
    )
    current = plan(
        (
            node("a", attrs={"metric": D("12345.67890123456789012345")}),
            node("b", attrs={"metric": -12345}),
        )
    )
    baseline = evaluate(current, active)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_DOWN
        ctx.traps[Inexact] = True
        actual = evaluate(current, active)
    assert actual == baseline
    assert actual.metrics["signed"] == D("0.67890123456789012345")
    assert actual.quality_key[-1] == D("-0.67890123456789012345")


def test_missing_contributions_have_zero_quality_without_inventing_a_raw_metric():
    active = ruleset(
        (SignedMetricRule("signed", "signed", RuleScope.NODE, True, "1", {}),),
        tuple(
            criterion("signed", aggregation=agg, identity=agg.value) for agg in QualityAggregation
        ),
    )
    result = evaluate(plan((node("a"),)), active)
    assert "signed" not in result.metrics
    assert result.quality_key[2:] == (0, 0, 0, 0)


def test_more_than_one_rule_on_same_subject_counts_as_multiple_records():
    active = ruleset(
        (
            SeverityRule("one", "one", RuleScope.CHAIN, True, "1", {}),
            SeverityRule("two", "two", RuleScope.CHAIN, True, "1", {}),
        )
    )
    result = evaluate(plan((node("a", attrs={"severity": D("2.5")}),)), active)
    assert [v.subject_id for v in result.violations] == ["a", "a"]
    assert result.quality_key[:2] == (2, D(5))


def test_plain_integer_metrics_remain_integers_without_decimal_coercion():
    active = ruleset(
        (SignedMetricRule("signed", "signed", RuleScope.NODE, True, "1", {}),),
        (criterion("signed", aggregation=QualityAggregation.SUM),),
    )
    result = evaluate(
        plan((node("a", attrs={"metric": -3}), node("b", attrs={"metric": 8}))), active
    )
    assert result.metrics["signed"] == result.quality_key[-1] == 5
    assert type(result.quality_key[-1]) is int
