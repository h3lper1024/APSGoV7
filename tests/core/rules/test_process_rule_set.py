"""Frozen, ordered rule dispatch; no search or full-plan scoring is implemented here."""

from dataclasses import FrozenInstanceError, dataclass, replace
from decimal import Decimal

import pytest

from apsgo_scheduler.api.request import QualityCriterionSpec, RuleDefinitionSpec, RuleSetSpec
from apsgo_scheduler.app.rule_set_loader import (
    RULE_REGISTRY,
    RuleSetLoadError,
    fingerprint_rule_set_spec,
    load_rule_set,
)
from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    ControlledSplitRuleSubject,
    EdgeRuleSubject,
    MetricContribution,
    NodeRuleSubject,
    NumericProjection,
    PlanRuleSubject,
    QualityAggregation,
    QualityCriterion,
    QualityDirection,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    RuleViolation,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import (
    ChainWeightRangeRule,
    ConsecutiveVirtualMaterialRule,
    ContinuousNarrowSteelWeightRule,
    ControlledOrderSplitRule,
    HighSurfaceRunCountRule,
    SameSpecContinuousRealWeightRule,
    SyntheticNodePriorityRule,
    SyntheticWidthLimitRule,
    WidthTransitionRule,
)
from apsgo_scheduler.core.rules.helpers import create_controlled_split_decision
from apsgo_scheduler.core.rules.rule_set import PROHIBITED_METRIC_KEYS, ProcessRuleSet


def node(name="a", width="1000", priority=1):
    return Node(
        name,
        name,
        name,
        "later",
        Decimal("20"),
        Decimal(width),
        None,
        None,
        None,
        "steel",
        MaterialRole.NORMAL_REAL,
        {"priority": priority},
    )


def context(order=("earlier", "later")):
    return RuleEvaluationContext(order, dict(zip(order, range(len(order)))), ())


def criterion(key, *, direction=QualityDirection.MINIMIZE):
    return QualityCriterion(
        key,
        key,
        direction,
        QualityAggregation.NAMED_VALUE,
        NumericProjection.EXACT_DECIMAL,
    )


def rule_set(rules=(), *, soft_keys=(), **changes):
    values = dict(
        product_line_code="synthetic",
        process_code="coating",
        scenario="month",
        version="1",
        rules=rules,
        quality_spec=tuple(criterion(key) for key in (*PROHIBITED_METRIC_KEYS, *soft_keys)),
        allowed_final_deviation_codes=frozenset(),
        fingerprint="verified-config-identity",
    )
    return ProcessRuleSet(**(values | changes))


def width_rule(name="width", *, maximum="10", enabled=True):
    return SyntheticWidthLimitRule(
        name,
        name,
        RuleScope.EDGE,
        enabled,
        "1",
        {"maximum_increase": Decimal(maximum)},
    )


def priority_rule(enabled=True):
    return SyntheticNodePriorityRule(
        "priority",
        "priority",
        RuleScope.NODE,
        enabled,
        "1",
        {"attribute": "priority"},
    )


def split_rule(name="split", enabled=True):
    return ControlledOrderSplitRule(
        name,
        name,
        RuleScope.ACTION_ELIGIBILITY,
        enabled,
        "1",
        {
            "grade_class": "IF钢",
            "width_upper_exclusive": Decimal(1400),
            "maximum_piece_weight": Decimal(500),
            "minimum_piece_weight": Decimal(1),
            "maximum_accepted_source_count": 10000,
            "maximum_separator_node_count": 2,
            "maximum_separator_weight": Decimal(40),
            "allowed_modes": ("same_period_split", "future_borrow_return"),
        },
    )


def test_rule_set_copies_and_freezes_metadata_and_scope_index():
    rules, codes = [width_rule()], {"underweight"}
    ruleset = rule_set(rules, allowed_final_deviation_codes=codes)
    rules.clear()
    codes.clear()
    assert ruleset.rules == (width_rule(),)
    assert ruleset.allowed_final_deviation_codes == frozenset({"underweight"})
    with pytest.raises(FrozenInstanceError):
        ruleset.version = "2"
    with pytest.raises(TypeError):
        ruleset._by_scope[RuleScope.EDGE] = ()
    with pytest.raises(ValueError):
        ruleset.rules_for_scope("edge")


