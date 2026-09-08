"""Complete evaluation retains chain detail without confusing runs across chains."""

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path

import pytest

from apsgo_scheduler.api.request import QualityCriterionSpec, RuleDefinitionSpec, RuleSetSpec
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core import evaluation
from apsgo_scheduler.core.contracts import RuleScope, fingerprint
from apsgo_scheduler.core.evaluation import (
    ChainEvaluation,
    PlanEvaluation,
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
    RuleDisposition,
    RuleEvaluationContext,
)
from apsgo_scheduler.core.rules.concrete import (
    ConsecutiveVirtualMaterialRule,
    ContinuousNarrowSteelWeightRule,
    HighSurfaceRunCountRule,
    SameSpecContinuousRealWeightRule,
    SyntheticNodePriorityRule,
    SyntheticWidthLimitRule,
)
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet

D = Decimal


def node(name, *, virtual=False, **changes):
    values = dict(
        node_id=name,
        source_order_id=name,
        source_resource_id=name,
        source_period="first",
        weight=D(10),
        width=D(1000),
        thickness=D("0.8"),
        min_temperature=D(600),
        max_temperature=D(680),
        grade="SPHC",
        material_role=MaterialRole.NORMAL_REAL,
        rule_attributes={"surface_grade": "FC", "grade_class": "IF钢", "hot_roll_grade": "SPHC"},
    )
    if virtual:
        values.update(
            source_order_id=None,
            source_resource_id=None,
            source_period=None,
            material_role=MaterialRole.GENERATED_VIRTUAL,
            virtual_lineage=VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, 1),
        )
    return Node(**(values | changes))


def context():
    return RuleEvaluationContext(("first", "later"), {"first": 0, "later": 1}, ("prototype",))


def ruleset(rules=()):
    criteria = tuple(
        QualityCriterion(
            key, key, QualityDirection.MINIMIZE, QualityAggregation.NAMED_VALUE, projection
        )
        for key, projection in (
            ("prohibited_violation_count", NumericProjection.EXACT_DECIMAL),
            ("prohibited_violation_severity", NumericProjection.REFERENCE_FLOAT_ROUND_6),
        )
    )
    return ProcessRuleSet(
        "synthetic", "coating", "month", "1", rules, criteria, frozenset(), "config"
    )


@pytest.fixture(scope="module")
def gqga4():
    raw = json.loads(
        (
            Path(__file__).parents[1]
            / "baselines/gqga4/gqga4_rule_set_spec_six_level_historical.json"
        ).read_text(),
        parse_float=D,
    )
    raw["rules"] = tuple(
        RuleDefinitionSpec(**(item | {"scope": RuleScope(item["scope"])})) for item in raw["rules"]
    )
    raw["quality_spec"] = tuple(QualityCriterionSpec(**item) for item in raw["quality_spec"])
    return load_rule_set(RuleSetSpec(**raw))


@pytest.mark.parametrize(
    "rule_type,parameters,key,scale,virtual_run",
    [
        (
            HighSurfaceRunCountRule,
            {"surface_grades": ("FC", "FD"), "max_run_count": 5},
            "max_high_surface_run_count",
            1,
            False,
        ),
        (
            ContinuousNarrowSteelWeightRule,
            {"grade_class": "IF钢", "width_upper_exclusive": D(1400), "max_real_weight": D(500)},
            "max_if_narrow_real_run_weight",
            10,
            False,
        ),
        (
            SameSpecContinuousRealWeightRule,
            {"group_by_fields": ("width", "thickness", "grade"), "max_real_weight": D(1000)},
            "max_same_spec_real_run_weight",
            10,
            False,
        ),
        (ConsecutiveVirtualMaterialRule, {"max_count": 2}, "max_consecutive_virtual_sphc", 1, True),
    ],
)
def test_each_chain_retains_its_value_while_global_takes_maximum(
    rule_type, parameters, key, scale, virtual_run
):
    rule = rule_type("run", "run", RuleScope.CHAIN, True, "1", parameters)
    # The actual declared key is authoritative; the explicit names guard accidental renaming.
    assert rule.metric_keys() == (key,)
    chains = tuple(
        Chain(
            str(count),
            ((node(f"anchor-{count}"),) if virtual_run else ())
            + tuple(node(f"{count}-{i}", virtual=virtual_run) for i in range(count)),
            "first",
        )
        for count in (3, 5)
    )
    plan = SchedulePlan(chains)
    result = evaluate_plan(plan, ruleset((rule,)), context())
    assert [chain.metrics[key] for chain in result.chain_evaluations] == [3 * scale, 5 * scale]
    assert result.metrics[key] == 5 * scale
    assert result.metrics[key] != 8 * scale
    for chain, detail in zip(chains, result.chain_evaluations):
        assert detail == evaluate_chain(chain, ruleset((rule,)), context())


