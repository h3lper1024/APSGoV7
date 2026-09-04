"""Exact, read-only chain-boundary objective and synthetic seven-level integration."""

from dataclasses import replace
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from apsgo_scheduler.api.request import (
    QualityCriterionSpec,
    RuleDefinitionSpec,
    fingerprint_public_request,
)
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import RULE_REGISTRY, RuleSetLoadError, load_rule_set
from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    RuleScope,
    SolverPolicy,
    fingerprint,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, MaterialRole, SchedulePlan, SearchState
from apsgo_scheduler.core.neighborhoods import SearchContext, try_complete_candidate
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    MetricContribution,
    NumericProjection,
    PlanRuleSubject,
    QualityAggregation,
    QualityDirection,
    Rule,
    RuleContribution,
    RuleEvaluationContext,
    UnsupportedRuleSubjectError,
)
from apsgo_scheduler.core.rules.concrete import InterChainWidthGapRule
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.app.test_input_normalizer import (
    get_issues,
    make_order,
    make_prototype,
    make_request,
    make_spec,
)
from tests.core.graph.test_construction_order import setup_graph
from tests.core.test_model_contracts import lineage
from tests.core.test_plan_evaluation import context, node, ruleset
from tests.core.test_quality_key import (
    SeverityRule,
    criterion,
)
from tests.core.test_quality_key import (
    evaluate as evaluate_quality,
)
from tests.core.test_quality_key import (
    node as quality_node,
)
from tests.core.test_quality_key import (
    plan as quality_plan,
)
from tests.core.test_quality_key import (
    ruleset as quality_ruleset,
)

D = Decimal
METRIC = "inter_chain_width_gap"


def rule(**changes):
    return InterChainWidthGapRule(
        **(
            dict(
                rule_id="width-gap",
                name="链间宽差",
                scope=RuleScope.PLAN,
                enabled=True,
                version="1",
                parameters={},
            )
            | changes
        )
    )


def subject(*chains):
    return PlanRuleSubject("plan", SchedulePlan(chains), None)


def gap_spec(*, enabled=True, include_metric=True, **definition_changes):
    definition = RuleDefinitionSpec(
        "width-gap", "InterChainWidthGapRule", "链间宽差", RuleScope.PLAN, enabled, "1", {}
    )
    base = make_spec()
    return make_spec(
        rules=(replace(definition, **definition_changes),),
        quality_spec=base.quality_spec
        + (
            (QualityCriterionSpec(METRIC, METRIC, "minimize", "sum", "exact_decimal"),)
            if include_metric
            else ()
        ),
    )


def with_gap(active):
    return replace(
        active,
        rules=active.rules + (rule(),),
        quality_spec=active.quality_spec + (criterion(METRIC, aggregation=QualityAggregation.SUM),),
        fingerprint="synthetic-width-gap",
    )


def test_declaration_and_static_registration_use_the_existing_rule_contract():
    active = rule()
    assert type(active).__bases__ == (Rule,)
    assert RULE_REGISTRY["InterChainWidthGapRule"] is InterChainWidthGapRule
    assert active.scope is RuleScope.PLAN
    assert active.required_fields() == ("width",)
    assert active.metric_keys() == (METRIC,)
    assert active.metric_aggregation is QualityAggregation.SUM
    assert dict(active.parameters) == {}


@pytest.mark.parametrize(
    "left,right,expected",
    (
        ("1000", "1000", "0"),
        ("1200", "900", "300"),
        ("900", "1200", "300"),
        ("1000.00000000000000000001", "1000", "0.00000000000000000001"),
    ),
)
def test_each_actual_boundary_is_an_exact_absolute_difference(left, right, expected):
    current = subject(
        Chain("left", (node("a", width=D(left)),), "first"),
        Chain("right", (node("b", width=D(right)),), "first"),
    )
    contribution = MetricContribution(METRIC, D(expected))
    assert rule().boundary_contributions(current, context()) == (("left", "right", contribution),)
    assert rule().evaluate(current, context()) == RuleContribution((), (contribution,))


def test_all_boundaries_include_cross_period_and_skipped_period_without_sort_or_wrap():
    periods = ("Z-first", "A-next", "M-empty", "B-last", "Q-unused")
    ctx = RuleEvaluationContext(periods, dict(zip(periods, range(len(periods)))), ())
    current = subject(
        Chain("z-chain", (node("a", width=D(1000)),), "Z-first"),
        Chain("a-chain", (node("b", width=D(1300)),), "Z-first"),
        Chain("middle", (node("c", width=D(1380)),), "A-next"),
        Chain("last", (node("d", width=D(1380)),), "B-last"),
    )
    before = fingerprint(current)
    boundaries = rule().boundary_contributions(current, ctx)
    assert [(left, right, metric.value) for left, right, metric in boundaries] == [
        ("z-chain", "a-chain", D(300)),
        ("a-chain", "middle", D(80)),
        ("middle", "last", D(0)),
    ]
    assert len(boundaries) == len(current.plan.chains) - 1
    assert rule().evaluate(current, ctx).violations == ()
    assert fingerprint(current) == before