def test_synthetic_line_parameters_change_edges_without_core_changes():
    edge = EdgeRuleSubject("a>b", node(), node("b", "1015"))
    strict, relaxed = rule_set([width_rule(maximum="10")]), rule_set([width_rule(maximum="20")])
    assert len(strict.evaluate_edge(edge, context()).violations) == 1
    assert relaxed.evaluate_edge(edge, context()).violations == ()
    reversed_edge = EdgeRuleSubject("b>a", edge.right, edge.left)
    assert strict.evaluate_edge(reversed_edge, context()).violations == ()
    assert (
        strict.evaluate_edge(
            EdgeRuleSubject("same", node(), node("b", "1010")), context()
        ).violations
        == ()
    )


def test_node_rule_contribution_and_construction_priority_share_enabled_source():
    ruleset = rule_set([priority_rule()])
    subject = NodeRuleSubject("a", node(priority=-2))
    assert ruleset.construction_priority(subject.node) == (-2,)
    assert ruleset.evaluate_node(subject, context()).metrics == (
        MetricContribution("synthetic_node_priority", -2),
    )
    disabled = rule_set([priority_rule(False)])
    assert disabled.construction_priority(subject.node) == ()
    assert disabled.evaluate_node(subject, context()) == RuleContribution((), ())


def test_disabled_rule_is_never_called_or_declared_as_metric_producer(monkeypatch):
    def fail(*args):
        raise AssertionError("disabled rule called")

    monkeypatch.setattr(SyntheticWidthLimitRule, "evaluate", fail)
    monkeypatch.setattr(SyntheticWidthLimitRule, "metric_keys", fail)
    ruleset = rule_set([width_rule(enabled=False)])
    assert ruleset.rules == ()
    assert ruleset.metric_aggregations() == {}
    assert ruleset.rules_for_scope(RuleScope.EDGE) == ()
    assert ruleset.evaluate_edge(
        EdgeRuleSubject("edge", node(), node("b")), context()
    ) == RuleContribution((), ())
    with pytest.raises(ValueError, match="no enabled producer"):
        rule_set([width_rule(enabled=False)], soft_keys=("synthetic_width_increase",))


@pytest.mark.parametrize(
    "entry,subject",
    [
        ("evaluate_edge", NodeRuleSubject("node", node())),
        ("evaluate_node", EdgeRuleSubject("edge", node(), node("b"))),
        ("evaluate_chain", NodeRuleSubject("node", node())),
        ("evaluate_complete_chain", NodeRuleSubject("node", node())),
        ("evaluate_plan", NodeRuleSubject("node", node())),
        ("evaluate_controlled_split", NodeRuleSubject("node", node())),
    ],
)
def test_wrong_subject_never_passes_silently_even_without_rules(entry, subject):
    with pytest.raises(UnsupportedRuleSubjectError):
        getattr(rule_set(), entry)(subject, context())


def test_empty_chain_and_plan_scope_dispatch_remain_contributions_not_evaluations():
    chain = Chain("chain", (node(),), "later")
    plan = SchedulePlan((chain,))
    ruleset = rule_set()
    assert ruleset.evaluate_chain(ChainRuleSubject("chain", chain), context()) == RuleContribution(
        (), ()
    )
    assert ruleset.evaluate_plan(
        PlanRuleSubject("plan", plan, None), context()
    ) == RuleContribution((), ())
    with pytest.raises(ValueError, match="context"):
        ruleset.evaluate_chain(ChainRuleSubject("chain", chain), None)


@dataclass(frozen=True, slots=True)
class TraceRule(Rule):
    supported_scope = RuleScope.EDGE

    def required_fields(self):
        return ()

    def evaluate(self, subject, context):
        return RuleContribution(
            (
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "trace",
                    "测试违规",
                    RuleDisposition.PROHIBITED,
                    self.parameters["severity"],
                ),
            ),
            (),
        )


