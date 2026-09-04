"""Numerical goldens: reference evaluation, not a scheduling benchmark.

The source with SHA-256 87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318
emits chain/edge failures at solver.py:919-1149 and projects quality at 1389-1408.
Goldens are self-contained; this suite never imports the external reference file.
Two-place underweight scoring is the approved difference from its six-place score.
"""

from dataclasses import replace
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_UP,
    Decimal,
    Inexact,
    localcontext,
)

import pytest

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.evaluation import (
    evaluate_chain,
    evaluate_plan,
    quick_chain_prohibited_profile,
)
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
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
from apsgo_scheduler.core.rules.concrete import (
    ChainWeightRangeRule,
    HighSurfaceRunCountRule,
    SoftHardConnectionRule,
)
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet

D = Decimal
PERIOD = "period"
CONTEXT = RuleEvaluationContext((PERIOD,), {PERIOD: 0}, ("prototype",))


def node(name, weight="1000", *, virtual=False, attributes=None):
    return Node(
        name,
        None if virtual else name,
        None if virtual else name,
        None if virtual else PERIOD,
        D(weight),
        None,
        None,
        None,
        None,
        "steel",
        MaterialRole.GENERATED_VIRTUAL if virtual else MaterialRole.NORMAL_REAL,
        attributes or {},
        VirtualLineage("prototype", VirtualPurpose.WEIGHT_FILL, None, 1) if virtual else None,
    )


def plan(*weights):
    return SchedulePlan(
        tuple(
            Chain(f"c-{index}", (node(f"n-{index}", weight),), PERIOD)
            for index, weight in enumerate(weights)
        )
    )


def criterion(
    key, aggregation=QualityAggregation.NAMED_VALUE, projection=NumericProjection.EXACT_DECIMAL
):
    return QualityCriterion(key, key, QualityDirection.MINIMIZE, aggregation, projection)


def rule_set(rules=(), *, extras=(), severity_projection=NumericProjection.REFERENCE_FLOAT_ROUND_6):
    return ProcessRuleSet(
        "synthetic",
        "coating",
        "month",
        "1",
        tuple(rules),
        (
            criterion("prohibited_violation_count"),
            criterion("prohibited_violation_severity", QualityAggregation.SUM, severity_projection),
            *extras,
        ),
        frozenset({"chain_weight_below_minimum"}),
        "verified-numerical-fixture",
    )


def chain_weight_rule(minimum="700", maximum="2000"):
    return ChainWeightRangeRule(
        "chain_weight_range",
        "chain weight",
        RuleScope.CHAIN,
        True,
        "1",
        {"min_weight": D(minimum), "max_weight": D(maximum), "target_weight": D(maximum)},
    )


def six_level_rules():
    return rule_set(
        (chain_weight_rule(),),
        extras=(
            criterion("underweight_chain_count", QualityAggregation.SUM),
            criterion(
                "underweight_total_gap",
                QualityAggregation.SUM,
                NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM,
            ),
            criterion("chain_count"),
            criterion(
                "generated_virtual_weight", projection=NumericProjection.REFERENCE_FLOAT_ROUND_6
            ),
        ),
    )


class NumericalSeverityRule(Rule):
    """Direct test rule isolates projection from physical severity formulas."""

    supported_scope = RuleScope.CHAIN

    def required_fields(self):
        return ()

    def evaluate(self, subject, context):
        disposition = (
            RuleDisposition.ALLOWED_FINAL_DEVIATION
            if self.parameters.get("allowed", False)
            else RuleDisposition.PROHIBITED
        )
        return RuleContribution(
            tuple(
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "numerical",
                    "numerical fixture",
                    disposition,
                    value,
                )
                for value in self.parameters["severities"]
            ),
            (),
        )


def severity_rule(*values, allowed=False):
    return NumericalSeverityRule(
        "severity",
        "severity",
        RuleScope.CHAIN,
        True,
        "1",
        {"severities": tuple(D(value) for value in values), "allowed": allowed},
    )


