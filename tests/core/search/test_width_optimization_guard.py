"""Width-only acceptance preserves old nodes and validates new internal bridges."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import pytest

from apsgo_scheduler.core import neighborhoods
from apsgo_scheduler.core.chain_order import stable_group_plan
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import SchedulePlan, SearchState, VirtualPurpose
from apsgo_scheduler.core.neighborhoods import try_complete_candidate
from apsgo_scheduler.core.rules.base import ControlledSplitRuleSubject
from apsgo_scheduler.core.rules.concrete import ConsecutiveVirtualMaterialRule
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.graph.test_bipartite_matching import budget
from tests.core.rules.test_inter_chain_width_gap import METRIC, rule, with_gap
from tests.core.search.test_chain_order import chain, search_case
from tests.core.search.test_complete_candidate_lifecycle import (
    Stop,
    merged,
    period_case,
    setup,
)
from tests.core.search.test_virtual_material_factory import prototype
from tests.core.search.test_width_optimization_baseline import width_case
from tests.core.test_quality_key import ruleset

D = Decimal


def bind_rules(state, context, active):
    factory = context.factory
    context.factory = replace(
        factory,
        cache=RuleEdgeDecisionCache(factory.cache.problem, active, factory.cache.context),
    )
    state.current_evaluation = evaluate_plan(state.current_plan, active, factory.cache.context)


def guard_case(*, maximum=2, runtime=None):
    state, context = width_case()
    factory = context.factory
    catalog = (prototype("bridge", width="1400"),)
    problem = replace(factory.cache.problem, virtual_prototypes=catalog)
    evaluation_context = replace(factory.cache.context, virtual_prototype_ids=("bridge",))
    context.factory = VirtualFactory(
        RuleEdgeDecisionCache(problem, factory.cache.rule_set, evaluation_context),
        factory.budget if runtime is None else runtime,
    )
    context.policy = replace(
        context.policy,
        maximum_virtual_bridge_nodes=maximum,
        candidate_check_limit=context.factory.budget.candidate_check_limit,
    )
    return state, context


def bridge(state, context, *, sequence=None, purpose=VirtualPurpose.EDGE_BRIDGE, **changes):
    left, right = state.current_plan.chains[0].nodes[:2]
    return context.factory.materialize(
        context.factory.cache.problem.virtual_prototypes[0],
        left,
        right,
        purpose=purpose,
        sequence=state.virtual_sequence + 1 if sequence is None else sequence,
        **changes,
    )


def joint_candidate(state, extras=()):
    first, second = state.current_plan.chains
    prefix = replace(first, nodes=(first.nodes[0], *extras, *first.nodes[1:-1]))
    suffix = replace(first, chain_id="cut-suffix", nodes=first.nodes[-1:])
    return prefix, second, suffix


def attempt(state, context, candidate, **changes):
    options = (
        dict(
            affected_chain_ids=tuple(chain.chain_id for chain in state.current_plan.chains),
            virtual_sequence=state.virtual_sequence,
            action_name="width_guard_probe",
            width_optimization_only=True,
        )
        | changes
    )
    return try_complete_candidate(state, context, tuple(candidate), **options)


def assert_rejected(state, context, candidate, *, evaluations=0, **changes):
    before, traces = fingerprint(state), context.accepted_move_traces
    count = context.complete_candidate_evaluation_count
    assert not attempt(state, context, candidate, **changes)
    assert fingerprint(state) == before and context.accepted_move_traces == traces
    assert context.complete_candidate_evaluation_count == count + evaluations


@pytest.mark.parametrize("explicit_false", (False, True))
def test_legacy_default_and_explicit_false_keep_non_width_acceptance(explicit_false):
    state, context = setup()
    options = {"width_optimization_only": False} if explicit_false else {}
    assert try_complete_candidate(
        state,
        context,
        merged(state),
        affected_chain_ids=("c-0", "c-1"),
        virtual_sequence=0,
        action_name="legacy_merge",
        **options,
    )
    assert state.accepted_move_count == context.complete_candidate_evaluation_count == 1


@pytest.mark.parametrize("flag", (None, 0, 1, "true"))
def test_width_flag_requires_an_actual_bool(flag):
    state, context = guard_case()
    before = fingerprint(state)
    with pytest.raises(ValueError, match="width_optimization_only must be boolean"):
        attempt(state, context, joint_candidate(state), width_optimization_only=flag)
    assert fingerprint(state) == before
    assert context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize("mode", ("missing", "disabled", "diagnostic"))
def test_target_must_be_enabled_and_declared(mode):
    state, context = guard_case()
    active = ruleset()
    if mode != "missing":
        active = replace(active, rules=active.rules + (rule(enabled=mode != "disabled"),))
    bind_rules(state, context, active)
    assert_rejected(state, context, joint_candidate(state))


@pytest.mark.parametrize(
    "maximum,count,accepted",
    ((0, 0, True), (0, 1, False), (1, 1, True), (1, 2, False), (2, 2, True), (2, 3, False)),
)
def test_internal_bridge_limit_and_private_numbering(maximum, count, accepted):
    state, context = guard_case(maximum=maximum)
    extras = tuple(bridge(state, context, sequence=i) for i in range(1, count + 1))
    before = fingerprint(state)
    assert context.factory.budget.consume_candidate_check()
    assert (
        attempt(state, context, joint_candidate(state, extras), virtual_sequence=count) is accepted
    )
    if accepted:
        assert state.current_evaluation.violations == ()
        assert state.current_evaluation.quality_key == (0, D(0), 0, D(0), D(400), D(20 * count), 3)
        assert state.virtual_sequence == count
        assert state.split_sequence == state.accepted_same_period_split_count == 0
        assert state.accepted_future_borrow_return_count == 0
        assert context.complete_candidate_evaluation_count == 1
    else:
        assert fingerprint(state) == before and context.accepted_move_traces == ()
        assert context.complete_candidate_evaluation_count == 0
    assert context.factory.budget.candidate_check_count == 1


def test_double_bridge_is_reconstructed_with_the_same_two_old_anchors(monkeypatch):
    state, context = guard_case()
    extras = tuple(bridge(state, context, sequence=i) for i in (1, 2))
    original, anchors = VirtualFactory.materialize, []

    def record(factory, prototype, left, right, **options):
        anchors.append((left.node_id, right.node_id, options["sequence"]))
        return original(factory, prototype, left, right, **options)

    monkeypatch.setattr(VirtualFactory, "materialize", record)
    assert attempt(state, context, joint_candidate(state, extras), virtual_sequence=2)
    assert anchors == [("a-head", "a-middle", 1), ("a-head", "a-middle", 2)]


def test_multiple_interfaces_share_continuous_private_numbers_not_one_global_bridge_cap():
    state, context = guard_case()
    factory = context.factory
    catalog = (
        *factory.cache.problem.virtual_prototypes,
        prototype("upper", width="1300"),
        prototype("lower", width="1100"),
    )
    problem = replace(factory.cache.problem, virtual_prototypes=catalog)
    evaluation_context = replace(
        factory.cache.context, virtual_prototype_ids=tuple(item.prototype_id for item in catalog)
    )
    context.factory = replace(
        factory, cache=RuleEdgeDecisionCache(problem, factory.cache.rule_set, evaluation_context)
    )
    prefix, middle, suffix = joint_candidate(state, (bridge(state, context),))
    extras = tuple(
        context.factory.materialize(
            item,
            middle.first_node,
            middle.last_node,
            purpose=VirtualPurpose.EDGE_BRIDGE,
            sequence=sequence,
        )
        for item, sequence in zip(catalog[1:], (2, 3))
    )
    middle = replace(middle, nodes=(middle.first_node, *extras, middle.last_node))
    assert attempt(state, context, (prefix, middle, suffix), virtual_sequence=3)
    assert state.virtual_sequence == 3
    assert state.current_evaluation.quality_key == (0, D(0), 0, D(0), D(400), D(60), 3)


@pytest.mark.parametrize("purpose", tuple(VirtualPurpose))
@pytest.mark.parametrize("change", ("remove", "weight", "lineage"))
def test_every_old_virtual_purpose_must_be_preserved_unchanged(purpose, change):
    state, context = guard_case()
    options = (
        {"related_partition_id": "old-partition"}
        if purpose is VirtualPurpose.SPLIT_SEPARATOR
        else {}
    )
    old = bridge(state, context, purpose=purpose, **options)
    first, second = state.current_plan.chains
    state.current_plan = SchedulePlan(
        (replace(first, nodes=(first.nodes[0], old, *first.nodes[1:])), second)
    )
    state.virtual_sequence = 1
    bind_rules(state, context, context.factory.cache.rule_set)
    assert state.current_evaluation.violations == ()
    candidate = joint_candidate(state)
    kept = candidate[0]
    if change == "remove":
        nodes = tuple(node for node in kept.nodes if node.node_id != old.node_id)
    else:
        replacement = (
            replace(old, weight=D(21))
            if change == "weight"
            else replace(old, virtual_lineage=replace(old.virtual_lineage, accepted_sequence=7))
        )
        nodes = tuple(replacement if node.node_id == old.node_id else node for node in kept.nodes)
    assert_rejected(state, context, (replace(kept, nodes=nodes), *candidate[1:]))


def test_old_virtual_can_move_without_recomputing_its_original_temperature():
    state, context = guard_case()
    first, second = state.current_plan.chains
    head = replace(first.first_node, min_temperature=D(600), max_temperature=D(950))
    first = replace(first, nodes=(head, *first.nodes[1:]))
    factory = context.factory
    problem = replace(factory.cache.problem, nodes=(head, *factory.cache.problem.nodes[1:]))
    context.factory = replace(
        factory, cache=RuleEdgeDecisionCache(problem, factory.cache.rule_set, factory.cache.context)
    )
    state.current_plan = SchedulePlan((first, second))
    old = bridge(state, context)
    state.current_plan = SchedulePlan(
        (replace(first, nodes=(first.nodes[0], old, *first.nodes[1:])), second)
    )
    state.virtual_sequence = 1
    bind_rules(state, context, context.factory.cache.rule_set)
    prefix, middle, suffix = joint_candidate(state)
    prefix = replace(prefix, nodes=tuple(node for node in prefix.nodes if node != old))
    middle = replace(middle, nodes=(old, *middle.nodes))
    assert attempt(state, context, (prefix, middle, suffix))
    assert (
        next(node for node in state.current_plan.chains[1].nodes if node.node_id == old.node_id)
        is old
    )
    assert state.virtual_sequence == 1


@pytest.mark.parametrize(
    "change",
    (
        "fill",
        "separator",
        "partition",
        "prototype",
        "weight",
        "width",
        "thickness",
        "grade",
        "attributes",
        "temperature",
        "identity",
        "anchor",
    ),
)
def test_new_bridge_rejects_wrong_purpose_or_materialized_values(change):
    state, context = guard_case()
    item = bridge(state, context)
    if change in {"fill", "separator"}:
        purpose = VirtualPurpose.WEIGHT_FILL if change == "fill" else VirtualPurpose.SPLIT_SEPARATOR
        lineage = replace(
            item.virtual_lineage,
            purpose=purpose,
            related_partition_id=None if change == "fill" else "new-partition",
        )
        item = replace(item, virtual_lineage=lineage)
    elif change == "partition":
        # The value contract already refuses this forged edge-bridge association.
        with pytest.raises(ValueError, match="only split separators"):
            replace(item.virtual_lineage, related_partition_id="new-partition")
        return
    elif change == "prototype":
        item = replace(item, virtual_lineage=replace(item.virtual_lineage, prototype_id="foreign"))
    elif change == "anchor":
        left, right = state.current_plan.chains[0].nodes[:2]
        item = context.factory.materialize(
            context.factory.cache.problem.virtual_prototypes[0],
            replace(left, min_temperature=D(600)),
            right,
            purpose=VirtualPurpose.EDGE_BRIDGE,
            sequence=1,
        )
    else:
        fields = {
            "weight": {"weight": D(21)},
            "width": {"width": D(1401)},
            "thickness": {"thickness": D("1.1")},
            "grade": {"grade": "forged"},
            "attributes": {"rule_attributes": {"forged": True}},
            "temperature": {"max_temperature": D(901)},
            "identity": {"node_id": "forged-bridge"},
        }
        item = replace(item, **fields[change])
    assert_rejected(state, context, joint_candidate(state, (item,)), virtual_sequence=1)


@pytest.mark.parametrize("end", ("head", "tail"))
def test_new_bridge_cannot_be_an_outer_endpoint(end):
    state, context = guard_case()
    item = bridge(state, context)
    first, *rest = joint_candidate(state)
    nodes = (item, *first.nodes) if end == "head" else (*first.nodes, item)
    assert_rejected(state, context, (replace(first, nodes=nodes), *rest), virtual_sequence=1)


@pytest.mark.parametrize("numbers,cursor", (((2,), 2), ((1, 1), 2), ((1,), 2)))
def test_new_bridge_numbers_cannot_have_gaps_duplicates_or_unused_cursor(numbers, cursor):
    state, context = guard_case()
    extras = tuple(bridge(state, context, sequence=i) for i in numbers)
    if len(set(numbers)) != len(numbers):
        extras = (extras[0], replace(extras[1], node_id="different-id-same-sequence"))
    assert_rejected(state, context, joint_candidate(state, extras), virtual_sequence=cursor)


def test_existing_chain_and_node_identity_protections_still_apply():
    state, context = guard_case()
    candidate = joint_candidate(state)
    before = fingerprint(state)
    with pytest.raises(ValueError, match="absent from the current plan"):
        attempt(state, context, candidate, affected_chain_ids=("A", "cut-suffix"))
    assert fingerprint(state) == before
    assert_rejected(state, context, (*candidate, candidate[-1]))
    assert_rejected(state, context, (*candidate, replace(candidate[-1], chain_id="other")))
    assert_rejected(state, context, candidate, chain_order_only=True)


def test_pure_order_and_width_guards_can_be_enabled_together():
    state, context = search_case()
    a, b, c = state.current_plan.chains
    assert attempt(state, context, (a, c, b), chain_order_only=True)
    assert state.current_evaluation.metrics[METRIC] == D(500)


@pytest.mark.parametrize("change", ("reorder", "content"))
def test_unaffected_chain_order_and_content_are_still_protected(change):
    state, context = search_case()
    a, b, c = state.current_plan.chains
    candidate = (
        (a, c, b)
        if change == "reorder"
        else (a, replace(b, nodes=(replace(b.first_node, weight=D(801)),)), c)
    )
    assert_rejected(state, context, candidate, affected_chain_ids=("A",))


def test_width_must_improve_even_when_a_later_chain_count_decreases():
    active = with_gap(ruleset())
    old = active.quality_spec
    active = replace(active, quality_spec=(*old[:4], old[6], old[5], old[4]))
    state, context = search_case((chain("A"), chain("B")), active=active)
    a, b = state.current_plan.chains
    candidate = (replace(a, nodes=a.nodes + b.nodes),)
    evaluation = evaluate_plan(SchedulePlan(candidate), active, context.factory.cache.context)
    assert evaluation.quality_key < state.current_evaluation.quality_key
    assert evaluation.metrics[METRIC] == state.current_evaluation.metrics[METRIC] == 0
    assert_rejected(state, context, candidate, evaluations=1)


def test_prefix_must_be_equal_not_merely_better():
    state, context = search_case()
    a, b, c = state.current_plan.chains
    candidate = (replace(a, nodes=a.nodes + b.nodes), c)
    evaluation = evaluate_plan(
        SchedulePlan(candidate), context.factory.cache.rule_set, context.factory.cache.context
    )
    assert evaluation.violations == ()
    assert evaluation.quality_key < state.current_evaluation.quality_key
    assert evaluation.metrics[METRIC] < state.current_evaluation.metrics[METRIC]
    assert_rejected(state, context, candidate, evaluations=1)


@pytest.mark.parametrize("current_violates", (False, True))
def test_any_underweight_violation_is_forbidden_even_when_not_a_declared_objective(
    current_violates,
):
    state, context = guard_case()
    active = context.factory.cache.rule_set
    active = replace(active, quality_spec=(*active.quality_spec[:2], active.quality_spec[4]))
    original = state.current_plan
    a, b = original.chains
    head, tail = replace(a, nodes=a.nodes[:1]), replace(a, chain_id="short-cut", nodes=a.nodes[1:])
    if current_violates:
        state.current_plan = SchedulePlan((head, tail, b))
        candidate = original.chains
    else:
        candidate = (head, b, tail)
    bind_rules(state, context, active)
    evaluation = evaluate_plan(SchedulePlan(candidate), active, context.factory.cache.context)
    assert evaluation.quality_key < state.current_evaluation.quality_key
    assert bool(state.current_evaluation.violations) is current_violates
    assert bool(evaluation.violations) is not current_violates
    assert_rejected(state, context, candidate, evaluations=int(not current_violates))


def test_complete_chain_rules_still_reject_edge_legal_new_bridges():
    state, context = guard_case()
    cap = ConsecutiveVirtualMaterialRule("cap", "cap", RuleScope.CHAIN, True, "1", {"max_count": 1})
    active = context.factory.cache.rule_set
    bind_rules(state, context, replace(active, rules=active.rules + (cap,)))
    extras = tuple(bridge(state, context, sequence=i) for i in (1, 2))
    candidate = joint_candidate(state, extras)
    members = candidate[0].nodes
    assert all(
        context.factory.cache.allows(left, right) for left, right in zip(members, members[1:])
    )
    assert_rejected(state, context, candidate, virtual_sequence=2, evaluations=1)


def test_split_target_lock_and_split_authorization_remain_separate():
    state, context = period_case(split=True)
    state.current_plan = stable_group_plan(
        state.current_plan, context.factory.cache.context.period_index
    )
    bind_rules(state, context, with_gap(context.factory.cache.rule_set))
    assert state.current_evaluation.violations == ()
    assert_rejected(state, context, merged(state))

    state, context = guard_case()
    parent = state.current_plan.chains[0].nodes[0]
    subject = ControlledSplitRuleSubject(parent.node_id, parent, "period", "period", 0)
    decision = context.factory.cache.rule_set.evaluate_controlled_split(
        subject, context.factory.cache.context
    )
    assert_rejected(
        state, context, joint_candidate(state), split_subject=subject, split_decision=decision
    )


@pytest.mark.parametrize("when", ("before", "materialize", "evaluation", "trace"))
@pytest.mark.parametrize("kind", ("cancel", "time"))
def test_stop_at_each_new_candidate_boundary_keeps_state_and_numbers_private(
    monkeypatch, when, kind
):
    stop = Stop()
    runtime = budget(
        candidate_check_limit=10,
        cancellation=stop if kind == "cancel" else None,
        clock=stop.clock if kind == "time" else lambda: 1.0,
    )
    state, context = guard_case(runtime=runtime)
    candidate = joint_candidate(state, (bridge(state, context),))
    assert runtime.consume_candidate_check()
    if when == "before":
        stop.active = True
    else:
        owner, name = (
            (VirtualFactory, "materialize")
            if when == "materialize"
            else (
                neighborhoods,
                "_evaluate_candidate_plan" if when == "evaluation" else "AcceptedMoveTrace",
            )
        )
        original = getattr(owner, name)

        def interrupt(*args, **kwargs):
            result = original(*args, **kwargs)
            stop.active = True
            return result

        monkeypatch.setattr(owner, name, interrupt)
    assert_rejected(
        state,
        context,
        candidate,
        virtual_sequence=1,
        evaluations=int(when in {"evaluation", "trace"}),
    )
    assert runtime.candidate_check_count == 1
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if kind == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )


@pytest.mark.parametrize(
    "stage", ("materialize", "_evaluate_candidate_plan", "AcceptedMoveTrace", "commit_accepted")
)
def test_exceptions_before_commit_do_not_publish_partial_candidate(monkeypatch, stage):
    state, context = guard_case()
    candidate = joint_candidate(state, (bridge(state, context),))
    before = fingerprint(state)
    owner = (
        VirtualFactory
        if stage == "materialize"
        else SearchState
        if stage == "commit_accepted"
        else neighborhoods
    )
    error = RuntimeError("deliberate width candidate failure")
    monkeypatch.setattr(owner, stage, Mock(side_effect=error))
    assert context.factory.budget.consume_candidate_check()
    with pytest.raises(RuntimeError) as raised:
        attempt(state, context, candidate, virtual_sequence=1)
    assert raised.value is error
    assert fingerprint(state) == before and context.accepted_move_traces == ()
    assert context.complete_candidate_evaluation_count == int(stage != "materialize")
    assert context.factory.budget.candidate_check_count == 1


@pytest.mark.parametrize("reason", tuple(SearchStopReason))
def test_candidate_entry_never_resumes_a_stopped_budget(reason):
    state, context = guard_case()
    context.factory.budget.stop_reason = reason
    assert_rejected(state, context, joint_candidate(state))
    assert context.factory.budget.stop_reason is reason
