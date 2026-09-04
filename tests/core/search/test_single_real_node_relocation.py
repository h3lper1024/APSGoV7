"""Single-real-node moves preserve reference ordering and candidate isolation."""

from dataclasses import replace
from decimal import Decimal, Inexact, localcontext

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import MaterialRole, SchedulePlan, VirtualPurpose
from apsgo_scheduler.core.neighborhoods import SearchContext, improve_real_node_relocation
from apsgo_scheduler.core.rules.concrete import HighSurfaceRunCountRule, VirtualOutputRatioRule
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.search.test_complete_candidate_lifecycle import period_case
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.search.test_whole_chain_neighborhood import chain, make_search, weight_rule
from tests.core.test_reference_numeric_projection import criterion

D = Decimal


def score_underweight(state, context):
    old = context.factory
    quality = old.cache.rule_set.quality_spec + (
        criterion("underweight_chain_count"),
        criterion("underweight_total_gap"),
    )
    rules = replace(
        old.cache.rule_set,
        quality_spec=quality,
        fingerprint=fingerprint((old.cache.rule_set.rules, quality)),
    )
    factory = VirtualFactory(
        RuleEdgeDecisionCache(old.cache.problem, rules, old.cache.context), old.budget
    )
    return (
        replace(
            state,
            current_evaluation=evaluate_plan(state.current_plan, rules, old.cache.context),
        ),
        SearchContext(factory, context.policy),
    )


def relocation(chains, *, minimum="50", maximum="200", improvement=False, extras=(), **kwargs):
    state, context = make_search(
        chains,
        rule_items=(weight_rule(minimum=minimum, maximum=maximum), *extras),
        **kwargs,
    )
    return score_underweight(state, context) if improvement else (state, context)


def assert_unchanged(state, context, before):
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()


def test_target_donor_node_and_position_order_is_stable_without_acceptance(monkeypatch):
    initial = (
        chain("T", node("t1", weight="10"), node("t2", weight="10")),
        chain("D1", node("a", weight="10"), node("base1", weight="50")),
        chain("U", node("u", weight="30")),
        chain("D2", node("b", weight="10"), node("base2", weight="50")),
    )
    state, context = relocation(initial)
    before, observed = fingerprint(state), []
    original = neighborhoods.try_complete_candidate

    def record(state, context, chains, **kwargs):
        donor_id, target_id = kwargs["affected_chain_ids"]
        target = next(item for item in chains if item.chain_id == target_id)
        observed.append((donor_id, target_id, tuple(item.node_id for item in target.nodes)))
        assert tuple(item.chain_id for item in chains) == ("T", "D1", "U", "D2")
        assert kwargs["action_name"] == "real_node_relocation"
        assert kwargs["virtual_sequence"] == 0
        return original(state, context, chains, **kwargs)

    monkeypatch.setattr(neighborhoods, "try_complete_candidate", record)
    assert improve_real_node_relocation(state, context) is state
    assert observed == [
        (donor, target, members[:position] + (moved,) + members[position:])
        for target, members in (("T", ("t1", "t2")), ("U", ("u",)))
        for donor, moved in (("D1", "a"), ("D2", "b"))
        for position in range(len(members) + 1)
    ]
    assert context.factory.budget.candidate_check_count == 10
    assert context.complete_candidate_evaluation_count == 10
    assert context.factory.budget.stop_reason is None
    assert_unchanged(state, context, before)


def test_first_improvement_restarts_and_preserves_chain_positions_and_virtual_cursor():
    initial = (
        chain("T", node("t", weight="20")),
        chain("D", node("a", weight="10"), node("b", weight="10"), node("base", weight="50")),
        chain("U", node("u", weight="50")),
    )
    state, context = relocation(initial, improvement=True)
    improve_real_node_relocation(state, context)
    assert tuple(item.chain_id for item in state.current_plan.chains) == ("T", "D", "U")
    assert tuple(item.node_id for item in state.current_plan.chains[0].nodes) == ("b", "a", "t")
    assert tuple(item.node_id for item in state.current_plan.chains[1].nodes) == ("base",)
    assert state.current_plan.chains[2] is initial[2]
    assert state.virtual_sequence == 0
    assert state.accepted_move_count == 2
    assert context.factory.budget.candidate_check_count == 2
    assert context.complete_candidate_evaluation_count == 2
    assert tuple(item.candidate_check_count for item in context.accepted_move_traces) == (1, 2)
    assert all(item.affected_chain_ids == ("D", "T") for item in context.accepted_move_traces)
    assert (
        context.accepted_move_traces[1].quality_before
        == context.accepted_move_traces[0].quality_after
    )
    assert context.factory.budget.stop_reason is None
    assert initial[0].nodes[0].node_id == "t" and len(initial[1].nodes) == 3