def test_missing_surface_breaks_the_run_and_disabled_rule_has_no_metric():
    rule = HighSurfaceRunCountRule(
        "surface",
        "surface",
        RuleScope.CHAIN,
        True,
        "1",
        {"surface_grades": ("FC",), "max_run_count": 1},
    )
    plan = SchedulePlan(
        (
            Chain(
                "chain",
                (
                    node("a"),
                    node("empty", rule_attributes={}),
                    node("b"),
                ),
                "first",
            ),
        )
    )
    result = evaluate_plan(plan, ruleset((rule,)), context())
    assert result.metrics["max_high_surface_run_count"] == 1
    assert result.violations == ()
    disabled = evaluate_plan(plan, ruleset((replace(rule, enabled=False),)), context())
    assert "max_high_surface_run_count" not in disabled.metrics
    assert "max_high_surface_run_count" not in disabled.chain_evaluations[0].metrics


def test_two_synthetic_lines_use_the_same_evaluator_with_different_rules():
    plan = SchedulePlan((Chain("chain", (node("a"), node("b", width=D(1015))), "first"),))
    strict = SyntheticWidthLimitRule(
        "width", "width", RuleScope.EDGE, True, "1", {"maximum_increase": D(10)}
    )
    relaxed = replace(strict, parameters={"maximum_increase": D(20)})
    a = evaluate_plan(plan, ruleset((strict,)), context())
    b = evaluate_plan(plan, ruleset((relaxed,)), context())
    assert a.metrics["prohibited_violation_count"] == 1
    assert b.metrics["prohibited_violation_count"] == 0
    assert a.metrics["synthetic_width_increase"] == b.metrics["synthetic_width_increase"] == 15


def test_full_gqga4_keeps_chain_edge_plan_rules_and_internal_virtual_anchor(gqga4):
    nodes = (
        node("a", weight=D(1000)),
        node("v1", virtual=True, width=D(1100), weight=D(20)),
        node("v2", virtual=True, width=D(1050), weight=D(20)),
        node("v3", virtual=True, width=D(1040), weight=D(20)),
    ) + tuple(node(f"b{i}", width=D(1030), weight=D(200)) for i in range(6))
    plan = SchedulePlan((Chain("chain", nodes, "first"),))
    before = fingerprint((plan, gqga4, context()))
    result = evaluate_plan(plan, gqga4, context())
    assert [v.rule_id for v in result.violations] == [
        "chain_weight_range",
        "max_reverse_width_count",
        "max_consecutive_virtual_sphc",
        "reverse_width_limit",
        "chain_high_surface_run_count_lte",
        "chain_if_narrow_real_weight_lte",
        "chain_if_narrow_real_weight_lte",
        "chain_same_spec_real_weight_lte",
    ]
    assert result.violations[3].reason_code == "virtual_bridge_reverse_width_exceeded"
    assert result.metrics["prohibited_violation_count"] == len(result.violations)
    assert len(result.quality_key) == 6
    assert result.metrics["strategic_customer_rank"] == len(nodes)
    assert result.metrics["generated_virtual_weight"] == 60
    assert result.metrics["borrowed_future_weight"] == 0
    assert fingerprint((plan, gqga4, context())) == before
    quick = quick_chain_prohibited_profile(plan.chains[0], gqga4, context())
    assert quick == result.quality_key[:2]
    summary = result.chain_evaluations[0].summary
    assert (summary.real_weight, summary.virtual_weight, summary.total_weight) == (2200, 60, 2260)
    assert (summary.real_node_count, summary.virtual_node_count, summary.split_piece_count) == (
        7,
        3,
        0,
    )