def test_actual_rules_preserve_chain_edge_interleaving_for_float_severity():
    """Reference evaluates prefix weight, both edges, then high-surface segment."""
    weight = chain_weight_rule("0", "20000000000000000")
    edge = SoftHardConnectionRule(
        "gqga4_soft_hard_connection",
        "soft hard",
        RuleScope.EDGE,
        True,
        "1",
        {
            "virtual_sphc_allows_bridge": True,
            "transition_material_breaks_soft_hard": True,
            "missing_grade_policy": "deny",
        },
    )
    high = HighSurfaceRunCountRule(
        "chain_high_surface_run_count_lte",
        "high surface",
        RuleScope.CHAIN,
        True,
        "1",
        {"surface_grades": ("FC",), "max_run_count": 1},
    )
    chain = Chain(
        "c",
        tuple(
            node(
                f"n-{i}",
                "10000000000000000",
                attributes={"surface_grade": "FC", "soft_hard_class": classification},
            )
            for i, classification in enumerate(("soft", "hard", "soft"))
        ),
        PERIOD,
    )
    rules = rule_set((weight, edge, high))
    result = evaluate_plan(SchedulePlan((chain,)), rules, CONTEXT)
    assert tuple(v.rule_id for v in result.violations) == (
        weight.rule_id,
        edge.rule_id,
        edge.rule_id,
        high.rule_id,
    )
    assert tuple(v.severity for v in result.violations) == (D("1e16"), D(1), D(1), D(2))
    assert result.metrics["prohibited_violation_severity"] == D("10000000000000004")
    assert result.quality_key == (4, D("10000000000000002"))
    assert evaluate_chain(chain, rules, CONTEXT).violations == result.violations
    assert quick_chain_prohibited_profile(chain, rules, CONTEXT) == (4, 10000000000000002)
    # This is what the forbidden all-chain-then-all-edge order would produce.
    assert round(sum((1e16, 2.0, 1.0, 1.0)), 6) == 10000000000000004


@pytest.mark.parametrize(
    "raw,projected",
    [
        ("0.0000004", "0"),
        ("0.0000005", "0"),
        ("0.0000015", "0.000002"),
        ("0.0000025", "0.000003"),
        ("1.2345665", "1.234566"),
        ("1.2345675", "1.234568"),
        ("1.2345685", "1.234568"),
    ],
)
def test_six_place_projection_matches_binary_float_rounding_without_changing_raw(raw, projected):
    result = evaluate_plan(plan("1000"), rule_set((severity_rule(raw),)), CONTEXT)
    assert result.metrics["prohibited_violation_severity"] == D(raw)
    assert result.violations[0].severity == D(raw)
    assert result.quality_key == (1, D(projected))
    assert type(result.quality_key[0]) is int
    assert isinstance(result.quality_key[1], Decimal)


def test_raw_exact_severity_is_distinct_from_ordered_float_projection():
    rule = severity_rule("1e16", "1", "1")
    result = evaluate_plan(plan("1000"), rule_set((rule,)), CONTEXT)
    assert result.metrics["prohibited_violation_severity"] == D("10000000000000002")
    assert result.quality_key == (3, D("10000000000000000"))
    exact = evaluate_plan(
        plan("1000"),
        rule_set((rule,), severity_projection=NumericProjection.EXACT_DECIMAL),
        CONTEXT,
    )
    assert exact.quality_key == (3, D("10000000000000002"))


@pytest.mark.parametrize(
    "weight,gap,count,score",
    [
        ("699.995", "0.005", 1, "0.00"),
        ("699.985", "0.015", 1, "0.02"),
        ("699.975", "0.025", 1, "0.02"),
        ("690.001", "9.999", 1, "10.00"),
        ("600.001", "99.999", 1, "100.00"),
        ("0.001", "699.999", 1, "700.00"),
        ("699.99999851", "0.00000149", 1, "0.00"),
        ("699.999999", "0", 0, "0"),
        ("699.9999991", "0", 0, "0"),
        ("700", "0", 0, "0"),
        ("701", "0", 0, "0"),
    ],
)
def test_true_underweight_keeps_exact_diagnostics_with_two_place_score(weight, gap, count, score):
    result = evaluate_plan(plan(weight), six_level_rules(), CONTEXT)
    assert result.metrics["underweight_total_gap"] == D(gap)
    assert result.chain_evaluations[0].metrics["underweight_total_gap"] == D(gap)
    assert result.quality_key == (0, D(0), count, D(score), 1, D(0))
    assert tuple(type(result.quality_key[index]) for index in (0, 2, 4)) == (int, int, int)
    assert len(result.violations) == count
    if count:
        assert result.violations[0].severity == D(gap)
        assert result.violations[0].disposition is RuleDisposition.ALLOWED_FINAL_DEVIATION


