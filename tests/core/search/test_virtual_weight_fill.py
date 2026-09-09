"""Virtual weight filling keeps reference enumeration, screening and private numbers."""

from dataclasses import replace
from decimal import Decimal, Inexact, localcontext

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, SearchStopReason, fingerprint
from apsgo_scheduler.core.model import VirtualPurpose
from apsgo_scheduler.core.neighborhoods import improve_virtual_weight_fill
from apsgo_scheduler.core.rules.concrete import (
    ConsecutiveVirtualMaterialRule,
    VirtualOutputRatioRule,
)
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.search.test_single_real_node_relocation import Stop, score_underweight
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.search.test_whole_chain_neighborhood import chain, make_search, weight_rule

D = Decimal


def fill_case(
    chains, prototypes=(), *, minimum="50", maximum="100", improvement=True, extras=(), **kwargs
):
    state, context = make_search(
        chains,
        prototypes=prototypes,
        rule_items=(weight_rule(minimum=minimum, maximum=maximum), *extras),
        **kwargs,
    )
    return score_underweight(state, context) if improvement else (state, context)


def ratio_rule(limit="1", *, enabled=True):
    return VirtualOutputRatioRule(
        "ratio",
        "ratio",
        RuleScope.PLAN,
        enabled,
        "1",
        {"max_ratio": D(limit)},
    )


def assert_unchanged(state, context, before, input_before):
    assert fingerprint(state) == before
    assert fingerprint(context.factory.cache.problem) == input_before
    assert context.accepted_move_traces == ()


@pytest.mark.parametrize(
    "position,edges,temperatures",
    (
        (0, {("p", "L")}, ("500", "700")),
        (1, {("L", "p"), ("p", "R")}, ("500", "900")),
        (2, {("R", "p")}, ("600", "900")),
    ),
)
def test_head_middle_and_tail_fill_use_actual_boundary_edges_and_temperature_anchors(
    position,
    edges,
    temperatures,
    monkeypatch,
):
    left = replace(
        node("L", weight="15"), grade="L", min_temperature=D(500), max_temperature=D(700)
    )
    right = replace(
        node("R", weight="15"), grade="R", min_temperature=D(600), max_temperature=D(900)
    )
    initial = (chain("T", left, right), chain("U", node("u", weight="50")))
    state, context = fill_case(
        initial,
        (prototype("p", min_temperature=D(100), max_temperature=D(200)),),
        allowed_edges=edges | {("L", "R")},
    )
    original_input = fingerprint(context.factory.cache.problem)
    observed = []
    original = RuleEdgeDecisionCache.allows

    def record(cache, left, right):
        observed.append((left.grade, right.grade, context.factory.budget.candidate_check_count))
        return original(cache, left, right)

    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", record)
    assert improve_virtual_weight_fill(state, context) is state
    target, unchanged = state.current_plan.chains
    filler = target.nodes[position]
    assert target.chain_id == "T" and unchanged is initial[1]
    assert target.total_weight == D(50)
    assert tuple(item for item in target.nodes if item.virtual_lineage is None) == (left, right)
    assert (filler.min_temperature, filler.max_temperature) == tuple(map(D, temperatures))
    assert filler.node_id == "virtual-000001"
    assert filler.virtual_lineage.purpose is VirtualPurpose.WEIGHT_FILL
    assert filler.virtual_lineage.prototype_id == "p"
    assert filler.virtual_lineage.related_partition_id is None
    assert filler.virtual_lineage.accepted_sequence == 1
    assert state.virtual_sequence == state.accepted_move_count == 1
    assert context.factory.budget.candidate_check_count == position + 1
    assert context.complete_candidate_evaluation_count == 1
    trace = context.accepted_move_traces[0]
    assert trace.action_name == "virtual_weight_fill" and trace.affected_chain_ids == ("T",)
    assert trace.affected_source_order_ids == ("L", "R")
    assert fingerprint(context.factory.cache.problem) == original_input
    assert observed == (
        [("p", "L", 1)]
        if position == 0
        else [("p", "L", 1), ("L", "p", 2), ("p", "R", 2)]
        if position == 1
        else [("p", "L", 1), ("L", "p", 2), ("R", "p", 3)]
    )