@dataclass(frozen=True, slots=True)
class ChainTraceRule(Rule):
    supported_scope = RuleScope.CHAIN
    required_fields = TraceRule.required_fields
    evaluate = TraceRule.evaluate


def trace_rule(name, scope=RuleScope.CHAIN, *, enabled=True):
    rule_class = TraceRule if scope is RuleScope.EDGE else ChainTraceRule
    return rule_class(name, name, scope, enabled, "1", {"severity": Decimal(1)})


def test_complete_chain_inserts_all_adjacent_pairs_at_first_enabled_edge_position():
    configured = rule_set(
        (
            trace_rule("disabled-first-edge", RuleScope.EDGE, enabled=False),
            trace_rule("before-a"),
            priority_rule(),
            trace_rule("before-b"),
            trace_rule("edge-a", RuleScope.EDGE),
            trace_rule("after-a"),
            trace_rule("edge-b", RuleScope.EDGE),
            trace_rule("after-b"),
            split_rule(),
        )
    )
    chain = Chain("chain", (node("a"), node("b"), node("c")), "later")
    subject, ctx = ChainRuleSubject("chain-subject", chain), context()
    contribution = configured.evaluate_complete_chain(subject, ctx)
    assert [(item.rule_id, item.subject_id) for item in contribution.violations] == [
        ("before-a", "chain-subject"),
        ("before-b", "chain-subject"),
        ("edge-a", "chain-subject:a>b"),
        ("edge-b", "chain-subject:a>b"),
        ("edge-a", "chain-subject:b>c"),
        ("edge-b", "chain-subject:b>c"),
        ("after-a", "chain-subject"),
        ("after-b", "chain-subject"),
    ]
    assert tuple(item.rule_id for item in configured.evaluate_chain(subject, ctx).violations) == (
        "before-a",
        "before-b",
        "after-a",
        "after-b",
    )
    assert contribution.metrics == ()  # NODE and action rules are not dispatched here.
    assert subject.chain is chain and chain.assigned_period == "later"
    with pytest.raises(ValueError, match="context"):
        configured.evaluate_complete_chain(subject, None)


@pytest.mark.parametrize("disabled_edge", (False, True))
def test_complete_chain_without_enabled_edges_is_exactly_chain_only(disabled_edge):
    rules = (trace_rule("first"), trace_rule("second"))
    if disabled_edge:
        rules = (trace_rule("disabled", RuleScope.EDGE, enabled=False),) + rules
    configured = rule_set(rules)
    subject = ChainRuleSubject("subject", Chain("chain", (node(), node("b")), "later"))
    assert configured.evaluate_complete_chain(subject, context()) == configured.evaluate_chain(
        subject, context()
    )


def test_complete_single_node_chain_still_checks_chain_rules_without_an_edge():
    configured = rule_set((trace_rule("edge", RuleScope.EDGE), trace_rule("after")))
    subject = ChainRuleSubject("subject", Chain("chain", (node(),), "later"))
    assert tuple(
        item.rule_id for item in configured.evaluate_complete_chain(subject, context()).violations
    ) == ("after",)


def test_complete_chain_keeps_derived_virtual_anchor_check_in_configured_order():
    virtual = replace(
        node("virtual", "1250"),
        source_order_id=None,
        source_resource_id=None,
        source_period=None,
        material_role=MaterialRole.GENERATED_VIRTUAL,
        virtual_lineage=VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, 1),
    )
    subject = ChainRuleSubject(
        "subject", Chain("chain", (node("left"), virtual, node("right", "1050")), "later")
    )
    width = WidthTransitionRule(
        "width",
        "width",
        RuleScope.EDGE,
        True,
        "1",
        {"max_reverse_width": Decimal(20), "virtual_width_tolerance": Decimal(200)},
    )
    configured = rule_set((trace_rule("before"), width, trace_rule("after")))
    contribution = configured.evaluate_complete_chain(subject, context())
    assert [(item.rule_id, item.scope, item.subject_id) for item in contribution.violations] == [
        ("before", RuleScope.CHAIN, "subject"),
        ("width", RuleScope.EDGE, "subject:left>virtual"),
        ("width", RuleScope.CHAIN, "subject:width:virtual_anchor:0-2"),
        ("after", RuleScope.CHAIN, "subject"),
    ]
    assert contribution.metrics == ()