def test_underweight_rounds_each_chain_before_summing_and_keeps_both_raw_values():
    result = evaluate_plan(plan("699.9851", "699.9851"), six_level_rules(), CONTEXT)
    assert tuple(e.metrics["underweight_total_gap"] for e in result.chain_evaluations) == (
        D("0.0149"),
        D("0.0149"),
    )
    assert result.metrics["underweight_total_gap"] == D("0.0298")
    assert result.quality_key == (0, D(0), 2, D("0.02"), 2, D(0))
    assert result.quality_key[3] != D("0.03")
    # Historical reference fourth item is 0.0298, not the new two-place value.
    assert result.quality_key[3] != D("0.0298")


def test_two_place_underweight_can_turn_raw_improvement_into_equal_quality():
    current = evaluate_plan(plan("699.9851"), six_level_rules(), CONTEXT)
    candidate = evaluate_plan(plan("699.9852"), six_level_rules(), CONTEXT)
    assert candidate.metrics["underweight_total_gap"] < current.metrics["underweight_total_gap"]
    assert candidate.quality_key == current.quality_key
    assert not candidate.quality_key < current.quality_key


@pytest.mark.parametrize(
    "precision,rounding", [(2, ROUND_UP), (6, ROUND_DOWN), (50, ROUND_HALF_EVEN)]
)
def test_projection_and_exact_statistics_ignore_ambient_decimal_context(precision, rounding):
    current, rules = plan("699.9851", "699.9851"), six_level_rules()
    expected = evaluate_plan(current, rules, CONTEXT)
    severity = rule_set((severity_rule("1e16", "1", "1"),))
    severity_plan = plan("1000")
    expected_severity = evaluate_plan(severity_plan, severity, CONTEXT)
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.rounding = rounding
        ctx.traps[Inexact] = True
        assert evaluate_plan(current, rules, CONTEXT) == expected
        assert evaluate_plan(severity_plan, severity, CONTEXT) == expected_severity


def test_virtual_weight_is_summed_exactly_before_its_single_float_projection():
    chain = Chain(
        "c",
        (
            node("r", "1000"),
            node("v0", "1e16", virtual=True),
            node("v1", "1", virtual=True),
            node("v2", "1", virtual=True),
        ),
        PERIOD,
    )
    rules = rule_set(
        extras=(
            criterion(
                "generated_virtual_weight",
                projection=NumericProjection.REFERENCE_FLOAT_ROUND_6,
            ),
        )
    )
    result = evaluate_plan(SchedulePlan((chain,)), rules, CONTEXT)
    assert result.metrics["generated_virtual_weight"] == D("10000000000000002")
    assert result.quality_key == (0, D(0), D("10000000000000002"))
    assert sum((1e16, 1.0, 1.0)) == 10000000000000000


@pytest.mark.parametrize("values", [("1e1000",), ("1e308", "1e308")])
def test_nonfinite_float_severity_projection_is_rejected(values):
    with pytest.raises(ValueError):
        evaluate_plan(plan("1000"), rule_set((severity_rule(*values),)), CONTEXT)


def test_nonfinite_virtual_weight_projection_is_rejected():
    current = SchedulePlan((Chain("c", (node("r"), node("v", "1e1000", virtual=True)), PERIOD),))
    rules = rule_set(
        extras=(
            criterion(
                "generated_virtual_weight",
                projection=NumericProjection.REFERENCE_FLOAT_ROUND_6,
            ),
        )
    )
    with pytest.raises(ValueError):
        evaluate_plan(current, rules, CONTEXT)


