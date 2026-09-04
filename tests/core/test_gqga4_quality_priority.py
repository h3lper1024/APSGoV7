"""The actual GQGA4 declaration governs width, virtual weight, then chain count."""

from dataclasses import replace
from decimal import Decimal

import pytest

from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.chain_order import chain_order_objective_index
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, SchedulePlan, SearchState, VirtualPurpose
from apsgo_scheduler.core.neighborhoods import improve_chain_order, try_complete_candidate
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.app.test_input_normalizer import read_gqga4_spec
from tests.core.rules.test_inter_chain_width_gap import METRIC, with_gap
from tests.core.search.test_chain_order import chain, search_case, signature
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.test_quality_key import SeverityRule, evaluate, node, plan, ruleset
from tests.core.test_solver_orchestration import resign_problem

D = Decimal


@pytest.fixture(scope="module")
def active():
    # Keep the small synthetic rule set, but consume the real signed quality declaration.
    declared = load_rule_set(read_gqga4_spec("gqga4_rule_set_spec.json"))
    return replace(
        with_gap(ruleset()),
        quality_spec=declared.quality_spec,
        fingerprint="synthetic-gqga4-current-priority",
    )


def attempt(state, context, candidate, *, sequence=None):
    before = fingerprint(state)
    assert context.factory.budget.consume_candidate_check()
    accepted = try_complete_candidate(
        state,
        context,
        candidate,
        affected_chain_ids=tuple(item.chain_id for item in state.current_plan.chains),
        virtual_sequence=state.virtual_sequence if sequence is None else sequence,
        action_name="priority-regression",
    )
    assert context.complete_candidate_evaluation_count == 1
    assert state.accepted_move_count == len(context.accepted_move_traces) == int(accepted)
    if accepted:
        assert state.current_evaluation == evaluate_plan(
            state.current_plan, context.factory.cache.rule_set, context.factory.cache.context
        )
    else:
        assert fingerprint(state) == before
    return accepted


def add_prototype(context, left, right, width):
    item = prototype("p", width=str(width))
    cache = context.factory.cache
    context.factory = VirtualFactory(
        RuleEdgeDecisionCache(
            resign_problem(cache.problem, virtual_prototypes=(item,)),
            cache.rule_set,
            replace(cache.context, virtual_prototype_ids=(item.prototype_id,)),
        ),
        context.factory.budget,
    )
    return context.factory.materialize(
        item, left, right, purpose=VirtualPurpose.WEIGHT_FILL, sequence=1
    )


def test_formal_order_changes_only_comparison_positions_not_raw_metrics(active):
    assert tuple(item.metric_key for item in active.quality_spec) == (
        "prohibited_violation_count",
        "prohibited_violation_severity",
        "underweight_chain_count",
        "underweight_total_gap",
        METRIC,
        "generated_virtual_weight",
        "chain_count",
    )
    state, context = search_case(active=active)
    current = state.current_evaluation
    old_order = with_gap(ruleset())
    original = evaluate_plan(state.current_plan, old_order, context.factory.cache.context)
    assert dict(current.metrics) == dict(original.metrics)
    assert current.violations == original.violations
    assert current.chain_evaluations == original.chain_evaluations
    assert original.quality_key == (0, 0, 0, 0, 3, 0, 800)
    assert current.quality_key == (0, 0, 0, 0, 800, 0, 3)


@pytest.mark.parametrize("reverse", (False, True))
def test_width_gain_can_cost_a_chain_and_fewer_chains_cannot_buy_worse_width(active, reverse):
    a, b, c = (chain(name, width) for name, width in zip("ABC", (1000, 2000, 1900)))
    a = replace(a, nodes=(replace(a.first_node, weight=D(400)),))
    x = replace(chain("X", 2000).first_node, weight=D(400))
    a = replace(a, nodes=a.nodes + (x,))
    three = (a, b, c)
    two = (b, Chain("merged", a.nodes + c.nodes, "period"))
    initial, candidate = (two, three) if reverse else (three, two)
    state, context = search_case(initial, active=active)
    assert state.current_evaluation.quality_key == (
        (0, 0, 0, 0, 1000, 0, 2) if reverse else (0, 0, 0, 0, 100, 0, 3)
    )
    assert attempt(state, context, candidate) is reverse