def test_complete_chain_metric_contributions_follow_chain_edge_chain_order():
    weight = load_rule_set(six_criterion_specification()).rules[0]
    virtual_limit = ConsecutiveVirtualMaterialRule(
        "virtual", "virtual", RuleScope.CHAIN, True, "1", {"max_count": 2}
    )
    configured = rule_set((weight, width_rule(), virtual_limit))
    subject = ChainRuleSubject(
        "subject", Chain("chain", (node("a"), node("b", "1010"), node("c", "1025")), "later")
    )
    contributions = configured.evaluate_complete_chain(subject, context())
    assert tuple(item.metric_key for item in contributions.metrics) == (
        *weight.metric_keys(),
        "synthetic_width_increase",
        "synthetic_width_increase",
        "max_consecutive_virtual_sphc",
    )
    assert contributions.metrics[-1].value == 0


@pytest.mark.parametrize("aggregation", (QualityAggregation.SUM, QualityAggregation.MAXIMUM))
def test_enabled_metric_aggregation_declarations_are_explicit_immutable_and_not_quality_defaults(
    monkeypatch, aggregation
):
    monkeypatch.setattr(SyntheticNodePriorityRule, "metric_aggregation", aggregation)
    configured = rule_set((priority_rule(), width_rule(enabled=False)))
    declared = configured.metric_aggregations()
    assert declared == {"synthetic_node_priority": aggregation}
    assert configured.fingerprint == "verified-config-identity"
    with pytest.raises(TypeError):
        declared["synthetic_node_priority"] = QualityAggregation.SUM


@pytest.mark.parametrize(
    "invalid", (QualityAggregation.COUNT, QualityAggregation.NAMED_VALUE, "sum", "maximum", None, 0)
)
def test_invalid_aggregation_declaration_fails_even_without_a_quality_reference(
    monkeypatch, invalid
):
    monkeypatch.setattr(SyntheticNodePriorityRule, "metric_aggregation", invalid)
    with pytest.raises(ValueError, match="metric_aggregation"):
        rule_set((priority_rule(),))
    assert rule_set((priority_rule(enabled=False),)).metric_aggregations() == {}


@pytest.mark.parametrize(
    "rule_class,parameters,metric",
    (
        (
            HighSurfaceRunCountRule,
            {"surface_grades": ["FC", "FD"], "max_run_count": 5},
            "max_high_surface_run_count",
        ),
        (
            ContinuousNarrowSteelWeightRule,
            {
                "grade_class": "IF钢",
                "width_upper_exclusive": Decimal(1400),
                "max_real_weight": Decimal(500),
            },
            "max_if_narrow_real_run_weight",
        ),
        (
            SameSpecContinuousRealWeightRule,
            {"group_by_fields": ["width"], "max_real_weight": Decimal(1000)},
            "max_same_spec_real_run_weight",
        ),
        (ConsecutiveVirtualMaterialRule, {"max_count": 2}, "max_consecutive_virtual_sphc"),
    ),
)
def test_four_maximum_run_rules_declare_maximum_and_disabled_rules_contribute_nothing(
    rule_class, parameters, metric
):
    rule = rule_class("run", "run", RuleScope.CHAIN, True, "1", parameters)
    assert rule_set((rule,)).metric_aggregations() == {metric: QualityAggregation.MAXIMUM}
    assert rule_set((replace(rule, enabled=False),)).metric_aggregations() == {}
    assert Rule.metric_aggregation is QualityAggregation.SUM