def endpoint(name, width, kind):
    if kind == "virtual":
        return node(name, width=D(width), virtual=True)
    if kind == "split":
        return node(
            name,
            width=D(width),
            split_lineage=lineage(
                parent_node_id=f"parent-{name}",
                parent_source_order_id=name,
                source_resource_id=name,
                source_period="first",
                origin_assigned_period="first",
                target_assigned_period="first",
                parent_weight=D(20),
            ),
        )
    role = MaterialRole.ACTUAL_TRANSITION if kind == "actual" else MaterialRole.NORMAL_REAL
    return node(name, width=D(width), material_role=role)


@pytest.mark.parametrize("left_kind", ("real", "actual", "virtual", "split"))
@pytest.mark.parametrize("right_kind", ("real", "actual", "virtual", "split"))
def test_endpoint_roles_do_not_skip_actual_first_or_last_node(left_kind, right_kind):
    current = subject(
        Chain("left", (node("anchor-l", width=D(1900)), endpoint("l", 1000, left_kind)), "first"),
        Chain("right", (endpoint("r", 1200, right_kind), node("anchor-r", width=D(500))), "first"),
    )
    assert rule().boundary_contributions(current, context()) == (
        ("left", "right", MetricContribution(METRIC, D(200))),
    )


def test_single_chain_has_no_boundary_but_explicit_decimal_zero():
    current = subject(Chain("only", (node("a", width=D(900)), node("b", width=D(1700))), "first"))
    assert rule().boundary_contributions(current, context()) == ()
    assert rule().evaluate(current, context()) == RuleContribution(
        (), (MetricContribution(METRIC, D(0)),)
    )


@pytest.mark.parametrize("periods", (("later", "first"), ("first", "unknown")))
def test_invalid_period_membership_or_order_is_rejected_without_repair(periods):
    current = subject(
        *(Chain(f"c{i}", (node(f"n{i}"),), period) for i, period in enumerate(periods))
    )
    before = fingerprint(current)
    for method in (rule().boundary_contributions, rule().evaluate):
        with pytest.raises(ValueError, match="period"):
            method(current, context())
    assert fingerprint(current) == before
    assert tuple(chain.assigned_period for chain in current.plan.chains) == periods


@pytest.mark.parametrize("position", (0, -1))
@pytest.mark.parametrize(
    "width", (None, D(0), D(-1), D("NaN"), D("Infinity"), D("-Infinity"), 1000, "1000", True)
)
def test_single_chain_still_validates_both_endpoint_widths(position, width):
    ends = (node("first"), node("last"))
    # Node construction already rejects malformed widths; exercise the direct rule guard too.
    object.__setattr__(ends[position], "width", width)
    current = subject(Chain("only", ends, "first"))
    for method in (rule().boundary_contributions, rule().evaluate):
        with pytest.raises(ValueError, match="width"):
            method(current, context())


@pytest.mark.parametrize("chain_index,position", ((0, 0), (0, -1), (1, 0), (1, -1)))
def test_each_chain_validates_both_endpoints_including_outer_plan_endpoints(chain_index, position):
    chains = tuple(
        Chain(f"c{i}", (node(f"first-{i}"), node(f"last-{i}")), "first") for i in range(2)
    )
    object.__setattr__(chains[chain_index].nodes[position], "width", None)
    with pytest.raises(ValueError, match="width"):
        rule().evaluate(subject(*chains), context())


@pytest.mark.parametrize("enabled", (False, True))
def test_wrong_subject_is_rejected_even_when_disabled(enabled):
    wrong = ChainRuleSubject("chain", Chain("chain", (node("a"),), "first"))
    for method in (rule(enabled=enabled).boundary_contributions, rule(enabled=enabled).evaluate):
        with pytest.raises(UnsupportedRuleSubjectError):
            method(wrong, context())


@pytest.mark.parametrize("invalid_context", (None, object()))
def test_enabled_rule_requires_a_valid_evaluation_context(invalid_context):
    current = subject(Chain("only", (node("a"),), "first"))
    for method in (rule().boundary_contributions, rule().evaluate):
        with pytest.raises(ValueError, match="context"):
            method(current, invalid_context)


