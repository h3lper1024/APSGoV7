"""Actual node recipes build complete improvements, including every new seam."""

from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.core import width_optimization
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    SchedulePlan,
    SearchState,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.concrete import HighSurfaceRunCountRule
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.width_optimization import (
    _node_recipes,
    _scan_width_batch,
    _scan_width_families,
    _try_segment_edit,
)
from tests.core.graph.test_construction_order import node
from tests.core.search.test_chain_order import chain, search_case
from tests.core.search.test_complete_candidate_lifecycle import Stop
from tests.core.search.test_virtual_material_factory import AllowedConnectionsRule, prototype
from tests.core.search.test_width_optimization_baseline import width_case
from tests.core.search.test_width_optimization_guard import bind_rules, bridge, guard_case
from tests.core.test_model_contracts import lineage

D = Decimal
MOVE = "width_node_move"
EXCHANGE = "width_node_exchange"


def run_actual_recipe(state, context, wanted):
    recipe = next(item for item in _node_recipes(state, context) if item == wanted)
    count = context.factory.budget.candidate_check_count
    accepted, exhausted = _scan_width_batch(state, context, iter((recipe,)), _try_segment_edit, 1)
    assert not exhausted
    assert context.factory.budget.candidate_check_count == count + 1
    return accepted


def nodes_by_id(plan):
    return {node.node_id: node for chain in plan.chains for node in chain.nodes}


def assert_no_change(state, context, before):
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()