@pytest.mark.parametrize("role", (MaterialRole.NORMAL_REAL, MaterialRole.ACTUAL_TRANSITION))
def test_both_real_roles_are_movable(role):
    moved = replace(node("a", weight="10"), material_role=role)
    state, context = relocation(
        (chain("T", node("t", weight="20")), chain("D", moved, node("base", weight="50"))),
        improvement=True,
    )
    improve_real_node_relocation(state, context)
    assert state.current_plan.chains[0].nodes[0] is moved
    assert state.accepted_move_count == context.factory.budget.candidate_check_count == 1


@pytest.mark.parametrize("worse", (False, True))
def test_equal_and_worse_complete_candidates_do_not_mutate_the_plan(worse):
    t, a = (
        replace(node(name, weight=weight), rule_attributes={"surface_grade": "FC"})
        for name, weight in (("t", "20"), ("a", "10"))
    )
    surface = HighSurfaceRunCountRule(
        "surface",
        "surface",
        RuleScope.CHAIN,
        True,
        "1",
        {"surface_grades": ("FC",), "max_run_count": 1},
    )
    state, context = relocation(
        (chain("T", t), chain("D", a, node("base", weight="50"))),
        extras=(surface,) if worse else (),
    )
    before = fingerprint(state)
    improve_real_node_relocation(state, context)
    assert context.complete_candidate_evaluation_count == 2
    assert context.factory.budget.candidate_check_count == 2
    assert_unchanged(state, context, before)


@pytest.mark.parametrize(
    "target_weight,donor_weights,maximum",
    (
        ("20", ("0.0000005", "49.9999995"), "200"),
        ("20", ("30", "30"), "200"),
        ("49", ("20", "50"), "60"),
    ),
)
def test_weight_prefilters_reject_before_any_position_count(target_weight, donor_weights, maximum):
    state, context = relocation(
        (
            chain("T", node("t", weight=target_weight)),
            chain(
                "D",
                *(node(f"d-{index}", weight=weight) for index, weight in enumerate(donor_weights)),
            ),
        ),
        maximum=maximum,
    )
    before = fingerprint(state)
    improve_real_node_relocation(state, context)
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert_unchanged(state, context, before)


def test_donor_just_above_minimum_is_not_filtered_using_an_extra_epsilon():
    state, context = relocation(
        (
            chain("T", node("t", weight="20")),
            chain("D", node("tiny", weight="0.0000005"), node("base", weight="50")),
        )
    )
    before = fingerprint(state)
    improve_real_node_relocation(state, context)
    assert context.factory.budget.candidate_check_count == 2
    assert context.complete_candidate_evaluation_count == 2
    assert_unchanged(state, context, before)


@pytest.mark.parametrize("remaining,checks", (("50", 2), ("49.999999", 2), ("49.999998999", 0)))
def test_remaining_weight_epsilon_uses_exact_arithmetic(remaining, checks):
    state, context = relocation(
        (
            chain("T", node("t", weight="20")),
            chain("D", node("a", weight="10"), node("base", weight=remaining)),
        )
    )
    before = fingerprint(state)
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        decimal_context.traps[Inexact] = True
        improve_real_node_relocation(state, context)
    assert context.factory.budget.candidate_check_count == checks
    assert context.complete_candidate_evaluation_count == checks
    assert_unchanged(state, context, before)


@pytest.mark.parametrize(
    "moved_weight,first_moved", (("51", "a"), ("51.000001", "a"), ("51.000001001", "base"))
)
def test_target_upper_weight_epsilon_is_checked_before_position_count(moved_weight, first_moved):
    state, context = relocation(
        (
            chain("T", node("t", weight="49")),
            chain("D", node("a", weight=moved_weight), node("base", weight="50")),
        ),
        maximum="100",
    )
    improve_real_node_relocation(state, context)
    assert state.current_plan.chains[0].nodes[0].node_id == first_moved
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 1
    assert state.accepted_move_count == 1