def test_rule_contributions_preserve_order_and_do_not_deduplicate_subjects():
    rules = tuple(
        TraceRule(key, key, RuleScope.EDGE, True, "1", {"severity": Decimal(value)})
        for key, value in (("z", "1e20"), ("a", "1"), ("m", "0.1"))
    )
    result = rule_set(rules).evaluate_edge(EdgeRuleSubject("edge", node(), node("b")), context())
    assert tuple(item.rule_id for item in result.violations) == ("z", "a", "m")
    assert len(result.violations) == 3


def test_chain_segment_and_plan_order_violation_subjects_are_preserved():
    @dataclass(frozen=True, slots=True)
    class SegmentRule(Rule):
        supported_scope = RuleScope.CHAIN

        def required_fields(self):
            return ()

        def evaluate(self, subject, context):
            return RuleContribution(
                (
                    RuleViolation(
                        self.rule_id,
                        self.scope,
                        "chain:0-1",
                        "segment",
                        "连续片段违规",
                        RuleDisposition.PROHIBITED,
                        Decimal(1),
                    ),
                ),
                (),
            )

    @dataclass(frozen=True, slots=True)
    class OrderRule(Rule):
        supported_scope = RuleScope.PLAN

        def required_fields(self):
            return ()

        def evaluate(self, subject, context):
            return RuleContribution(
                (
                    RuleViolation(
                        self.rule_id,
                        self.scope,
                        "source-order",
                        "order",
                        "订单违规",
                        RuleDisposition.PROHIBITED,
                        Decimal(1),
                    ),
                ),
                (),
            )

    ruleset = rule_set(
        (
            SegmentRule("segment", "segment", RuleScope.CHAIN, True, "1", {}),
            OrderRule("order", "order", RuleScope.PLAN, True, "1", {}),
        )
    )
    chain = Chain("chain", (node(),), "later")
    assert (
        ruleset.evaluate_chain(ChainRuleSubject("chain", chain), context()).violations[0].subject_id
        == "chain:0-1"
    )
    assert (
        ruleset.evaluate_plan(PlanRuleSubject("plan", SchedulePlan((chain,)), None), context())
        .violations[0]
        .subject_id
        == "source-order"
    )


@pytest.mark.parametrize(
    "rules,match",
    [
        ([width_rule(), width_rule()], "duplicate rule"),
        ([width_rule("a"), width_rule("b")], "duplicate metric"),
        ([split_rule("a"), split_rule("b")], "at most one"),
    ],
)
def test_ambiguous_rule_and_producer_registration_fails(rules, match):
    with pytest.raises(ValueError, match=match):
        rule_set(rules)


def test_fixed_metrics_cannot_be_shadowed(monkeypatch):
    monkeypatch.setattr(SyntheticWidthLimitRule, "metric_keys", lambda self: ("chain_count",))
    with pytest.raises(ValueError, match="duplicate metric"):
        rule_set([width_rule()])


def test_third_inheritance_level_and_foreign_action_producer_fail():
    @dataclass(frozen=True, slots=True)
    class ThirdLevel(SyntheticWidthLimitRule):
        pass

    with pytest.raises(ValueError, match="directly"):
        rule_set(
            [ThirdLevel("x", "x", RuleScope.EDGE, True, "1", {"maximum_increase": Decimal(1)})]
        )

    @dataclass(frozen=True, slots=True)
    class ForeignAction(Rule):
        supported_scope = RuleScope.ACTION_ELIGIBILITY

        def required_fields(self):
            return ()

    with pytest.raises(ValueError, match="requires ControlledOrderSplitRule"):
        rule_set([ForeignAction("x", "x", RuleScope.ACTION_ELIGIBILITY, True, "1", {})])


def test_quality_priority_is_frozen_with_hard_metrics_first():
    first = rule_set(soft_keys=("chain_count", "generated_virtual_weight"))
    second = rule_set(soft_keys=("generated_virtual_weight", "chain_count"))
    assert first.quality_spec != second.quality_spec
    for bad in (
        (),
        first.quality_spec[::-1],
        (
            replace(first.quality_spec[0], direction=QualityDirection.MAXIMIZE),
            *first.quality_spec[1:],
        ),
    ):
        with pytest.raises(ValueError, match="first minimizing"):
            rule_set(quality_spec=bad)
    with pytest.raises(ValueError, match="duplicate quality"):
        rule_set(quality_spec=first.quality_spec + (first.quality_spec[-1],))