def test_disabled_rule_has_no_requirement_or_hidden_contribution():
    inactive = rule(enabled=False, parameters={"unused": True})
    current = subject(Chain("only", (node("a", width=None),), "unknown"))
    assert inactive.required_fields() == inactive.metric_keys() == ()
    assert inactive.boundary_contributions(current, None) == ()
    assert inactive.evaluate(current, None) == RuleContribution((), ())


def test_sum_and_quality_projection_preserve_decimal_precision_without_float_conversion():
    current = subject(
        Chain("large", (node("a", width=D("1e400")),), "first"),
        Chain("small", (node("b", width=D(1)),), "first"),
        Chain("tiny-gap", (node("c", width=D("1.000000000000001")),), "first"),
    )
    active = with_gap(ruleset())
    expected = D("9" * 400 + ".000000000000001")
    with localcontext() as arithmetic:
        arithmetic.prec = 2
        arithmetic.traps[Inexact] = arithmetic.traps[Rounded] = True
        evaluation = evaluate_plan(current.plan, active, context())
    assert evaluation.metrics[METRIC] == expected
    assert evaluation.quality_key[-1] == expected
    assert evaluation.violations == ()
    assert active.quality_spec[-1].direction is QualityDirection.MINIMIZE
    assert active.quality_spec[-1].aggregation is QualityAggregation.SUM
    assert active.quality_spec[-1].numeric_projection is NumericProjection.EXACT_DECIMAL


def test_synthetic_loader_and_normalizer_work_without_any_edge_rule():
    request = make_request(rule_set_spec=gap_spec())
    before = fingerprint_public_request(request)
    active = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, active)
    assert tuple(type(item) for item in active.rules) == (InterChainWidthGapRule,)
    assert len(problem.nodes) == len(problem.virtual_prototypes) == 2
    assert active.quality_spec[-1].metric_key == METRIC
    assert fingerprint_public_request(request) == before


def test_plan_only_rule_requires_width_for_every_order_role_and_virtual_prototype():
    request = make_request(
        rule_set_spec=gap_spec(),
        orders=(
            make_order(width=None),
            make_order(1, width=None, material_role=MaterialRole.ACTUAL_TRANSITION),
        ),
        virtual_prototypes=(make_prototype(width=None), make_prototype(1, width=None)),
    )
    assert {(item.code, item.field_path) for item in get_issues(request)} == {
        ("missing_required_field", f"{collection}[{index}].width")
        for collection in ("orders", "virtual_prototypes")
        for index in range(2)
    }


@pytest.mark.parametrize("collection", ("orders", "virtual_prototypes"))
@pytest.mark.parametrize("width", (D(0), D(-1), D("NaN"), D("Infinity"), D("1e400")))
def test_normalizer_rejects_invalid_or_non_float_finite_physical_width(collection, width):
    factory = make_order if collection == "orders" else make_prototype
    request = make_request(rule_set_spec=gap_spec(), **{collection: (factory(width=width),)})
    issues = get_issues(request)
    assert any(item.field_path == f"{collection}[0].width" for item in issues)
    if width == D("1e400"):
        assert ("non_finite_float_projection", f"{collection}[0].width") in {
            (item.code, item.field_path) for item in issues
        }


def test_disabled_configuration_without_quality_reference_allows_missing_width():
    request = make_request(
        rule_set_spec=gap_spec(enabled=False, include_metric=False),
        orders=(make_order(width=None),),
        virtual_prototypes=(make_prototype(width=None),),
    )
    active = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, active)
    assert active.rules == ()
    assert problem.nodes[0].width is problem.virtual_prototypes[0].width is None
    assert METRIC not in {item.metric_key for item in active.quality_spec}


@pytest.mark.parametrize(
    "changes,code,path",
    (
        ({"enabled": False}, "invalid_rule_set", "rule_set_spec"),
        (
            {"parameters": {"threshold": D(20)}},
            "invalid_rule_parameters",
            "rule_set_spec.rules[0].parameters",
        ),
        ({"scope": RuleScope.EDGE}, "rule_scope_mismatch", "rule_set_spec.rules[0].scope"),
    ),
)
def test_loader_rejects_disabled_producer_business_parameters_and_wrong_scope(changes, code, path):
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(gap_spec(**changes))
    assert (code, path) in {(item.code, item.field_path) for item in caught.value.issues}