def test_full_plan_does_not_reassign_period_and_separates_allowed_from_prohibited(gqga4):
    plan = SchedulePlan((Chain("late", (node("a", weight=D(600)),), "later"),))
    before = fingerprint(plan)
    result = evaluate_plan(plan, gqga4, context())
    assert plan.chains[0].assigned_period == "later" and fingerprint(plan) == before
    assert result.quality_key == (2, D(101), 1, D(100), 1, D(0))
    # A 600 t IF-narrow run is prohibited; a 600 t chain is an allowed weight deviation.
    assert [v.disposition for v in result.violations] == [
        RuleDisposition.ALLOWED_FINAL_DEVIATION,
        RuleDisposition.PROHIBITED,
        RuleDisposition.PROHIBITED,
    ]
    assert result.metrics["underweight_total_gap"] == 100
    assert result.metrics["late_original_due_period_move_count"] == 1
    assert result.metrics["future_fill_total_gap"] == 600


def test_full_evaluation_does_not_restore_removed_reverse_carrier_grade_filter(gqga4):
    attributes = {"hot_roll_grade": "H260Y", "soft_hard_class": "软钢", "grade_class": "普碳"}
    plan = SchedulePlan(
        (
            Chain(
                "c",
                (
                    node("a", weight=D(400), rule_attributes=attributes),
                    node("b", weight=D(400), width=D(1010), rule_attributes=attributes),
                ),
                "first",
            ),
        )
    )
    result = evaluate_plan(plan, gqga4, context())
    assert result.quality_key == (0, D(0), 0, D(0), 1, D(0))
    assert result.violations == ()
    assert quick_chain_prohibited_profile(plan.chains[0], gqga4, context()) == (0, D(0))


def test_evaluation_carriers_copy_and_freeze_collections():
    detail = evaluate_chain(Chain("c", (node("a"),), "first"), ruleset(), context())
    metrics, quality, chains, violations = {"raw": D("0.123456789")}, [0, D(0)], [detail], []
    result = PlanEvaluation(chains, violations, metrics, quality)
    metrics.clear()
    quality.clear()
    chains.clear()
    violations.clear()
    assert result.metrics["raw"] == D("0.123456789") and result.chain_evaluations == (detail,)
    with pytest.raises(TypeError):
        result.metrics["raw"] = 0
    with pytest.raises(FrozenInstanceError):
        result.quality_key = ()
    with pytest.raises(TypeError):
        detail.metrics["x"] = 1
    with pytest.raises(FrozenInstanceError):
        detail.summary.real_weight = D(0)


@pytest.mark.parametrize("value", [True, 1.0, None, "1", D("NaN"), D("Infinity"), []])
def test_metric_and_quality_values_must_be_finite_numbers(value):
    with pytest.raises(ValueError):
        PlanEvaluation((), (), {"bad": value}, ())
    with pytest.raises(ValueError):
        PlanEvaluation((), (), {}, (value,))


def test_evaluation_rejects_invalid_subjects_and_duplicate_chain_details():
    plan = SchedulePlan((Chain("c", (node("a"),), "first"),))
    for args in ((None, ruleset(), context()), (plan, None, context()), (plan, ruleset(), None)):
        with pytest.raises(ValueError):
            evaluate_plan(*args)
    detail = evaluate_chain(plan.chains[0], ruleset(), context())
    with pytest.raises(ValueError):
        PlanEvaluation((detail, detail), (), {}, ())
    with pytest.raises(ValueError):
        ChainEvaluation("c", None, (), {})