def six_criterion_specification():
    weight_rule = ChainWeightRangeRule(
        "chain-weight",
        "链重范围",
        RuleScope.CHAIN,
        True,
        "1",
        {"min_weight": Decimal(700), "max_weight": Decimal(2000), "target_weight": Decimal(2000)},
    )
    quality = tuple(
        criterion(key)
        for key in (
            *PROHIBITED_METRIC_KEYS,
            "underweight_chain_count",
            "underweight_total_gap",
            "chain_count",
            "generated_virtual_weight",
        )
    )
    quality = (
        quality[:3]
        + (
            replace(
                quality[3],
                aggregation=QualityAggregation.SUM,
                numeric_projection=NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM,
            ),
        )
        + quality[4:]
    )
    configured = rule_set((weight_rule,), quality_spec=quality)
    spec = RuleSetSpec(
        configured.product_line_code,
        configured.process_code,
        configured.scenario,
        configured.version,
        (
            RuleDefinitionSpec(
                weight_rule.rule_id,
                weight_rule.rule_type,
                weight_rule.name,
                weight_rule.scope,
                weight_rule.enabled,
                weight_rule.version,
                weight_rule.parameters,
            ),
        ),
        tuple(
            QualityCriterionSpec(
                item.criterion_id,
                item.metric_key,
                item.direction.value,
                item.aggregation.value,
                item.numeric_projection.value,
            )
            for item in configured.quality_spec
        ),
        configured.allowed_final_deviation_codes,
        "pending",
    )
    return replace(spec, fingerprint=fingerprint_rule_set_spec(spec))


def test_six_criterion_configuration_loads_without_borrowed_weight_score():
    spec = six_criterion_specification()
    loaded = load_rule_set(spec)
    assert tuple(item.metric_key for item in loaded.quality_spec) == (
        "prohibited_violation_count",
        "prohibited_violation_severity",
        "underweight_chain_count",
        "underweight_total_gap",
        "chain_count",
        "generated_virtual_weight",
    )
    assert len(loaded.quality_spec) == 6
    assert "borrowed_future_weight" not in {item.metric_key for item in loaded.quality_spec}
    assert (
        loaded.quality_spec[3].numeric_projection
        is NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM
    )
    assert isinstance(loaded.rules[0], ChainWeightRangeRule)
    assert loaded.rules_for_scope(RuleScope.CHAIN) == loaded.rules
    assert loaded.fingerprint == spec.fingerprint


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "rule_id,rule_type,scope,parameters",
    [
        (
            "future_pool_borrow_limit_ratio",
            "FutureBorrowRatioRule",
            RuleScope.PLAN,
            {"max_ratio": Decimal(1)},
        ),
        ("grade_connection_policy", "GradeConnectionRule", RuleScope.EDGE, {"policy": {}}),
    ],
)
def test_removed_rules_are_not_registered_and_only_disabled_history_loads(
    enabled, rule_id, rule_type, scope, parameters
):
    assert rule_type not in RULE_REGISTRY
    assert "SoftHardConnectionRule" in RULE_REGISTRY
    spec = six_criterion_specification()
    removed = RuleDefinitionSpec(
        rule_id,
        rule_type,
        "已取消规则的历史定义",
        scope,
        enabled,
        "1",
        parameters,
    )
    spec = replace(spec, rules=spec.rules + (removed,))
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    if enabled:
        with pytest.raises(RuleSetLoadError) as caught:
            load_rule_set(spec)
        assert [issue.code for issue in caught.value.issues] == ["unknown_enabled_rule"]
        assert caught.value.issues[0].subject_id == removed.rule_id
    else:
        loaded = load_rule_set(spec)
        assert len(loaded.rules) == 1
        assert loaded.rules_for_scope(scope) == ()
        assert loaded.fingerprint == spec.fingerprint


