"""Candidate-private virtual numbering and interruption cannot mutate accepted state."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from unittest.mock import Mock

import pytest

from apsgo_scheduler.core import virtual_material
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import SearchStopReason, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import Chain, SchedulePlan, SearchState, VirtualPurpose
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.search.test_virtual_material_factory import make_factory, prototype


def single_factory(**changes):
    return make_factory(
        (prototype("discarded"), prototype("selected")),
        allowed_edges={("left", "selected"), ("selected", "right")},
        **changes,
    )


def state_for(factory):
    plan = SchedulePlan((Chain("chain", factory.cache.problem.nodes, "period"),))
    return SearchState(plan, evaluate_plan(plan, factory.cache.rule_set, factory.cache.context))


def materialize(factory, *, sequence=1, **changes):
    left, right = factory.cache.problem.nodes
    options = {"purpose": VirtualPurpose.EDGE_BRIDGE, "sequence": sequence} | changes
    return factory.materialize(factory.cache.problem.virtual_prototypes[-1], left, right, **options)


@pytest.mark.parametrize("discarded_count", (0, 1, 4))
def test_discarded_candidates_and_failed_selections_do_not_advance_accepted_numbering(
    discarded_count,
):
    factory = single_factory()
    state = state_for(factory)
    left, right = factory.cache.problem.nodes
    before = fingerprint(state)
    first_sequence = state.virtual_sequence + 1
    for _ in range(discarded_count):
        factory.materialize(
            factory.cache.problem.virtual_prototypes[0],
            left,
            right,
            purpose=VirtualPurpose.EDGE_BRIDGE,
            sequence=first_sequence,
        )
        assert factory.bridge(left, right, max_nodes=0, first_sequence=first_sequence) is None
    selected = factory.bridge(left, right, max_nodes=2, first_sequence=first_sequence)
    assert selected is not None and len(selected) == 1
    assert selected[0].node_id == "virtual-000001"
    assert selected[0].virtual_lineage.accepted_sequence == 1
    assert selected == (materialize(factory),)
    assert fingerprint(selected) == fingerprint((materialize(single_factory()),))
    assert fingerprint(state) == before
    assert state.virtual_sequence == state.accepted_move_count == 0
    assert factory.budget.candidate_check_count == 0 and factory.budget.stop_reason is None


def test_two_bridge_boundaries_in_one_candidate_use_a_private_continuous_cursor():
    factory = make_factory(
        (prototype("p"), prototype("q")),
        allowed_edges={("left", "p"), ("p", "q"), ("q", "right")},
    )
    left, right = factory.cache.problem.nodes
    first = factory.bridge(left, right, max_nodes=2, first_sequence=4)
    second = factory.bridge(left, right, max_nodes=2, first_sequence=4 + len(first))
    assert tuple(item.node_id for item in first + second) == tuple(
        f"virtual-{sequence:06d}" for sequence in (4, 5, 6, 7)
    )
    assert tuple(item.virtual_lineage.accepted_sequence for item in first + second) == (4, 5, 6, 7)
    assert tuple(item.virtual_lineage.prototype_id for item in first + second) == (
        "p",
        "q",
        "p",
        "q",
    )
    assert factory.bridge(left, right, max_nodes=2, first_sequence=4) == first
    assert factory.budget.candidate_check_count == 0


def test_only_existing_accepted_state_commit_advances_the_numbering_cursor():
    factory = make_factory((prototype("fill"),))
    state = state_for(factory)
    left, right = factory.cache.problem.nodes
    first = materialize(
        factory, purpose=VirtualPurpose.WEIGHT_FILL, sequence=state.virtual_sequence + 1
    )
    assert state.virtual_sequence == 0
    plan = SchedulePlan((Chain("chain", (left, first, right), "period"),))
    evaluation = evaluate_plan(plan, factory.cache.rule_set, factory.cache.context)
    # The caller has approved this commit; acceptance/search policy is a later function.
    state.commit_accepted(plan, evaluation, virtual_sequence=1)
    assert state.current_plan is plan and state.current_evaluation is evaluation
    assert state.virtual_sequence == state.accepted_move_count == 1
    before = fingerprint(state)
    for _ in range(2):
        materialize(factory, sequence=state.virtual_sequence + 1)
    assert fingerprint(state) == before
    second = materialize(factory, purpose=VirtualPurpose.WEIGHT_FILL, sequence=2)
    plan = SchedulePlan((Chain("chain", (left, first, second, right), "period"),))
    state.commit_accepted(
        plan,
        evaluate_plan(plan, factory.cache.rule_set, factory.cache.context),
        virtual_sequence=2,
    )
    third = materialize(factory, sequence=state.virtual_sequence + 1)
    assert (first.node_id, second.node_id, third.node_id) == (
        "virtual-000001",
        "virtual-000002",
        "virtual-000003",
    )
    assert state.virtual_sequence == state.accepted_move_count == 2
    assert state.split_sequence == 0


@pytest.mark.parametrize(
    "names,prefix",
    (
        (("left", "right"), "virtual-"),
        (("virtual-000001", "right"), "_virtual-"),
        (("virtual-order", "_virtual-order"), "__virtual-"),
    ),
)
def test_input_identity_collisions_change_only_the_stable_prefix_not_the_sequence(names, prefix):
    factory = make_factory((prototype("p"),), nodes=tuple(node(name) for name in names))
    selected = tuple(materialize(factory, sequence=sequence) for sequence in (1, 2))
    assert tuple(item.node_id for item in selected) == (f"{prefix}000001", f"{prefix}000002")
    assert not {item.node_id for item in selected} & set(names)
    assert tuple(item.virtual_lineage.accepted_sequence for item in selected) == (1, 2)
    assert materialize(factory) == selected[0]


def test_factory_is_immutable_and_other_instances_do_not_share_numbering_state():
    first, second = single_factory(), single_factory()
    expected = materialize(second, sequence=9)
    materialize(first, sequence=100)
    assert materialize(first, sequence=9) == expected
    assert materialize(second, sequence=9) == expected
    with pytest.raises(FrozenInstanceError):
        first.cache = second.cache
    with pytest.raises(FrozenInstanceError):
        first.budget = second.budget
    with pytest.raises(FrozenInstanceError):
        expected.node_id = "changed"


def test_pure_materialization_does_not_probe_or_reset_an_already_stopped_budget():
    token = Mock()
    token.is_cancelled.side_effect = AssertionError("pure materialization does not poll")
    runtime = budget(cancellation=token, stop_reason=SearchStopReason.USER_CANCELLED)
    factory = single_factory(runtime=runtime)
    result = materialize(factory)
    assert result.node_id == "virtual-000001"
    token.is_cancelled.assert_not_called()
    assert runtime.stop_reason is SearchStopReason.USER_CANCELLED
    assert runtime.candidate_check_count == 0


@pytest.mark.parametrize("invalid", ("cache", "budget"))
def test_factory_requires_its_existing_cache_and_runtime_budget(invalid):
    factory = single_factory()
    with pytest.raises(ValueError):
        VirtualFactory(
            None if invalid == "cache" else factory.cache,
            None if invalid == "budget" else factory.budget,
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"sequence": 0},
        {"sequence": -1},
        {"sequence": True},
        {"sequence": "1"},
        {"purpose": "edge_bridge"},
        {"related_partition_id": "partition"},
        {"purpose": VirtualPurpose.SPLIT_SEPARATOR, "related_partition_id": None},
    ),
)
def test_materialization_rejects_invalid_number_or_purpose_metadata(changes):
    factory = single_factory()
    with pytest.raises(ValueError):
        materialize(factory, **changes)
    assert factory.budget.stop_reason is None and factory.budget.candidate_check_count == 0


@pytest.mark.parametrize(
    "invalid", ("foreign_prototype", "altered_prototype", "prototype_type", "left", "right")
)
def test_materialization_requires_an_exact_catalog_prototype_and_typed_anchors(invalid):
    factory = single_factory()
    selected = factory.cache.problem.virtual_prototypes[-1]
    left, right = factory.cache.problem.nodes
    if invalid == "foreign_prototype":
        selected = prototype("foreign")
    elif invalid == "altered_prototype":
        selected = replace(selected, unit_weight=Decimal(21))
    elif invalid == "prototype_type":
        selected = None
    elif invalid == "left":
        left = None
    else:
        right = "node"
    with pytest.raises(ValueError):
        factory.materialize(selected, left, right, purpose=VirtualPurpose.EDGE_BRIDGE, sequence=1)


def test_equal_prototype_value_does_not_require_the_same_object_address():
    factory = single_factory()
    selected = factory.cache.problem.virtual_prototypes[-1]
    left, right = factory.cache.problem.nodes
    copied = replace(selected)
    assert copied is not selected
    assert factory.materialize(
        copied, left, right, purpose=VirtualPurpose.EDGE_BRIDGE, sequence=1
    ) == materialize(factory)


@pytest.mark.parametrize(
    "changes",
    (
        {"max_nodes": -1},
        {"max_nodes": 3},
        {"max_nodes": True},
        {"max_nodes": "2"},
        {"first_sequence": 0},
        {"first_sequence": True},
    ),
)
def test_bridge_rejects_invalid_limits_or_sequence(changes):
    factory = single_factory()
    left, right = factory.cache.problem.nodes
    options = {"max_nodes": 2, "first_sequence": 1} | changes
    with pytest.raises(ValueError):
        factory.bridge(left, right, **options)


@pytest.mark.parametrize("partition", (None, " "))
def test_separator_requires_explicit_nonempty_partition_identity(partition):
    factory = single_factory()
    left, right = factory.cache.problem.nodes
    with pytest.raises(ValueError):
        factory.separator(left, right, first_sequence=1, related_partition_id=partition)


class Stop:
    active = False

    def is_cancelled(self):
        return self.active

    def clock(self):
        return 100.0 if self.active else 1.0


def select(factory, method):
    left, right = factory.cache.problem.nodes
    if method == "separator":
        return factory.separator(left, right, first_sequence=1, related_partition_id="partition")
    return factory.bridge(left, right, max_nodes=2, first_sequence=1)


@pytest.mark.parametrize("method", ("bridge", "separator"))
@pytest.mark.parametrize("kind", ("cancel", "time"))
def test_stopped_selection_returns_none_and_consumes_no_candidate_checks(method, kind):
    signal = Stop()
    signal.active = True
    runtime = budget(cancellation=signal) if kind == "cancel" else budget(clock=signal.clock)
    factory = single_factory(runtime=runtime)
    assert select(factory, method) is None
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if kind == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )
    assert runtime.candidate_check_count == 0


@pytest.mark.parametrize(
    "method,stage",
    (
        ("bridge", "materialize"),
        ("bridge", "cache"),
        ("bridge", "return"),
        ("separator", "profile"),
        ("separator", "return"),
        ("double", "return"),
    ),
)
@pytest.mark.parametrize("kind", ("cancel", "time"))
def test_interrupted_selection_never_leaks_its_best_so_far(monkeypatch, method, stage, kind):
    signal = Stop()
    runtime = budget(cancellation=signal) if kind == "cancel" else budget(clock=signal.clock)
    allowed = (
        {("left", "p"), ("p", "q"), ("q", "right")}
        if method == "double"
        else {("left", "p"), ("p", "right"), ("left", "q"), ("q", "right")}
    )
    factory = make_factory(
        (prototype("p", width="1200"), prototype("q")),
        allowed_edges=allowed,
        runtime=runtime,
    )
    state = state_for(factory)
    before = fingerprint(state)
    owner, attribute = {
        "materialize": (VirtualFactory, "materialize"),
        "cache": (RuleEdgeDecisionCache, "allows"),
        "profile": (virtual_material, "quick_chain_prohibited_profile"),
        "return": (virtual_material, "virtual_smoothness"),
    }[stage]
    original = getattr(owner, attribute)
    calls = 0

    def interrupt_after(*args, **kwargs):
        nonlocal calls
        value = original(*args, **kwargs)
        calls += 1
        # Single candidate p has already been considered before q is interrupted.
        # At "return", stop after the last ranking computation, including a double bridge.
        if stage == "materialize":
            signal.active = value.virtual_lineage.prototype_id == "q"
        elif stage == "cache":
            signal.active = args[-1].grade == "q"
        else:
            signal.active = calls == 2
        return value

    monkeypatch.setattr(owner, attribute, interrupt_after)
    assert select(factory, method) is None
    assert signal.active
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if kind == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )
    assert runtime.candidate_check_count == 0
    assert fingerprint(state) == before


@pytest.mark.parametrize("stage", ("materialize", "cache", "smoothness", "profile"))
def test_selection_errors_propagate_without_mutating_inputs_or_accepted_state(monkeypatch, stage):
    factory = single_factory()
    state = state_for(factory)
    before = fingerprint(state), fingerprint(factory.cache.problem)
    owner, attribute = {
        "materialize": (VirtualFactory, "materialize"),
        "cache": (RuleEdgeDecisionCache, "allows"),
        "smoothness": (virtual_material, "virtual_smoothness"),
        "profile": (virtual_material, "quick_chain_prohibited_profile"),
    }[stage]
    error = RuntimeError("deliberate virtual selection failure")
    monkeypatch.setattr(owner, attribute, Mock(side_effect=error))
    with pytest.raises(RuntimeError) as raised:
        select(factory, "separator" if stage == "profile" else "bridge")
    assert raised.value is error
    assert factory.budget.stop_reason is None and factory.budget.candidate_check_count == 0
    assert before == (fingerprint(state), fingerprint(factory.cache.problem))
