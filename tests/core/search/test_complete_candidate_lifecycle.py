"""Complete candidate isolation, authoritative evaluation and atomic accepted state."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from unittest.mock import Mock

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    SearchStopReason,
    SolverPolicy,
    fingerprint,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    SchedulePlan,
    SearchState,
    VirtualPurpose,
)
from apsgo_scheduler.core.neighborhoods import SearchContext, try_complete_candidate
from apsgo_scheduler.core.rules.base import RuleEvaluationContext
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.search.test_virtual_material_factory import make_factory, prototype
from tests.core.test_model_contracts import lineage
from tests.core.test_reference_numeric_projection import criterion


def setup(groups=(("z",), ("a",), ("u",)), *, runtime=None):
    nodes = tuple(node(name) for group in groups for name in group)
    runtime = budget(candidate_check_limit=10) if runtime is None else runtime
    factory = make_factory((prototype("p"),), nodes=nodes, runtime=runtime)
    rules = replace(
        factory.cache.rule_set,
        quality_spec=(*factory.cache.rule_set.quality_spec, criterion("chain_count")),
        fingerprint="lifecycle-rules",
    )
    factory = replace(
        factory, cache=RuleEdgeDecisionCache(factory.cache.problem, rules, factory.cache.context)
    )
    policy = SolverPolicy(
        1,
        Decimal(110),
        Decimal(10),
        runtime.candidate_check_limit,
        CONSTRUCTION_ORDER_KEY,
        NUMERIC_SEMANTICS_KEY,
        Decimal(40),
    )
    by_id = {item.node_id: item for item in nodes}
    plan = SchedulePlan(
        tuple(
            Chain(f"c-{index}", tuple(by_id[name] for name in group), "period")
            for index, group in enumerate(groups)
        )
    )
    return SearchState(plan, evaluate_plan(plan, rules, factory.cache.context)), SearchContext(
        factory, policy
    )


def merged(state, *extra):
    first, second, *remaining = state.current_plan.chains
    return (*remaining, Chain("merged", first.nodes + tuple(extra) + second.nodes, "period"))


def attempt(state, context, chains, **changes):
    options = (
        dict(
            affected_chain_ids=tuple(chain.chain_id for chain in state.current_plan.chains[:2]),
            virtual_sequence=state.virtual_sequence,
            action_name="whole_chain_merge",
        )
        | changes
    )
    return try_complete_candidate(state, context, chains, **options)


def assert_unchanged(state, context, before):
    assert fingerprint(state) == before
    assert context.accepted_move_traces == ()


def test_acceptance_reuses_unaffected_chain_and_atomically_records_complete_trace():
    state, context = setup()
    original = state.current_plan
    before_quality = state.current_evaluation.quality_key
    candidate = merged(state)
    assert context.factory.budget.consume_candidate_check()
    assert attempt(state, context, candidate)
    assert state.current_plan.chains[0] is original.chains[2]
    assert (
        state.current_plan.chains[-1].nodes == original.chains[0].nodes + original.chains[1].nodes
    )
    assert state.current_evaluation == evaluate_plan(
        state.current_plan, context.factory.cache.rule_set, context.factory.cache.context
    )
    assert state.accepted_move_count == 1 and state.virtual_sequence == state.split_sequence == 0
    assert context.complete_candidate_evaluation_count == 1
    (trace,) = context.accepted_move_traces
    assert trace.sequence == 1
    assert trace.action_name == "whole_chain_merge"
    assert trace.affected_chain_ids == ("c-0", "c-1")
    assert trace.affected_source_order_ids == ("z", "a")
    assert trace.quality_before == before_quality
    assert trace.quality_after == state.current_evaluation.quality_key < before_quality
    assert trace.candidate_check_count == context.factory.budget.candidate_check_count == 1
    with pytest.raises(FrozenInstanceError):
        state.current_plan.chains = ()
    with pytest.raises(FrozenInstanceError):
        trace.sequence = 2
    assert original.chains[0].nodes[0].node_id == "z"


@pytest.mark.parametrize("worse", (False, True))
def test_equal_or_worse_complete_candidates_are_evaluated_but_never_committed(worse):
    state, context = setup(groups=(("z", "a"), ("u",)))
    before = fingerprint(state)
    if worse:
        first, second = state.current_plan.chains
        chains = (
            Chain("left", (first.nodes[0],), "period"),
            Chain("right", (first.nodes[1],), "period"),
            second,
        )
    else:
        chains = tuple(reversed(state.current_plan.chains))
    assert context.factory.budget.consume_candidate_check()
    assert not attempt(state, context, chains)
    assert_unchanged(state, context, before)
    assert context.complete_candidate_evaluation_count == 1
    assert context.factory.budget.candidate_check_count == 1


@pytest.mark.parametrize(
    "invalid",
    (
        "missing",
        "extra",
        "weight",
        "attributes",
        "role",
        "duplicate",
        "undeclared_change",
        "undeclared_removed",
    ),
)
def test_ineligible_coverage_or_value_changes_are_rejected_before_full_evaluation(
    monkeypatch, invalid
):
    state, context = setup()
    first, second, untouched = state.current_plan.chains
    original_nodes = first.nodes + second.nodes
    if invalid == "missing":
        nodes = first.nodes
    elif invalid == "extra":
        nodes = original_nodes + (node("extra"),)
    elif invalid == "weight":
        nodes = (replace(first.nodes[0], weight=Decimal(21)), *second.nodes)
    elif invalid == "attributes":
        nodes = (replace(first.nodes[0], rule_attributes={"changed": True}), *second.nodes)
    elif invalid == "role":
        nodes = (
            replace(first.nodes[0], material_role=MaterialRole.ACTUAL_TRANSITION),
            *second.nodes,
        )
    else:
        nodes = original_nodes
    chains = (untouched, Chain("merged", nodes, "period"))
    if invalid == "duplicate":
        chains += (first,)
    elif invalid == "undeclared_change":
        chains = (replace(untouched, assigned_period="changed"), chains[-1])
    elif invalid == "undeclared_removed":
        chains = (replace(chains[-1], nodes=nodes + untouched.nodes),)
    before = fingerprint(state)
    evaluation = Mock(side_effect=AssertionError("ineligible candidates are not evaluated"))
    monkeypatch.setattr(neighborhoods, "evaluate_plan", evaluation)
    assert not attempt(state, context, chains)
    assert_unchanged(state, context, before)
    assert context.complete_candidate_evaluation_count == 0
    evaluation.assert_not_called()


def test_unchanged_chains_cannot_be_reordered_without_declaring_them_affected():
    state, context = setup(groups=(("z",), ("a",), ("u",), ("v",)))
    before = fingerprint(state)
    first, second, joined = merged(state)
    assert not attempt(state, context, (second, first, joined))
    assert_unchanged(state, context, before)
    assert context.complete_candidate_evaluation_count == 0


def period_case(*, other_period="z-early", split=False):
    state, old_context = setup()
    old_factory = old_context.factory
    periods = ("z-early", "a-late")
    parent, other, unchanged = old_factory.cache.problem.nodes
    nodes = (
        replace(parent, source_period="a-late"),
        replace(other, source_period=other_period),
        replace(unchanged, source_period="a-late"),
    )
    problem = replace(
        old_factory.cache.problem,
        nodes=nodes,
        period_order=periods,
        input_fingerprint="period-input",
    )
    evaluation_context = RuleEvaluationContext(
        periods, {name: i for i, name in enumerate(periods)}, ("p",)
    )
    factory = VirtualFactory(
        RuleEdgeDecisionCache(problem, old_factory.cache.rule_set, evaluation_context),
        old_factory.budget,
    )
    context = SearchContext(factory, old_context.policy)
    parent, other, unchanged = nodes
    if split:
        pieces = tuple(
            replace(
                parent,
                node_id=f"piece-{index}",
                weight=Decimal(10),
                split_lineage=lineage(
                    parent_node_id=parent.node_id,
                    parent_source_order_id=parent.source_order_id,
                    source_resource_id=parent.source_resource_id,
                    source_period="a-late",
                    origin_assigned_period="a-late",
                    target_assigned_period="a-late",
                    parent_weight=parent.weight,
                    piece_index=index,
                ),
            )
            for index in (1, 2)
        )
    else:
        pieces = (parent,)
    plan = SchedulePlan(
        (
            Chain("c-0", pieces, "a-late"),
            Chain("c-1", (other,), other_period),
            Chain("c-2", (unchanged,), "a-late"),
        )
    )
    state = SearchState(
        plan,
        evaluate_plan(plan, factory.cache.rule_set, evaluation_context),
        split_sequence=int(split),
        accepted_same_period_split_count=int(split),
    )
    return state, context


def test_only_affected_periods_are_normalized_to_the_earliest_task_period():
    state, context = period_case()
    untouched = state.current_plan.chains[2]
    candidate = merged(state)
    candidate = (candidate[0], replace(candidate[1], assigned_period="caller-placeholder"))
    assert attempt(state, context, candidate)
    assert state.current_plan.chains[0] is untouched
    assert tuple(chain.assigned_period for chain in state.current_plan.chains) == (
        "a-late",
        "z-early",
    )
    assert candidate[1].assigned_period == "caller-placeholder"


@pytest.mark.parametrize("other_period,accepted", (("z-early", False), ("a-late", True)))
def test_existing_split_fragments_stay_in_the_authorized_target_after_normalization(
    other_period, accepted
):
    state, context = period_case(other_period=other_period, split=True)
    before = fingerprint(state)
    pieces = state.current_plan.chains[0].nodes
    assert attempt(state, context, merged(state)) is accepted
    assert state.split_sequence == state.accepted_same_period_split_count == 1
    if accepted:
        assert state.current_plan.chains[-1].assigned_period == "a-late"
        assert state.current_plan.chains[-1].nodes[:2] == pieces
        assert context.accepted_move_traces[0].affected_source_order_ids == ("z", "a")
        assert context.complete_candidate_evaluation_count == 1
    else:
        assert_unchanged(state, context, before)
        assert context.complete_candidate_evaluation_count == 0


def test_existing_split_fragment_values_cannot_be_rewritten_by_an_ordinary_candidate():
    state, context = period_case(other_period="a-late", split=True)
    before = fingerprint(state)
    candidate = merged(state)
    chain = candidate[-1]
    rewritten = replace(chain.nodes[0], weight=Decimal(11))
    candidate = (*candidate[:-1], replace(chain, nodes=(rewritten, *chain.nodes[1:])))
    assert not attempt(state, context, candidate)
    assert_unchanged(state, context, before)
    assert context.complete_candidate_evaluation_count == 0


def proposed_virtual(state, context, sequence=None):
    left = state.current_plan.chains[0].nodes[0]
    right = state.current_plan.chains[1].nodes[0]
    return context.factory.materialize(
        context.factory.cache.problem.virtual_prototypes[0],
        left,
        right,
        purpose=VirtualPurpose.EDGE_BRIDGE,
        sequence=state.virtual_sequence + 1 if sequence is None else sequence,
    )


def test_rejected_virtual_candidate_does_not_consume_number_then_acceptance_commits_it():
    state, context = setup()
    virtual = proposed_virtual(state, context)
    before = fingerprint(state)
    chains = list(state.current_plan.chains)
    chains[0] = replace(chains[0], nodes=chains[0].nodes + (virtual,))
    assert not attempt(state, context, tuple(chains), virtual_sequence=1)
    assert_unchanged(state, context, before)
    assert state.virtual_sequence == 0
    assert attempt(state, context, merged(state, virtual), virtual_sequence=1)
    assert state.virtual_sequence == state.accepted_move_count == 1
    assert context.complete_candidate_evaluation_count == 2
    assert len(context.accepted_move_traces) == 1
    assert (
        next(
            node
            for chain in state.current_plan.chains
            for node in chain.nodes
            if node.virtual_lineage
        ).node_id
        == "virtual-000001"
    )


@pytest.mark.parametrize("invalid", ("gap", "unused_cursor", "foreign_prototype"))
def test_new_virtual_nodes_require_continuous_numbers_and_bound_prototypes(invalid):
    state, context = setup()
    before = fingerprint(state)
    virtual = proposed_virtual(state, context, sequence=2 if invalid == "gap" else 1)
    if invalid == "foreign_prototype":
        virtual = replace(
            virtual, virtual_lineage=replace(virtual.virtual_lineage, prototype_id="foreign")
        )
    cursor = 2 if invalid in ("gap", "unused_cursor") else 1
    assert not attempt(state, context, merged(state, virtual), virtual_sequence=cursor)
    assert_unchanged(state, context, before)
    assert context.complete_candidate_evaluation_count == 0


def test_retained_virtual_node_values_cannot_be_rewritten():
    state, context = setup()
    virtual = proposed_virtual(state, context)
    assert attempt(state, context, merged(state, virtual), virtual_sequence=1)
    before, traces = fingerprint(state), context.accepted_move_traces
    candidate = merged(state)
    changed = tuple(
        replace(node, weight=Decimal(21)) if node.virtual_lineage else node
        for node in candidate[-1].nodes
    )
    assert not attempt(state, context, (replace(candidate[-1], nodes=changed),))
    assert fingerprint(state) == before and context.accepted_move_traces == traces
    assert context.complete_candidate_evaluation_count == 1


class Stop:
    active = False

    def is_cancelled(self):
        return self.active

    def clock(self):
        return 100.0 if self.active else 1.0


@pytest.mark.parametrize("after_evaluation", (False, True))
@pytest.mark.parametrize("kind", ("cancel", "time"))
def test_stop_before_or_after_full_evaluation_keeps_plan_number_and_trace_uncommitted(
    monkeypatch, after_evaluation, kind
):
    signal = Stop()
    runtime = (
        budget(candidate_check_limit=10, cancellation=signal)
        if kind == "cancel"
        else budget(candidate_check_limit=10, clock=signal.clock)
    )
    state, context = setup(runtime=runtime)
    candidate = merged(state, proposed_virtual(state, context))
    before = fingerprint(state)
    assert runtime.consume_candidate_check()
    original = neighborhoods.evaluate_plan
    calls = 0

    def evaluate_then_stop(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        signal.active = True
        return result

    monkeypatch.setattr(neighborhoods, "evaluate_plan", evaluate_then_stop)
    signal.active = not after_evaluation
    assert not attempt(state, context, candidate, virtual_sequence=1)
    assert_unchanged(state, context, before)
    assert calls == context.complete_candidate_evaluation_count == int(after_evaluation)
    assert runtime.candidate_check_count == 1
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if kind == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )


@pytest.mark.parametrize("stage", ("evaluate_plan", "AcceptedMoveTrace", "commit_accepted"))
def test_evaluation_trace_or_commit_errors_do_not_publish_a_partial_acceptance(monkeypatch, stage):
    state, context = setup()
    before = fingerprint(state)
    candidate = merged(state)
    owner = SearchState if stage == "commit_accepted" else neighborhoods
    error = RuntimeError("deliberate candidate failure")
    monkeypatch.setattr(owner, stage, Mock(side_effect=error))
    assert context.factory.budget.consume_candidate_check()
    with pytest.raises(RuntimeError) as raised:
        attempt(state, context, candidate)
    assert raised.value is error
    assert_unchanged(state, context, before)
    assert context.complete_candidate_evaluation_count == 1
    assert context.factory.budget.candidate_check_count == 1
    assert context.factory.budget.stop_reason is None


@pytest.mark.parametrize(
    "changes",
    (
        {"virtual_sequence": -1},
        {"virtual_sequence": True},
        {"action_name": " "},
        {"affected_chain_ids": ("unknown",)},
    ),
)
def test_invalid_candidate_call_metadata_fails_without_changing_accepted_state(changes):
    state, context = setup()
    before = fingerprint(state)
    with pytest.raises(ValueError):
        attempt(state, context, merged(state), **changes)
    assert_unchanged(state, context, before)
    assert context.complete_candidate_evaluation_count == 0