def test_zero_split_producer_is_canonical_rejection_bound_to_current_task():
    subject = ControlledSplitRuleSubject("split", node(), "earlier", "later", 0)
    ruleset = rule_set([split_rule(enabled=False)])
    first = ruleset.evaluate_controlled_split(subject, context())
    assert not first.eligible and first.reason_code == "controlled_order_split_disabled"
    assert first.maximum_separator_weight == 0 and first.mode is None
    assert first == ruleset.evaluate_controlled_split(subject, context())
    assert (
        first.decision_fingerprint
        != ruleset.evaluate_controlled_split(
            subject, context(("later", "earlier"))
        ).decision_fingerprint
    )


def test_split_routing_forwards_both_objects_unchanged(monkeypatch):
    subject = ControlledSplitRuleSubject("split", node(), "earlier", "later", 0)
    ctx = context()
    seen = []

    def evaluate(self, passed_subject, passed_context):
        seen.append((passed_subject, passed_context))
        return create_controlled_split_decision(
            passed_subject,
            passed_context,
            eligible=False,
            rule_id=self.rule_id,
            rule_version=self.version,
            reason_code="test_only_rejection",
        )

    monkeypatch.setattr(ControlledOrderSplitRule, "evaluate_controlled_split", evaluate)
    ruleset = rule_set([split_rule()])
    decision = ruleset.evaluate_controlled_split(subject, ctx)
    assert seen[0][0] is subject and seen[0][1] is ctx
    assert decision.rule_id == "split"
    assert ruleset.evaluate_edge(
        EdgeRuleSubject("edge", node(), node("b")), ctx
    ) == RuleContribution((), ())
    assert len(seen) == 1


def test_real_split_qualification_is_routed_only_through_its_dedicated_entry():
    parent = replace(node(), rule_attributes={"grade_class": "IF钢"})
    subject = ControlledSplitRuleSubject("split", parent, "earlier", "later", 0)
    decision = rule_set([split_rule()]).evaluate_controlled_split(subject, context())
    assert not decision.eligible and decision.reason_code == "source_weight_within_limit"
    assert decision == split_rule().evaluate_controlled_split(subject, context())
    with pytest.raises(UnsupportedRuleSubjectError):
        split_rule().evaluate(subject, context())


@pytest.mark.parametrize("entry", ("edge", "complete_edge", "complete_prefix", "complete_suffix"))
@pytest.mark.parametrize("defect", ("wrong_type", "identity", "scope", "metric"))
def test_invalid_contribution_fails_closed(monkeypatch, defect, entry):
    def evaluate(self, subject, context):
        if defect == "wrong_type":
            return None
        if defect == "metric":
            return RuleContribution((), (MetricContribution("undeclared", 0),))
        return RuleContribution(
            (
                RuleViolation(
                    "other-rule" if defect == "identity" else self.rule_id,
                    RuleScope.NODE if defect == "scope" else self.scope,
                    subject.subject_id,
                    "invalid",
                    "错误身份",
                    RuleDisposition.PROHIBITED,
                    Decimal(1),
                ),
            ),
            (),
        )

    rule_class = (
        ChainTraceRule
        if entry in ("complete_prefix", "complete_suffix")
        else SyntheticWidthLimitRule
    )
    monkeypatch.setattr(rule_class, "evaluate", evaluate)
    rules = (width_rule(),)
    if entry == "complete_prefix":
        rules = (trace_rule("prefix"), *rules)
    elif entry == "complete_suffix":
        rules += (trace_rule("suffix"),)
    configured = rule_set(rules)
    with pytest.raises(ValueError):
        if entry == "edge":
            configured.evaluate_edge(EdgeRuleSubject("edge", node(), node("b")), context())
        else:
            configured.evaluate_complete_chain(
                ChainRuleSubject("chain", Chain("chain", (node(), node("b")), "later")),
                context(),
            )