@pytest.mark.parametrize(
    "closure_allowed,middle_left_allowed", ((False, False), (True, False), (True, True))
)
def test_donor_closure_precedes_count_then_left_and_right_edges_short_circuit(
    closure_allowed, middle_left_allowed, monkeypatch
):
    members = {
        name: replace(node(name, weight=weight), grade=name)
        for name, weight in (("t1", "10"), ("t2", "10"), ("L", "25"), ("M", "10"), ("R", "25"))
    }
    allowed = {("L", "M"), ("M", "R"), ("t1", "t2"), ("t2", "M")}
    if closure_allowed:
        allowed.add(("L", "R"))
    if middle_left_allowed:
        allowed.add(("t1", "M"))
    state, context = relocation(
        (
            chain("T", members["t1"], members["t2"]),
            chain("D", members["L"], members["M"], members["R"]),
        ),
        allowed_edges=allowed,
    )
    before, observed = fingerprint(state), []
    original = RuleEdgeDecisionCache.allows

    def record(cache, left, right):
        observed.append((left.node_id, right.node_id, context.factory.budget.candidate_check_count))
        return original(cache, left, right)

    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", record)
    improve_real_node_relocation(state, context)
    expected = [("L", "R", 0)]
    if closure_allowed:
        expected.extend((("M", "t1", 1), ("t1", "M", 2)))
        if middle_left_allowed:
            expected.append(("M", "t2", 2))
        expected.append(("t2", "M", 3))
    assert observed == expected
    assert context.factory.budget.candidate_check_count == (3 if closure_allowed else 0)
    assert context.complete_candidate_evaluation_count == int(closure_allowed)
    assert_unchanged(state, context, before)


@pytest.mark.parametrize("disabled", (False, True))
def test_missing_or_disabled_chain_weight_rule_has_no_hidden_targets(disabled):
    state, context = make_search(
        (
            chain("T", node("t", weight="20")),
            chain("D", node("a", weight="10"), node("base", weight="50")),
        ),
        rule_items=(weight_rule(minimum="50", enabled=False),) if disabled else (),
    )
    before = fingerprint(state)
    assert improve_real_node_relocation(state, context) is state
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert context.factory.budget.stop_reason is None
    assert_unchanged(state, context, before)


def add_existing_virtual(state, context):
    target, donor = state.current_plan.chains
    virtual = context.factory.materialize(
        context.factory.cache.problem.virtual_prototypes[0],
        donor.nodes[-1],
        target.nodes[0],
        purpose=VirtualPurpose.WEIGHT_FILL,
        sequence=1,
    )
    plan = SchedulePlan((target, replace(donor, nodes=donor.nodes + (virtual,))))
    return replace(
        state,
        current_plan=plan,
        current_evaluation=evaluate_plan(
            plan, context.factory.cache.rule_set, context.factory.cache.context
        ),
        virtual_sequence=1,
    )


def test_pure_virtual_donor_remainder_is_rejected_after_count_and_insert_edges(monkeypatch):
    state, context = relocation(
        (chain("T", node("t", weight="600")), chain("D", node("a", weight="20"))),
        minimum="700",
        maximum="2000",
        prototypes=(prototype("p", weight="800"),),
    )
    state = add_existing_virtual(state, context)
    before, observed = fingerprint(state), []
    original = RuleEdgeDecisionCache.allows

    def record(cache, left, right):
        observed.append((left.node_id, right.node_id, context.factory.budget.candidate_check_count))
        return original(cache, left, right)

    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", record)
    improve_real_node_relocation(state, context)
    assert observed == [("a", "t", 1), ("t", "a", 2)]
    assert context.factory.budget.candidate_check_count == 2
    assert context.complete_candidate_evaluation_count == 0
    assert state.virtual_sequence == 1
    assert_unchanged(state, context, before)


@pytest.mark.parametrize("target_period,checks,accepted", (("a-late", 1, 1), ("z-early", 4, 0)))
def test_existing_split_pieces_are_enumerated_before_shared_target_period_check(
    target_period,
    checks,
    accepted,
):
    state, context = period_case(other_period=target_period, split=True)
    old = context.factory
    weights = {"a": D(5), "u": D(10)}
    original_nodes = tuple(
        replace(item, weight=weights.get(item.node_id, item.weight))
        for item in old.cache.problem.nodes
    )
    problem = replace(
        old.cache.problem, nodes=original_nodes, input_fingerprint=fingerprint(original_nodes)
    )
    rules = replace(
        old.cache.rule_set,
        rules=(weight_rule(minimum="10", maximum="100"),),
        quality_spec=old.cache.rule_set.quality_spec[:2],
        fingerprint="split-relocation-rules",
    )
    factory = VirtualFactory(RuleEdgeDecisionCache(problem, rules, old.cache.context), old.budget)
    context = SearchContext(factory, context.policy)
    plan = SchedulePlan(
        tuple(
            replace(
                item,
                nodes=tuple(
                    replace(member, weight=weights.get(member.node_id, member.weight))
                    for member in item.nodes
                ),
            )
            for item in state.current_plan.chains
        )
    )
    state = replace(
        state, current_plan=plan, current_evaluation=evaluate_plan(plan, rules, old.cache.context)
    )
    state, context = score_underweight(state, context)
    before = fingerprint(state)
    pieces = state.current_plan.chains[0].nodes
    improve_real_node_relocation(state, context)
    assert context.factory.budget.candidate_check_count == checks
    assert context.complete_candidate_evaluation_count == accepted
    assert state.accepted_move_count == accepted
    assert state.split_sequence == state.accepted_same_period_split_count == 1
    assert state.virtual_sequence == 0
    if accepted:
        assert tuple(item.chain_id for item in state.current_plan.chains) == ("c-0", "c-1", "c-2")
        assert state.current_plan.chains[0].nodes == (pieces[1],)
        assert state.current_plan.chains[1].nodes[0] == pieces[0]
        assert state.current_plan.chains[1].assigned_period == "a-late"
        assert context.accepted_move_traces[0].affected_source_order_ids == ("z", "a")
    else:
        assert_unchanged(state, context, before)