def test_width_gain_can_accept_additional_authorized_virtual_weight(active):
    state, context = search_case((chain("A", 1000), chain("B", 1500)), active=active)
    a, b = state.current_plan.chains
    virtual = add_prototype(context, a.last_node, b.first_node, 1500)
    assert attempt(state, context, (replace(a, nodes=a.nodes + (virtual,)), b), sequence=1)
    trace = context.accepted_move_traces[0]
    assert trace.quality_before == (0, 0, 0, 0, 500, 0, 2)
    assert trace.quality_after == (0, 0, 0, 0, 0, 20, 2)


@pytest.mark.parametrize("remove_virtual", (False, True))
def test_equal_width_compares_virtual_weight_before_chain_count(active, remove_virtual):
    state, context = search_case(tuple(chain(name) for name in "ABC"), active=active)
    a, b, c = state.current_plan.chains
    virtual = add_prototype(context, a.last_node, b.first_node, 1000)
    two = (Chain("merged", a.nodes + (virtual,) + b.nodes, "period"), c)
    if remove_virtual:
        current = SchedulePlan(two)
        state = SearchState(
            current,
            evaluate_plan(current, active, context.factory.cache.context),
            virtual_sequence=1,
        )
    candidate = (a, b, c) if remove_virtual else two
    assert attempt(state, context, candidate, sequence=1) is remove_virtual
    if remove_virtual:
        trace = context.accepted_move_traces[0]
        assert trace.quality_before == (0, 0, 0, 0, 0, 20, 2)
        assert trace.quality_after == (0, 0, 0, 0, 0, 0, 3)


@pytest.mark.parametrize("reverse", (False, True))
def test_chain_count_only_breaks_ties_after_width_and_virtual_weight(active, reverse):
    a, b, c = (chain(name) for name in "ABC")
    three = (a, b, c)
    two = (Chain("merged", a.nodes + b.nodes, "period"), c)
    initial, candidate = (two, three) if reverse else (three, two)
    state, context = search_case(initial, active=active)
    assert state.current_evaluation.quality_key[:6] == (0, 0, 0, 0, 0, 0)
    assert attempt(state, context, candidate) is not reverse


@pytest.mark.parametrize("level", range(4))
def test_all_four_constraint_priorities_still_precede_width(active, level):
    active = replace(
        active,
        rules=active.rules
        + (SeverityRule("severity", "severity", RuleScope.CHAIN, True, "1", {}),),
    )
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
    )
    before, candidate = (
        evaluate(
            SchedulePlan(
                current.chains
                + (Chain("anchor", (replace(node("anchor", 700), width=D(width)),), "first"),)
            ),
            active,
        )
        for current, width in zip(cases[level], (1000, 1200))
    )
    assert before.quality_key[:level] == candidate.quality_key[:level]
    assert candidate.quality_key[level] < before.quality_key[level]
    assert candidate.quality_key[4] == 200 > before.quality_key[4] == 0
    assert candidate.quality_key < before.quality_key


def test_pure_chain_relocation_finds_width_objective_at_fifth_position(active):
    assert chain_order_objective_index(active) == 4
    state, context = search_case(active=active)
    original = {item.chain_id: item for item in state.current_plan.chains}
    improve_chain_order(state, context)
    assert signature(state.current_plan) == ("A", "C", "B")
    assert state.current_evaluation.quality_key == (0, 0, 0, 0, 500, 0, 3)
    assert {item.chain_id: item for item in state.current_plan.chains} == original
    assert tuple(
        (item.quality_before[4], item.quality_after[4]) for item in context.accepted_move_traces
    ) == ((800, 700), (700, 500))
    assert all(
        trace.quality_before[:4] == trace.quality_after[:4]
        and trace.quality_before[5:] == trace.quality_after[5:]
        for trace in context.accepted_move_traces
    )