def test_recipe_stream_alternates_and_does_not_do_candidate_business_work(monkeypatch):
    state, context = width_case()
    before = fingerprint(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("recipe enumeration must not build or check a candidate")

    monkeypatch.setattr(VirtualFactory, "bridge", forbidden)
    monkeypatch.setattr(RuleEdgeDecisionCache, "allows", forbidden)
    monkeypatch.setattr(width_optimization, "_try_segment_edit", forbidden)
    recipes = tuple(_node_recipes(state, context))
    assert recipes[:6] == (
        (MOVE, 0, 1, 0, 1, 0, 0),
        (EXCHANGE, 0, 1, 0, 1, 0, 1),
        (MOVE, 0, 1, 0, 1, 1, 1),
        (EXCHANGE, 0, 1, 0, 1, 1, 2),
        (MOVE, 0, 1, 0, 1, 2, 2),
        (EXCHANGE, 0, 1, 1, 2, 0, 1),
    )
    assert sum(item[0] == MOVE for item in recipes) == 17
    assert sum(item[0] == EXCHANGE for item in recipes) == 6
    assert all(item[1] < item[2] for item in recipes if item[0] == EXCHANGE)
    assert len(recipes) == len(set(recipes))
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert_no_change(state, context, before)


def test_chain_priority_uses_maximum_incident_gap_with_production_order_ties():
    state, context = search_case(
        tuple(chain(name, width) for name, width in zip("ABCD", (1000, 1600, 1000, 2000)))
    )
    recipes = tuple(_node_recipes(state, context))
    assert all(item[0] == EXCHANGE for item in recipes)
    assert [item[1:3] for item in recipes] == [(2, 3), (0, 2), (0, 3), (0, 1), (1, 2), (1, 3)]


@pytest.mark.parametrize(
    "recipe,gap", (((MOVE, 0, 1, 2, 3, 2, 2), 300), ((EXCHANGE, 0, 1, 1, 2, 0, 1), 400))
)
def test_actual_move_and_exchange_improve_initially_compliant_chains(recipe, gap):
    state, context = width_case()
    old = nodes_by_id(state.current_plan)
    assert state.current_evaluation.violations == ()
    assert run_actual_recipe(state, context, recipe)
    assert state.current_evaluation.quality_key == (0, D(0), 0, D(0), D(gap), D(0), 2)
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == old
    assert context.complete_candidate_evaluation_count == 1
    assert state.accepted_move_count == len(context.accepted_move_traces) == 1
    assert context.accepted_move_traces[0].action_name == recipe[0]
    assert state.virtual_sequence == state.split_sequence == 0


def test_real_node_family_can_scan_accept_restart_and_finish_with_a_complete_plan():
    state, context = width_case()
    old = nodes_by_id(state.current_plan)
    assert _scan_width_families(state, context, (_node_recipes,), _try_segment_edit) is state
    assert state.accepted_move_count == context.complete_candidate_evaluation_count == 3
    assert state.current_evaluation.quality_key[4] == 100
    assert state.current_evaluation.violations == ()
    assert nodes_by_id(state.current_plan) == old
    assert state.current_evaluation == evaluate_plan(
        state.current_plan, context.factory.cache.rule_set, context.factory.cache.context
    )
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert context.factory.budget.candidate_check_count == 48


def test_exchange_can_improve_when_neither_chain_can_donate_a_node():
    _, base = width_case()
    a = Chain(
        "A",
        (node("a-head", width="1600", weight="350"), node("a-tail", width="800", weight="350")),
        "period",
    )
    b = Chain(
        "B",
        (
            node("b-head", width="1500", weight="200"),
            node("b-middle", width="1200", weight="350"),
            node("b-tail", width="700", weight="150"),
        ),
        "period",
    )
    state, context = search_case((a, b), active=base.factory.cache.rule_set)
    old = nodes_by_id(state.current_plan)
    assert state.current_evaluation.violations == ()
    assert state.current_evaluation.quality_key[4] == 700
    assert _scan_width_families(state, context, (_node_recipes,), _try_segment_edit) is state
    assert nodes_by_id(state.current_plan) == old
    assert [chain.total_weight for chain in state.current_plan.chains] == [D(700), D(700)]
    assert state.current_evaluation.quality_key[4] == 300
    assert state.current_evaluation.violations == ()
    assert state.accepted_move_count == context.complete_candidate_evaluation_count == 1
    assert context.accepted_move_traces[0].action_name == EXCHANGE
    assert context.factory.budget.candidate_check_count == 33
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE


@pytest.mark.parametrize("role", (MaterialRole.NORMAL_REAL, MaterialRole.ACTUAL_TRANSITION))
def test_both_real_material_roles_are_eligible(role):
    state, context = width_case()
    a, b = state.current_plan.chains
    moved = replace(a.last_node, material_role=role)
    state, context = search_case(
        (replace(a, nodes=(*a.nodes[:-1], moved)), b), active=context.factory.cache.rule_set
    )
    assert run_actual_recipe(state, context, (MOVE, 0, 1, 2, 3, 2, 2))
    assert state.current_plan.chains[1].last_node is moved


def period_case(*, connected=True, missing=None, donor_weights=(500, 500, 700)):
    periods = ("Z-first", "M-empty", "A-later")
    widths = (1400, 1200, 800, 1500, 900)
    weights = (*donor_weights, 500, 300)
    names = ("a-head", "a-middle", "a-tail", "b-head", "b-tail")
    members = tuple(
        replace(
            node(name, width=str(width), weight=str(weight)),
            source_period=periods[-1] if name in {"a-head", "a-tail"} else periods[0],
            grade=name,
        )
        for name, width, weight in zip(names, widths, weights)
    )
    _, base_context = width_case()
    active = base_context.factory.cache.rule_set
    if not connected:
        allowed = {
            ("a-head", "a-middle"),
            ("a-middle", "a-tail"),
            ("b-head", "b-tail"),
            ("a-head", "bridge"),
            ("bridge", "a-tail"),
            ("b-head", "a-middle"),
            ("a-middle", "b-tail"),
        }
        if missing is not None:
            allowed.remove(missing)
        connection = AllowedConnectionsRule(
            "connections",
            "connections",
            RuleScope.EDGE,
            True,
            "1",
            {"allowed_edges": tuple(sorted(allowed))},
        )
        active = replace(
            active,
            rules=tuple(rule for rule in active.rules if rule.scope is not RuleScope.EDGE)
            + (connection,),
        )
    state, context = search_case(
        (Chain("A", members[:3], periods[0]), Chain("B", members[3:], periods[0])),
        active=active,
        periods=periods,
    )
    if not connected:
        factory = context.factory
        catalog = (prototype("bridge", width="1100"),)
        problem = replace(factory.cache.problem, virtual_prototypes=catalog)
        evaluation_context = replace(factory.cache.context, virtual_prototype_ids=("bridge",))
        context.factory = replace(
            factory, cache=RuleEdgeDecisionCache(problem, active, evaluation_context)
        )
    assert state.current_evaluation.violations == ()
    return state, context


def test_interior_move_can_change_period_and_improve_cross_period_boundary():
    state, context = period_case()
    original = state.current_plan
    assert run_actual_recipe(state, context, (MOVE, 0, 1, 1, 2, 1, 1))
    assert tuple(chain.chain_id for chain in state.current_plan.chains) == ("B", "A")
    assert tuple(chain.assigned_period for chain in state.current_plan.chains) == (
        "Z-first",
        "A-later",
    )
    assert state.current_evaluation.quality_key[4] == 500
    assert nodes_by_id(state.current_plan) == nodes_by_id(original)
    old_endpoints = {
        chain.chain_id: (chain.first_node, chain.last_node) for chain in original.chains
    }
    assert {
        chain.chain_id: (chain.first_node, chain.last_node) for chain in state.current_plan.chains
    } == old_endpoints


@pytest.mark.parametrize(
    "missing", (None, ("bridge", "a-tail"), ("b-head", "a-middle"), ("a-middle", "b-tail"))
)
def test_donor_cut_and_both_receiver_interfaces_are_reconnected_atomically(monkeypatch, missing):
    state, context = period_case(connected=False, missing=missing)
    before = fingerprint(state)
    observed = []
    original = VirtualFactory.bridge

    def record(factory, left, right, **options):
        observed.append((left.node_id, right.node_id, factory.budget.candidate_check_count))
        return original(factory, left, right, **options)

    monkeypatch.setattr(VirtualFactory, "bridge", record)
    accepted = run_actual_recipe(state, context, (MOVE, 0, 1, 1, 2, 1, 1))
    assert accepted is (missing is None)
    assert all(count == 1 for _, _, count in observed)
    if accepted:
        assert [(left, right) for left, right, _ in observed] == [
            ("a-head", "a-tail"),
            ("b-head", "a-middle"),
            ("a-middle", "b-tail"),
        ]
        assert state.virtual_sequence == 1
        assert state.current_evaluation.quality_key == (0, D(0), 0, D(0), D(500), D(20), 2)
    else:
        assert_no_change(state, context, before)
        assert context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize("weights,accepted", (((350, 500, 330), True), ((995, 10, 995), False)))
def test_final_weight_includes_necessary_bridge_before_acceptance(weights, accepted):
    state, context = period_case(connected=False, donor_weights=weights)
    before = fingerprint(state)
    assert run_actual_recipe(state, context, (MOVE, 0, 1, 1, 2, 1, 1)) is accepted
    if accepted:
        donor = next(chain for chain in state.current_plan.chains if chain.chain_id == "A")
        assert donor.total_weight == D(700)
        assert state.current_evaluation.violations == ()
    else:
        assert_no_change(state, context, before)
        assert state.virtual_sequence == 0


def test_no_width_gain_is_counted_but_does_not_build_bridges(monkeypatch):
    state, context = width_case()
    before = fingerprint(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("same endpoint and period widths need no bridge construction")

    monkeypatch.setattr(VirtualFactory, "bridge", forbidden)
    assert not run_actual_recipe(state, context, (MOVE, 0, 1, 1, 2, 1, 1))
    assert_no_change(state, context, before)
    assert context.complete_candidate_evaluation_count == 0


def test_underweight_rejection_still_consumes_one_check():
    state, context = width_case()
    before = fingerprint(state)
    assert not run_actual_recipe(state, context, (MOVE, 1, 0, 1, 2, 3, 3))
    assert_no_change(state, context, before)


def test_edge_legal_node_move_cannot_violate_a_complete_chain_rule():
    state, context = width_case()
    active = context.factory.cache.rule_set
    surface = HighSurfaceRunCountRule(
        "surface",
        "surface",
        RuleScope.CHAIN,
        True,
        "1",
        {"surface_grades": ("FC",), "max_run_count": 1},
    )
    chains = tuple(
        replace(
            chain,
            nodes=tuple(
                replace(
                    node,
                    rule_attributes={
                        "surface_grade": "FC" if node.node_id in {"a-tail", "b-tail"} else ""
                    },
                )
                for node in chain.nodes
            ),
        )
        for chain in state.current_plan.chains
    )
    state, context = search_case(chains, active=replace(active, rules=active.rules + (surface,)))
    assert state.current_evaluation.violations == ()
    before = fingerprint(state)
    assert not run_actual_recipe(state, context, (MOVE, 0, 1, 2, 3, 2, 2))
    assert_no_change(state, context, before)
    assert context.complete_candidate_evaluation_count == 1


def test_old_virtual_is_retained_and_its_own_move_recipe_is_rejected_after_debit():
    state, context = guard_case()
    old = bridge(state, context, purpose=VirtualPurpose.WEIGHT_FILL)
    a, b = state.current_plan.chains
    state.current_plan = SchedulePlan((replace(a, nodes=(a.first_node, old, *a.nodes[1:])), b))
    state.virtual_sequence = 1
    bind_rules(state, context, context.factory.cache.rule_set)
    before = fingerprint(state)
    assert not run_actual_recipe(state, context, (MOVE, 0, 1, 1, 2, 0, 0))
    assert_no_change(state, context, before)
    assert run_actual_recipe(state, context, (MOVE, 0, 1, 3, 4, 2, 2))
    assert nodes_by_id(state.current_plan)[old.node_id] is old
    assert state.virtual_sequence == 1


def test_moving_the_only_real_node_cannot_leave_a_virtual_only_chain():
    state, context = guard_case()
    a, b = state.current_plan.chains
    factory = context.factory
    problem = replace(factory.cache.problem, virtual_prototypes=(prototype("bridge", width="800"),))
    context.factory = replace(
        factory, cache=RuleEdgeDecisionCache(problem, factory.cache.rule_set, factory.cache.context)
    )
    old = bridge(state, context)
    state.current_plan = SchedulePlan(
        (
            replace(a, nodes=(a.last_node, old)),
            replace(b, nodes=(a.first_node, b.first_node, a.nodes[1], b.last_node)),
        )
    )
    state.virtual_sequence = 1
    bind_rules(state, context, context.factory.cache.rule_set)
    assert state.current_evaluation.violations == ()
    before = fingerprint(state)
    assert not run_actual_recipe(state, context, (MOVE, 0, 1, 0, 1, 0, 0))
    assert_no_change(state, context, before)
    assert context.complete_candidate_evaluation_count == 0


def split_case():
    periods = ("Z-first", "A-later")
    parent = replace(node("parent", width="1600", weight="1000"), source_period=periods[1])
    members = tuple(
        replace(node(name, width=str(width), weight=str(weight)), source_period=period)
        for name, width, weight, period in (
            ("b-head", 1500, 500, periods[0]),
            ("b-tail", 900, 300, periods[0]),
            ("a-middle", 1200, 500, periods[1]),
            ("a-tail", 800, 700, periods[1]),
            ("c-support", 1500, 200, periods[1]),
        )
    )
    _, base = width_case()
    state, context = search_case(
        (
            Chain("B", members[:2], periods[0]),
            Chain("A", (parent, *members[2:4]), periods[1]),
            Chain("C", members[4:], periods[1]),
        ),
        active=base.factory.cache.rule_set,
        periods=periods,
    )
    pieces = tuple(
        replace(
            parent,
            node_id=f"piece-{i}",
            weight=D(500),
            split_lineage=lineage(
                parent_node_id=parent.node_id,
                parent_source_order_id=parent.source_order_id,
                source_resource_id=parent.source_resource_id,
                source_period=periods[1],
                origin_assigned_period=periods[1],
                target_assigned_period=periods[1],
                parent_weight=parent.weight,
                piece_index=i,
            ),
        )
        for i in (1, 2)
    )
    b, a, c = state.current_plan.chains
    plan = SchedulePlan(
        (b, replace(a, nodes=(pieces[0], *a.nodes[1:])), replace(c, nodes=(pieces[1], *c.nodes)))
    )
    state = SearchState(
        plan,
        evaluate_plan(plan, context.factory.cache.rule_set, context.factory.cache.context),
        split_sequence=1,
        accepted_same_period_split_count=1,
    )
    assert state.current_evaluation.violations == ()
    return state, context


@pytest.mark.parametrize("target,accepted", ((0, False), (2, True)))
def test_existing_split_piece_uses_same_move_but_keeps_authorized_period(target, accepted):
    state, context = split_case()
    before = fingerprint(state)
    assert run_actual_recipe(state, context, (MOVE, 1, target, 0, 1, 0, 0)) is accepted
    assert state.split_sequence == state.accepted_same_period_split_count == 1
    assert state.accepted_future_borrow_return_count == 0
    if accepted:
        assert state.current_evaluation.violations == ()
        assert state.current_evaluation.quality_key[4] < 1500
    else:
        assert_no_change(state, context, before)


@pytest.mark.parametrize("kind", ("cancel", "error"))
def test_stop_or_error_inside_seam_build_never_publishes_partial_chain(monkeypatch, kind):
    state, context = period_case(connected=False)
    stop = Stop()
    context.factory.budget.cancellation = stop
    before = fingerprint(state)
    original = VirtualFactory.bridge

    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        if kind == "error":
            raise RuntimeError("deliberate seam failure")
        stop.active = True
        return result

    monkeypatch.setattr(VirtualFactory, "bridge", interrupted)
    if kind == "error":
        with pytest.raises(RuntimeError, match="deliberate seam failure"):
            run_actual_recipe(state, context, (MOVE, 0, 1, 1, 2, 1, 1))
    else:
        assert not run_actual_recipe(state, context, (MOVE, 0, 1, 1, 2, 1, 1))
        assert context.factory.budget.stop_reason is SearchStopReason.USER_CANCELLED
    assert context.factory.budget.candidate_check_count == 1
    assert_no_change(state, context, before)
    assert context.complete_candidate_evaluation_count == 0