def test_unchanged_global_virtual_ratio_violation_does_not_block_an_improving_move():
    ratio_rule = VirtualOutputRatioRule(
        "ratio",
        "ratio",
        RuleScope.PLAN,
        True,
        "1",
        {"max_ratio": D("0.1")},
    )
    state, context = relocation(
        (
            chain("T", node("t", weight="20")),
            chain("D", node("a", weight="10"), node("base", weight="50")),
        ),
        improvement=True,
        extras=(ratio_rule,),
        prototypes=(prototype("p", weight="20"),),
    )
    state = add_existing_virtual(state, context)
    before = state.current_evaluation
    improve_real_node_relocation(state, context)
    assert state.current_evaluation.quality_key < before.quality_key
    assert state.current_evaluation.quality_key[:2] == before.quality_key[:2]
    assert state.current_evaluation.metrics["virtual_output_weight_ratio"] == D("0.2")
    assert state.current_plan.chains[0].nodes[0].node_id == "a"
    assert state.virtual_sequence == state.accepted_move_count == 1
    assert context.factory.budget.candidate_check_count == 1


class Stop:
    active = False

    def is_cancelled(self):
        return self.active


@pytest.mark.parametrize("stop_kind", ("cancel", "time"))
@pytest.mark.parametrize("when", ("before", "after_edge", "after_evaluation"))
def test_cancellation_and_time_never_publish_an_interrupted_move(stop_kind, when, monkeypatch):
    stop = Stop()
    runtime = budget(
        candidate_check_limit=10,
        cancellation=stop if stop_kind == "cancel" else None,
        clock=lambda: 100.0 if stop.active and stop_kind == "time" else 1.0,
    )
    state, context = relocation(
        (
            chain("T", node("t", weight="20")),
            chain("D", node("a", weight="10"), node("base", weight="50")),
        ),
        improvement=True,
        runtime=runtime,
    )
    before = fingerprint(state)
    if when == "before":
        stop.active = True
    elif when == "after_edge":
        original = RuleEdgeDecisionCache.allows

        def stop_after_edge(cache, left, right):
            result = original(cache, left, right)
            stop.active = True
            return result

        monkeypatch.setattr(RuleEdgeDecisionCache, "allows", stop_after_edge)
    else:
        original = neighborhoods.evaluate_plan

        def stop_after_evaluation(*args):
            result = original(*args)
            stop.active = True
            return result

        monkeypatch.setattr(neighborhoods, "evaluate_plan", stop_after_evaluation)
    improve_real_node_relocation(state, context)
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if stop_kind == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )
    assert runtime.candidate_check_count == int(when != "before")
    assert context.complete_candidate_evaluation_count == int(when == "after_evaluation")
    assert_unchanged(state, context, before)


def test_zero_candidate_allowance_stops_before_edges_or_evaluation():
    state, context = relocation(
        (
            chain("T", node("t", weight="20")),
            chain("D", node("a", weight="10"), node("base", weight="50")),
        ),
        improvement=True,
        limit=0,
    )
    before = fingerprint(state)
    improve_real_node_relocation(state, context)
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert_unchanged(state, context, before)


@pytest.mark.parametrize("during_evaluation", (False, True))
def test_rule_or_evaluation_exceptions_propagate_without_candidate_leak(
    during_evaluation, monkeypatch
):
    state, context = relocation(
        (
            chain("T", node("t", weight="20")),
            chain("D", node("a", weight="10"), node("base", weight="50")),
        ),
        improvement=True,
    )
    before = fingerprint(state)

    def fail(*args):
        raise RuntimeError("synthetic candidate failure")

    if during_evaluation:
        monkeypatch.setattr(neighborhoods, "evaluate_plan", fail)
    else:
        monkeypatch.setattr(RuleEdgeDecisionCache, "allows", fail)
    with pytest.raises(RuntimeError, match="synthetic candidate failure"):
        improve_real_node_relocation(state, context)
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == int(during_evaluation)
    assert context.factory.budget.stop_reason is None
    assert_unchanged(state, context, before)