@pytest.mark.parametrize("level", range(6))
def test_each_existing_priority_still_wins_when_the_seventh_metric_gets_worse(level):
    six = quality_ruleset((SeverityRule("severity", "severity", RuleScope.CHAIN, True, "1", {}),))
    active = with_gap(six)
    cases = (
        (
            quality_plan(
                (
                    quality_node("a", 400, attrs={"severity": D(1)}),
                    quality_node("b", 400, attrs={"severity": D(1)}),
                )
            ),
            quality_plan((quality_node("a", 100, attrs={"severity": D(1000)}),)),
        ),
        (
            quality_plan((quality_node("a", attrs={"severity": D(5)}),)),
            quality_plan((quality_node("a", 100, attrs={"severity": D(4)}),)),
        ),
        (
            quality_plan((quality_node("a", 600),), (quality_node("b", 600),)),
            quality_plan((quality_node("a", 100),)),
        ),
        (quality_plan((quality_node("a", 600),)), quality_plan((quality_node("a", 650),))),
        (
            quality_plan((quality_node("a", 700),), (quality_node("b", 700),)),
            quality_plan((quality_node("a", 1400),)),
        ),
        (
            quality_plan((quality_node("a"), quality_node("v", 20, virtual=True))),
            quality_plan((quality_node("a"), quality_node("v", 10, virtual=True))),
        ),
    )
    plans = tuple(
        SchedulePlan(
            current.chains
            + (Chain("anchor", (replace(quality_node("anchor", 700), width=D(width)),), "first"),)
        )
        for current, width in zip(cases[level], (1000, 1200))
    )
    before, candidate = (evaluate_quality(current, active) for current in plans)
    assert len(before.quality_key) == len(candidate.quality_key) == 7
    assert tuple(evaluate_quality(current, six).quality_key for current in plans) == (
        before.quality_key[:6],
        candidate.quality_key[:6],
    )
    assert before.quality_key[:level] == candidate.quality_key[:level]
    assert candidate.quality_key[level] < before.quality_key[level]
    assert candidate.quality_key[-1] == D(200) > before.quality_key[-1] == D(0)
    assert candidate.quality_key < before.quality_key


@pytest.mark.parametrize("improve", (False, True))
def test_complete_candidate_acceptance_uses_seventh_only_after_all_six_tie(improve):
    nodes = tuple(
        replace(quality_node(name), width=D(width), source_period="period")
        for name, width in (("a", 1000), ("b", 1500), ("c", 1200))
    )
    active = with_gap(quality_ruleset())
    _, cache, runtime = setup_graph(nodes, active)
    runtime.candidate_check_limit = 1
    policy = SolverPolicy(7, D(110), D(10), 1, CONSTRUCTION_ORDER_KEY, NUMERIC_SEMANTICS_KEY, D(0))
    search = SearchContext(VirtualFactory(cache, runtime), policy)
    current = SchedulePlan(tuple(Chain(item.node_id, (item,), "period") for item in nodes))
    state = SearchState(current, evaluate_plan(current, active, cache.context))
    before = fingerprint(state)
    a, b, c = current.chains
    candidate = (a, c, b) if improve else (c, b, a)
    evaluation = evaluate_plan(SchedulePlan(candidate), active, cache.context)
    assert evaluation.quality_key[:6] == state.current_evaluation.quality_key[:6]
    assert evaluation.quality_key[-1] == (D(500) if improve else D(800))
    assert runtime.consume_candidate_check()
    accepted = try_complete_candidate(
        state,
        search,
        candidate,
        affected_chain_ids=("a", "b", "c"),
        virtual_sequence=0,
        action_name="synthetic_reorder",
    )
    assert accepted is improve
    assert search.complete_candidate_evaluation_count == 1
    assert state.accepted_move_count == len(search.accepted_move_traces) == int(improve)
    if improve:
        assert state.current_evaluation == evaluation
    else:
        assert fingerprint(state) == before


def test_borrowed_weight_cannot_break_an_equal_seven_level_key():
    a, b, c = (
        quality_node("a", 700),
        quality_node("b", 100, source="later"),
        quality_node("c", 800, source="later"),
    )
    active = with_gap(quality_ruleset())
    borrowed = evaluate_quality(
        SchedulePlan((Chain("early", (a, b), "first"), Chain("late", (c,), "later"))), active
    )
    local = evaluate_quality(
        SchedulePlan((Chain("early", (a,), "first"), Chain("late", (b, c), "later"))), active
    )
    assert borrowed.metrics["borrowed_future_weight"] == 100
    assert local.metrics["borrowed_future_weight"] == 0
    assert borrowed.quality_key == local.quality_key == (0, D(0), 0, D(0), 2, D(0), D(0))
    assert not local.quality_key < borrowed.quality_key