@pytest.mark.parametrize("maximum_increase", (20, 0))
def test_captured_entries_keep_raw_metrics_and_ordered_node_contributions(maximum_increase):
    width = SyntheticWidthLimitRule(
        "width", "width", RuleScope.EDGE, True, "1", {"maximum_increase": D(maximum_increase)}
    )
    priority = SyntheticNodePriorityRule(
        "priority", "priority", RuleScope.NODE, True, "1", {"attribute": "priority"}
    )
    active = ruleset((width, priority))
    active = replace(
        active,
        quality_spec=active.quality_spec
        + tuple(
            QualityCriterion(
                aggregation.value,
                "synthetic_width_increase",
                QualityDirection.MINIMIZE,
                aggregation,
                NumericProjection.EXACT_DECIMAL,
            )
            for aggregation in (
                QualityAggregation.SUM,
                QualityAggregation.MAXIMUM,
                QualityAggregation.COUNT,
            )
        ),
    )
    plan = SchedulePlan(
        tuple(
            Chain(
                f"c-{i}",
                tuple(
                    node(f"n-{i}-{j}", width=D(value), rule_attributes={"priority": i * 3 + j})
                    for j, value in enumerate(widths)
                ),
                "first",
            )
            for i, widths in enumerate(((1000, 1003, 1008), (1000, 1007, 1018)))
        )
    )
    ctx = context()
    expected = evaluate_plan(plan, active, ctx)
    actual, entries = evaluation._evaluate_plan(plan, active, ctx, previous_entries={})
    assert actual == expected and fingerprint(actual) == fingerprint(expected)
    assert actual.quality_key[2:] == (D(26), D(11), 4)
    assert [item.metrics["synthetic_width_increase"] for item in actual.chain_evaluations] == [
        D(8), D(18)
    ]
    assert tuple(entries) == ("c-0", "c-1")
    assert tuple(
        violation for entry in entries.values() for violation in entry.contribution.violations
    ) == actual.violations
    assert tuple(
        metric.value for entry in entries.values() for metric in entry.contribution.metrics
    ) == (D(3), D(5), D(7), D(11))
    assert tuple(
        metric.value
        for entry in entries.values()
        for contribution in entry.node_contributions
        for metric in contribution.metrics
    ) == tuple(range(6))
    for chain, detail in zip(plan.chains, actual.chain_evaluations):
        entry = entries[chain.chain_id]
        assert entry.chain is chain and entry.evaluation is detail
        assert isinstance(entry.node_contributions, tuple)
        assert len(entry.node_contributions) == len(chain.nodes)
        with pytest.raises(FrozenInstanceError):
            entry.chain = chain


def test_uncached_plan_evaluation_does_not_create_reuse_entries(monkeypatch):
    plan = SchedulePlan((Chain("c", (node("a"),), "first"),))
    active, ctx = ruleset(), context()
    expected = evaluate_plan(plan, active, ctx)

    def forbidden(*args, **kwargs):
        pytest.fail("public uncached evaluation must not allocate candidate entries")

    monkeypatch.setattr(evaluation, "_ChainEvaluationEntry", forbidden)
    assert evaluate_plan(plan, active, ctx) == expected
    actual, entries = evaluation._evaluate_plan(plan, active, ctx)
    assert actual == expected and entries is None


@pytest.mark.parametrize("capture", (False, True))
@pytest.mark.parametrize("failure", (None, ("chain", "b"), ("node", "a1")))
def test_shared_evaluation_preserves_all_chain_then_all_node_execution_order(
    monkeypatch, capture, failure
):
    plan = SchedulePlan(
        (Chain("a", (node("a1"), node("a2")), "first"), Chain("b", (node("b1"),), "first"))
    )
    active, ctx = ruleset(), context()
    expected = evaluate_plan(plan, active, ctx)
    seen = []
    error = RuntimeError("ordered rule execution failure")
    original_resources = evaluation.derive_evaluation_resource_view

    def resources(*args):
        seen.append(("resources", "plan"))
        return original_resources(*args)

    monkeypatch.setattr(evaluation, "derive_evaluation_resource_view", resources)
    for name, method in (
        ("chain", "evaluate_complete_chain"),
        ("node", "evaluate_node"),
        ("plan", "evaluate_plan"),
    ):
        original = getattr(ProcessRuleSet, method)

        def observe(self, subject, context, name=name, original=original):
            event = (name, subject.subject_id)
            seen.append(event)
            if event == failure:
                raise error
            return original(self, subject, context)

        monkeypatch.setattr(ProcessRuleSet, method, observe)

    def run():
        if capture:
            return evaluation._evaluate_plan(plan, active, ctx, previous_entries={})[0]
        return evaluate_plan(plan, active, ctx)

    events = [
        ("resources", "plan"), ("chain", "a"), ("chain", "b"),
        ("node", "a1"), ("node", "a2"), ("node", "b1"), ("plan", "plan"),
    ]
    if failure is None:
        actual = run()
        assert actual == expected and fingerprint(actual) == fingerprint(expected)
        assert seen == events
    else:
        with pytest.raises(RuntimeError) as raised:
            run()
        assert raised.value is error
        assert seen == events[: events.index(failure) + 1]