def test_finite_exact_only_large_severity_needs_no_float_projection():
    result = evaluate_plan(
        plan("1000"),
        rule_set((severity_rule("1e1000"),), severity_projection=NumericProjection.EXACT_DECIMAL),
        CONTEXT,
    )
    assert result.quality_key == (1, D("1e1000"))


def test_allowed_deviation_is_retained_but_not_projected_as_prohibited():
    result = evaluate_plan(
        plan("1000"), rule_set((severity_rule("1e1000", allowed=True),)), CONTEXT
    )
    assert result.quality_key == (0, D(0))
    assert result.metrics["prohibited_violation_severity"] == 0
    assert result.violations[0].severity == D("1e1000")


def test_multiple_contributions_with_same_subject_are_not_deduplicated():
    rule = severity_rule("1", "2", "3")
    result = evaluate_plan(plan("1000"), rule_set((rule,)), CONTEXT)
    assert len({v.subject_id for v in result.violations}) == 1
    assert result.quality_key == (3, D(6))
    assert len(result.violations) == 3


@pytest.mark.parametrize("values,expected", [((), (0, 0, 0, 0)), (("1", "2", "3"), (6, 6, 3, 3))])
def test_severity_aggregations_read_original_records_not_the_total(values, expected):
    aggregations = (
        QualityAggregation.NAMED_VALUE,
        QualityAggregation.SUM,
        QualityAggregation.MAXIMUM,
        QualityAggregation.COUNT,
    )
    extras = tuple(
        replace(
            criterion("prohibited_violation_severity", aggregation),
            criterion_id=f"severity-{aggregation.value}",
        )
        for aggregation in aggregations
    )
    result = evaluate_plan(
        plan("1000"), rule_set((severity_rule(*values),), extras=extras), CONTEXT
    )
    assert result.quality_key[2:] == expected
    assert result.metrics["prohibited_violation_severity"] == expected[0]


@pytest.mark.parametrize("values", [(), ("1",), ("1", "2", "3")])
@pytest.mark.parametrize("aggregation", [QualityAggregation.COUNT, QualityAggregation.SUM])
def test_first_prohibited_count_criterion_counts_each_record_including_empty(values, aggregation):
    rules = rule_set((severity_rule(*values),))
    rules = replace(
        rules,
        quality_spec=(
            replace(rules.quality_spec[0], aggregation=aggregation),
            rules.quality_spec[1],
        ),
    )
    result = evaluate_plan(plan("1000"), rules, CONTEXT)
    assert result.quality_key[0] == len(values)
    assert type(result.quality_key[0]) is int
    assert result.metrics["prohibited_violation_count"] == len(values)


def test_chain_count_uses_per_chain_unit_contributions_without_changing_report():
    extras = tuple(
        replace(criterion("chain_count", aggregation), criterion_id=f"chains-{aggregation.value}")
        for aggregation in (
            QualityAggregation.NAMED_VALUE,
            QualityAggregation.SUM,
            QualityAggregation.MAXIMUM,
            QualityAggregation.COUNT,
        )
    )
    result = evaluate_plan(plan("1000", "1000"), rule_set(extras=extras), CONTEXT)
    assert result.quality_key[2:] == (2, 2, 1, 2)
    assert result.metrics["chain_count"] == 2


def test_float_projection_does_not_rewrite_rule_contributions_or_plan():
    current = plan("1000")
    rule = severity_rule("1.2345675")
    changed = replace(rule, parameters={"severities": (D("1.2345674"),)})
    first = evaluate_plan(current, rule_set((rule,)), CONTEXT)
    second = evaluate_plan(current, rule_set((changed,)), CONTEXT)
    assert first.violations[0].severity == D("1.2345675")
    assert second.violations[0].severity == D("1.2345674")
    assert current.chains[0].nodes[0].weight == D("1000")
    assert first == evaluate_plan(current, rule_set((rule,)), CONTEXT)