def test_chain_position_then_prototype_order_consumes_each_check_and_reuses_rejected_number(
    monkeypatch,
):
    state, context = fill_case(
        (
            chain("T", node("a", weight="10"), node("b", weight="10")),
            chain("U", node("u", weight="30")),
        ),
        (prototype("p", weight="10"), prototype("q", weight="20")),
        improvement=False,
    )
    before, input_before = fingerprint(state), fingerprint(context.factory.cache.problem)
    observed = []
    original = VirtualFactory.materialize

    def record(factory, prototype, left, right, **kwargs):
        result = original(factory, prototype, left, right, **kwargs)
        observed.append(
            (
                left.node_id,
                right.node_id,
                prototype.prototype_id,
                kwargs["sequence"],
                factory.budget.candidate_check_count,
            )
        )
        assert kwargs["purpose"] is VirtualPurpose.WEIGHT_FILL
        return result

    monkeypatch.setattr(VirtualFactory, "materialize", record)
    improve_virtual_weight_fill(state, context)
    expected = [
        (left, right, name, 1)
        for left, right in (("a", "a"), ("a", "b"), ("b", "b"), ("u", "u"), ("u", "u"))
        for name in ("p", "q")
    ]
    assert observed == [(*item, index) for index, item in enumerate(expected, start=1)]
    assert context.factory.budget.candidate_check_count == 10
    assert context.complete_candidate_evaluation_count == 10
    assert context.factory.budget.stop_reason is None
    assert_unchanged(state, context, before, input_before)


def test_first_improvement_restarts_catalog_and_only_acceptance_advances_numbers(monkeypatch):
    state, context = fill_case(
        (chain("T", node("t", weight="20")),),
        (prototype("over", weight="40"), prototype("p", weight="10"), prototype("q", weight="20")),
        maximum="50",
    )
    context.policy = replace(context.policy, maximum_virtual_bridge_nodes=0)
    observed = []
    original = VirtualFactory.materialize

    def record(factory, prototype, left, right, **kwargs):
        observed.append(
            (prototype.prototype_id, kwargs["sequence"], factory.budget.candidate_check_count)
        )
        return original(factory, prototype, left, right, **kwargs)

    def forbid_bridge(*args, **kwargs):
        raise AssertionError("weight filling must not call the bridge or separator selector")

    monkeypatch.setattr(VirtualFactory, "materialize", record)
    monkeypatch.setattr(VirtualFactory, "bridge", forbid_bridge)
    monkeypatch.setattr(VirtualFactory, "separator", forbid_bridge)
    improve_virtual_weight_fill(state, context)
    assert observed == [
        (name, sequence, check)
        for sequence in (1, 2, 3)
        for name, check in (("over", 2 * sequence - 1), ("p", 2 * sequence))
    ]
    assert tuple(item.node_id for item in state.current_plan.chains[0].nodes) == (
        "virtual-000003",
        "virtual-000002",
        "virtual-000001",
        "t",
    )
    assert state.current_plan.chains[0].total_weight == D(50)
    assert state.accepted_move_count == state.virtual_sequence == 3
    assert context.factory.budget.candidate_check_count == 6
    assert context.complete_candidate_evaluation_count == 3
    assert tuple(item.candidate_check_count for item in context.accepted_move_traces) == (2, 4, 6)
    assert context.factory.budget.stop_reason is None


@pytest.mark.parametrize(
    "weight,accepted,checks", (("1", 1, 1), ("1.000001", 1, 1), ("1.000001001", 0, 2))
)
def test_chain_upper_weight_uses_exact_epsilon_after_candidate_count(weight, accepted, checks):
    state, context = fill_case(
        (chain("T", node("t", weight="49")),),
        (prototype("p", weight=weight),),
        maximum="50",
    )
    before, input_before = fingerprint(state), fingerprint(context.factory.cache.problem)
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        decimal_context.traps[Inexact] = True
        improve_virtual_weight_fill(state, context)
    assert context.factory.budget.candidate_check_count == checks
    assert context.complete_candidate_evaluation_count == accepted
    assert state.virtual_sequence == state.accepted_move_count == accepted
    if not accepted:
        assert_unchanged(state, context, before, input_before)


@pytest.mark.parametrize(
    "limit,enabled,accepted",
    (("0.4", True, 1), ("0.399999", True, 1), ("0.399998999", True, 0), ("0", False, 1)),
)
def test_global_virtual_ratio_prefilter_obeys_enabled_rule_and_its_epsilon(
    limit, enabled, accepted
):
    state, context = fill_case(
        (chain("T", node("t", weight="30")),),
        (prototype("p"),),
        extras=(ratio_rule(limit, enabled=enabled),),
    )
    before, input_before = fingerprint(state), fingerprint(context.factory.cache.problem)
    improve_virtual_weight_fill(state, context)
    assert state.accepted_move_count == state.virtual_sequence == accepted
    assert context.complete_candidate_evaluation_count == accepted
    assert context.factory.budget.candidate_check_count == (1 if accepted else 2)
    if not accepted:
        assert_unchanged(state, context, before, input_before)
    elif enabled:
        assert state.current_evaluation.metrics["virtual_output_weight_ratio"] == D("0.4")
    else:
        assert "virtual_output_weight_ratio" not in state.current_evaluation.metrics


def test_continuous_virtual_violation_is_rejected_only_after_complete_evaluation():
    rule = ConsecutiveVirtualMaterialRule(
        "continuous",
        "continuous",
        RuleScope.CHAIN,
        True,
        "1",
        {"max_count": 0},
    )
    state, context = fill_case(
        (chain("T", node("t", weight="30")),),
        (prototype("p"),),
        extras=(rule,),
    )
    before, input_before = fingerprint(state), fingerprint(context.factory.cache.problem)
    improve_virtual_weight_fill(state, context)
    assert context.factory.budget.candidate_check_count == 2
    assert context.complete_candidate_evaluation_count == 2
    assert context.factory.budget.stop_reason is None
    assert_unchanged(state, context, before, input_before)


@pytest.mark.parametrize(
    "empty_reason", ("empty_catalog", "missing_rule", "disabled_rule", "no_underweight")
)
def test_empty_candidate_families_return_naturally_without_hidden_defaults(empty_reason):
    state, context = make_search(
        (chain("T", node("t", weight="60" if empty_reason == "no_underweight" else "30")),),
        prototypes=() if empty_reason == "empty_catalog" else (prototype("p"),),
        rule_items=()
        if empty_reason == "missing_rule"
        else (weight_rule(minimum="50", enabled=empty_reason != "disabled_rule"),),
    )
    before, input_before = fingerprint(state), fingerprint(context.factory.cache.problem)
    assert improve_virtual_weight_fill(state, context) is state
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert context.factory.budget.stop_reason is None
    assert_unchanged(state, context, before, input_before)


@pytest.mark.parametrize("stop_kind", ("cancel", "time"))
@pytest.mark.parametrize(
    "when", ("before", "after_materialize", "after_edge", "after_ratio", "after_evaluation")
)
def test_cancellation_or_time_at_candidate_boundaries_never_publishes_private_material(
    stop_kind,
    when,
    monkeypatch,
):
    stop = Stop()
    runtime = budget(
        candidate_check_limit=10,
        cancellation=stop if stop_kind == "cancel" else None,
        clock=lambda: 100.0 if stop.active and stop_kind == "time" else 1.0,
    )
    state, context = fill_case(
        (chain("T", node("t", weight="30")),),
        (prototype("p"),),
        extras=(ratio_rule(),),
        runtime=runtime,
    )
    before, input_before = fingerprint(state), fingerprint(context.factory.cache.problem)
    if when == "before":
        stop.active = True
    else:
        owner, attribute = {
            "after_materialize": (VirtualFactory, "materialize"),
            "after_edge": (RuleEdgeDecisionCache, "allows"),
            "after_ratio": (VirtualOutputRatioRule, "evaluate"),
            "after_evaluation": (neighborhoods, "_evaluate_candidate_plan"),
        }[when]
        original = getattr(owner, attribute)

        def stop_after_call(*args, **kwargs):
            result = original(*args, **kwargs)
            stop.active = True
            return result

        monkeypatch.setattr(owner, attribute, stop_after_call)
    improve_virtual_weight_fill(state, context)
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if stop_kind == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )
    assert runtime.candidate_check_count == int(when != "before")
    assert context.complete_candidate_evaluation_count == int(when == "after_evaluation")
    assert_unchanged(state, context, before, input_before)


def test_zero_candidate_allowance_does_not_materialize_a_filler(monkeypatch):
    state, context = fill_case(
        (chain("T", node("t", weight="30")),),
        (prototype("p"),),
        limit=0,
    )
    before, input_before = fingerprint(state), fingerprint(context.factory.cache.problem)

    def forbidden(*args, **kwargs):
        raise AssertionError("materialization must follow candidate allowance")

    monkeypatch.setattr(VirtualFactory, "materialize", forbidden)
    improve_virtual_weight_fill(state, context)
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert_unchanged(state, context, before, input_before)


@pytest.mark.parametrize("failure_point", ("materialize", "edge", "ratio", "evaluation"))
def test_candidate_errors_propagate_without_acceptance_or_sequence_change(
    failure_point, monkeypatch
):
    state, context = fill_case(
        (chain("T", node("t", weight="30")),),
        (prototype("p"),),
        extras=(ratio_rule(),),
    )
    before, input_before = fingerprint(state), fingerprint(context.factory.cache.problem)
    owner, attribute = {
        "materialize": (VirtualFactory, "materialize"),
        "edge": (RuleEdgeDecisionCache, "allows"),
        "ratio": (VirtualOutputRatioRule, "evaluate"),
        "evaluation": (neighborhoods, "_evaluate_candidate_plan"),
    }[failure_point]

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic filler failure")

    monkeypatch.setattr(owner, attribute, fail)
    with pytest.raises(RuntimeError, match="synthetic filler failure"):
        improve_virtual_weight_fill(state, context)
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == int(failure_point == "evaluation")
    assert context.factory.budget.stop_reason is None
    assert_unchanged(state, context, before, input_before)
