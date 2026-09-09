"""Rule-authorized splitting, fixed piece weights and private candidate lifecycle."""

from dataclasses import replace
from decimal import Decimal, Inexact, localcontext

import pytest

from apsgo_scheduler.core import controlled_split, neighborhoods
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import (
    ControlledSplitMode,
    RuleScope,
    SearchStopReason,
    fingerprint,
)
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    SchedulePlan,
    SchedulingProblem,
    SearchState,
    VirtualPurpose,
)
from apsgo_scheduler.core.neighborhoods import SearchContext
from apsgo_scheduler.core.rules.base import RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import (
    ConsecutiveVirtualMaterialRule,
    ContinuousNarrowSteelWeightRule,
    VirtualOutputRatioRule,
)
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet
from apsgo_scheduler.core.virtual_material import VirtualFactory
from tests.app.test_input_normalizer import make_request
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node, rules
from tests.core.rules.test_controlled_order_split import parameters, rule
from tests.core.search.test_single_real_node_relocation import Stop
from tests.core.search.test_virtual_material_factory import AllowedConnectionsRule, prototype

D = Decimal
SAME = ControlledSplitMode.SAME_PERIOD_SPLIT
FUTURE = ControlledSplitMode.FUTURE_BORROW_RETURN


def parent(name="parent", *, weight="120", source="P2", **changes):
    return replace(
        node(name, weight=weight, width="900"),
        **({"source_period": source, "rule_attributes": {"grade_class": "IF"}} | changes),
    )


def split_case(
    parents=None,
    *,
    origin="P2",
    anchor=False,
    rule_changes=None,
    enabled=True,
    prototypes=None,
    runtime=None,
    extra_rules=(),
    narrow_enabled=True,
):
    parents = (parent(),) if parents is None else tuple(parents)
    nodes = (
        (parent("anchor", weight="10", source="P9", grade="anchor"),) if anchor else ()
    ) + parents
    if anchor:
        nodes = (replace(nodes[0], rule_attributes={"grade_class": "OTHER"}), *nodes[1:])
    config = parameters(
        maximum_piece_weight=D(50),
        minimum_piece_weight=D(10),
        maximum_accepted_source_count=3,
        maximum_separator_node_count=3,
        maximum_separator_weight=D(20),
    ) | (rule_changes or {})
    items = (
        rule(enabled=enabled, parameters=config),
        ContinuousNarrowSteelWeightRule(
            "narrow",
            "narrow",
            RuleScope.CHAIN,
            narrow_enabled,
            "1",
            {"grade_class": "IF", "width_upper_exclusive": D(1000), "max_real_weight": D(50)},
        ),
        *extra_rules,
    )
    rule_set = replace(rules(), rules=items, fingerprint=fingerprint(items))
    catalog = (
        (prototype("p", weight="5", width="900"),) if prototypes is None else tuple(prototypes)
    )
    periods = ("P9", "P2", "P1")
    problem = SchedulingProblem(
        "split-test",
        rule_set.product_line_code,
        rule_set.process_code,
        rule_set.scenario,
        nodes,
        periods,
        catalog,
        fingerprint((nodes, periods, catalog)),
    )
    evaluation_context = RuleEvaluationContext(
        periods,
        {name: index for index, name in enumerate(periods)},
        tuple(item.prototype_id for item in catalog),
    )
    runtime = budget(candidate_check_limit=1000) if runtime is None else runtime
    factory = VirtualFactory(RuleEdgeDecisionCache(problem, rule_set, evaluation_context), runtime)
    policy = replace(make_request().policy, candidate_check_limit=runtime.candidate_check_limit)
    context = SearchContext(factory, policy)
    plan = SchedulePlan((Chain("donor", nodes, origin),))
    return SearchState(plan, evaluate_plan(plan, rule_set, evaluation_context)), context


def suppress_replay(monkeypatch):
    calls = []

    def record(state, context):
        calls.append((state, context))
        context.factory.budget.stop_reason = SearchStopReason.LOCAL_SEARCH_COMPLETE
        return state

    monkeypatch.setattr(controlled_split, "run_local_search", record)
    return calls


def capture_proposal(monkeypatch, **kwargs):
    """Stop at the real action's candidate boundary, without accepting its private values."""
    state, context = split_case(**kwargs)
    proposals = []

    def reject(current, current_context, chains, **options):
        assert current is state and current_context is context
        proposals.append((chains, options))
        return False

    with monkeypatch.context() as patch:
        patch.setattr(controlled_split, "try_complete_candidate", reject)
        controlled_split.run_controlled_order_split(state, context)
    assert len(proposals) == 1
    # Reopen only this isolated test boundary; the production driver owns real transitions.
    context.factory.budget.stop_reason = None
    return state, context, *proposals[0]


@pytest.mark.parametrize("origin,anchor,mode", (("P2", False, SAME), ("P9", True, FUTURE)))
def test_both_modes_use_one_complete_partition_and_recheck_rule_authorization(
    origin,
    anchor,
    mode,
    monkeypatch,
):
    state, context = split_case(origin=origin, anchor=anchor)
    original_parent = next(
        item for item in context.factory.cache.problem.nodes if item.node_id == "parent"
    )
    observed = []
    original = ProcessRuleSet.evaluate_controlled_split

    def record(rule_set, subject, evaluation_context):
        assert evaluation_context is context.factory.cache.context
        decision = original(rule_set, subject, evaluation_context)
        if subject.parent_node.node_id == "parent":
            observed.append((subject, decision, context.factory.budget.candidate_check_count))
        return decision

    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", record)
    replay = suppress_replay(monkeypatch)
    assert controlled_split.run_controlled_order_split(state, context) is state
    partition = state.current_plan.chains[-1]
    pieces = tuple(item for item in partition.nodes if item.split_lineage is not None)
    separators = tuple(item for item in partition.nodes if item.virtual_lineage is not None)
    assert tuple(item.weight for item in pieces) == (D(50), D(50), D(20))
    assert partition.assigned_period == "P2"
    assert len(state.current_plan.chains) == (2 if anchor else 1)
    assert len(separators) == 2
    assert all(
        item.virtual_lineage.purpose is VirtualPurpose.SPLIT_SEPARATOR for item in separators
    )
    assert tuple(item.virtual_lineage.accepted_sequence for item in separators) == (1, 2)
    assert len({item.split_lineage.partition_id for item in pieces}) == 1
    for index, piece in enumerate(pieces, start=1):
        lineage = piece.split_lineage
        assert lineage.piece_index == index and lineage.piece_count == 3
        assert lineage.split_mode is mode
        assert lineage.origin_assigned_period == origin
        assert lineage.target_assigned_period == lineage.source_period == "P2"
        assert lineage.accepted_source_sequence == 1
        assert (
            replace(
                piece,
                node_id=original_parent.node_id,
                weight=original_parent.weight,
                split_lineage=None,
            )
            == original_parent
        )
    assert observed[0][:2] == observed[1][:2]
    assert tuple(item[2] for item in observed) == (0, 1)
    assert (
        observed[0][1].decision_fingerprint
        == pieces[0].split_lineage.authorization_decision_fingerprint
    )
    assert state.split_sequence == 1 and state.virtual_sequence == 2
    assert state.accepted_same_period_split_count == int(mode is SAME)
    assert state.accepted_future_borrow_return_count == int(mode is FUTURE)
    assert state.accepted_move_count == context.complete_candidate_evaluation_count == 1
    assert context.factory.budget.candidate_check_count == 1
    assert state.current_evaluation.quality_key == (0, D(0))
    assert replay == [(state, context)]
    assert context.accepted_move_traces[0].action_name == "controlled_order_split"
    assert context.accepted_move_traces[0].affected_chain_ids == ("donor",)


@pytest.mark.parametrize(
    "case", ("disabled", "actual_transition", "not_narrow", "within_limit", "mode_disabled", "late")
)
def test_ineligible_parent_does_not_allocate_a_candidate_or_replay(case, monkeypatch):
    item = parent(
        weight="50" if case == "within_limit" else "120",
        source="P9" if case == "late" else "P2",
        material_role=MaterialRole.ACTUAL_TRANSITION
        if case == "actual_transition"
        else MaterialRole.NORMAL_REAL,
        rule_attributes={"grade_class": "OTHER" if case == "not_narrow" else "IF"},
    )
    state, context = split_case(
        (item,),
        enabled=case != "disabled",
        rule_changes={"allowed_modes": []} if case == "mode_disabled" else None,
    )
    before = fingerprint(state)
    replay = suppress_replay(monkeypatch)
    controlled_split.run_controlled_order_split(state, context)
    assert fingerprint(state) == before
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0
    assert context.factory.budget.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert replay == []


@pytest.mark.parametrize(
    "weight,expected",
    (
        ("100", ("50", "50")),
        ("110", ("50", "50", "10")),
        ("120.123456789012345678901", ("50", "50", "20.123456789012345678901")),
        ("109.999999", ()),
        ("1e1000", ()),
    ),
)
def test_exact_ceil_maximum_prefix_and_tail_before_allocating_bounded_separators(
    weight, expected, monkeypatch
):
    state, context = split_case((parent(weight=weight),))
    before = fingerprint(state)
    suppress_replay(monkeypatch)
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        decimal_context.traps[Inexact] = True
        controlled_split.run_controlled_order_split(state, context)
    if expected:
        pieces = tuple(item for item in state.current_plan.chains[-1].nodes if item.split_lineage)
        assert tuple(item.weight for item in pieces) == tuple(map(D, expected))
        assert state.split_sequence == 1
        assert context.factory.budget.candidate_check_count == 1
    else:
        assert fingerprint(state) == before
        assert context.factory.budget.candidate_check_count == 0
        assert context.complete_candidate_evaluation_count == 0


def test_separator_count_limit_is_checked_before_generating_any_separator(monkeypatch):
    state, context = split_case(rule_changes={"maximum_separator_node_count": 1})

    def forbidden(*args, **kwargs):
        raise AssertionError("count limit must be checked before separator generation")

    monkeypatch.setattr(VirtualFactory, "separator", forbidden)
    replay = suppress_replay(monkeypatch)
    before = fingerprint(state)
    controlled_split.run_controlled_order_split(state, context)
    assert fingerprint(state) == before and replay == []
    assert context.factory.budget.candidate_check_count == 0


@pytest.mark.parametrize("unit_weight,accepted", (("5", 1), ("5.0000005", 1), ("5.0000005005", 0)))
def test_separator_total_weight_epsilon_is_checked_before_candidate_count(
    unit_weight, accepted, monkeypatch
):
    state, context = split_case(
        prototypes=(prototype("p", weight=unit_weight),),
        rule_changes={"maximum_separator_weight": D(10)},
    )
    before = fingerprint(state)
    suppress_replay(monkeypatch)
    controlled_split.run_controlled_order_split(state, context)
    assert state.split_sequence == accepted
    assert context.factory.budget.candidate_check_count == accepted
    assert context.complete_candidate_evaluation_count == accepted
    if not accepted:
        assert fingerprint(state) == before


def test_failed_partial_separator_generation_reuses_private_numbers_for_next_parent(monkeypatch):
    state, context = split_case(
        (parent("first"), parent("second")),
        rule_changes={"maximum_accepted_source_count": 1},
    )
    original = VirtualFactory.separator
    observed = []

    def fail_second_separator(factory, left, right, **kwargs):
        observed.append((left.split_lineage.parent_node_id, kwargs["first_sequence"]))
        if left.split_lineage.parent_node_id == "first" and left.split_lineage.piece_index == 2:
            return None
        return original(factory, left, right, **kwargs)

    monkeypatch.setattr(VirtualFactory, "separator", fail_second_separator)
    replay = suppress_replay(monkeypatch)
    controlled_split.run_controlled_order_split(state, context)
    assert observed == [("first", 1), ("first", 2), ("second", 1), ("second", 2)]
    assert state.split_sequence == 1 and state.virtual_sequence == 2
    assert context.factory.budget.candidate_check_count == 1
    assert replay == [(state, context)]
    assert any(
        item.node_id == "first" for chain in state.current_plan.chains for item in chain.nodes
    )
    assert all(
        item.split_lineage.parent_node_id == "second"
        for item in state.current_plan.chains[-1].nodes
        if item.split_lineage
    )


@pytest.mark.parametrize("enabled", (False, True))
def test_enabled_virtual_ratio_is_a_precount_filter_not_a_hidden_default(enabled, monkeypatch):
    ratio = VirtualOutputRatioRule(
        "ratio", "ratio", RuleScope.PLAN, enabled, "1", {"max_ratio": D("0.01")}
    )
    state, context = split_case(extra_rules=(ratio,))
    before = fingerprint(state)
    suppress_replay(monkeypatch)
    controlled_split.run_controlled_order_split(state, context)
    assert state.split_sequence == int(not enabled)
    assert context.factory.budget.candidate_check_count == int(not enabled)
    assert context.complete_candidate_evaluation_count == int(not enabled)
    if enabled:
        assert fingerprint(state) == before


def test_equal_complete_candidate_is_rejected_without_replay_or_number_commit(monkeypatch):
    state, context = split_case(narrow_enabled=False)
    before = fingerprint(state)
    replay = suppress_replay(monkeypatch)
    controlled_split.run_controlled_order_split(state, context)
    assert (
        context.factory.budget.candidate_check_count
        == context.complete_candidate_evaluation_count
        == 1
    )
    assert fingerprint(state) == before and replay == []


def test_parent_removal_cannot_drop_a_nonbridge_virtual_only_remainder(monkeypatch):
    state, context = split_case()
    original = state.current_plan.chains[0].nodes[0]
    virtual = context.factory.materialize(
        context.factory.cache.problem.virtual_prototypes[0],
        original,
        original,
        purpose=VirtualPurpose.WEIGHT_FILL,
        sequence=1,
    )
    plan = SchedulePlan((replace(state.current_plan.chains[0], nodes=(original, virtual)),))
    state = replace(
        state,
        current_plan=plan,
        virtual_sequence=1,
        current_evaluation=evaluate_plan(
            plan, context.factory.cache.rule_set, context.factory.cache.context
        ),
    )
    before = fingerprint(state)
    replay = suppress_replay(monkeypatch)
    controlled_split.run_controlled_order_split(state, context)
    assert fingerprint(state) == before and replay == []
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0


def test_parent_removal_discards_adjacent_edge_bridges_before_split(monkeypatch):
    cap = ConsecutiveVirtualMaterialRule(
        "virtual-run", "virtual-run", RuleScope.CHAIN, True, "1", {"max_count": 2}
    )
    state, context = split_case(anchor=True, extra_rules=(cap,))
    anchor, original = state.current_plan.chains[0].nodes
    virtuals = tuple(
        context.factory.materialize(
            context.factory.cache.problem.virtual_prototypes[0],
            original,
            original,
            purpose=VirtualPurpose.EDGE_BRIDGE,
            sequence=sequence,
        )
        for sequence in range(1, 5)
    )
    plan = SchedulePlan(
        (
            replace(
                state.current_plan.chains[0],
                nodes=(anchor,) + virtuals[:2] + (original,) + virtuals[2:],
            ),
        )
    )
    state = replace(
        state,
        current_plan=plan,
        virtual_sequence=4,
        current_evaluation=evaluate_plan(
            plan, context.factory.cache.rule_set, context.factory.cache.context
        ),
    )
    replay = suppress_replay(monkeypatch)

    controlled_split.run_controlled_order_split(state, context)

    assert state.split_sequence == 1
    assert state.virtual_sequence == 6
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 1
    assert state.current_plan.chains[0].nodes == (anchor,)
    assert not {node.node_id for node in virtuals} & {
        node.node_id for chain in state.current_plan.chains for node in chain.nodes
    }
    assert replay == [(state, context)]


def test_parent_removal_rebuilds_middle_boundary_with_fresh_edge_bridge(monkeypatch):
    left = parent(
        "left", weight="10", grade="left", rule_attributes={"grade_class": "OTHER"}
    )
    original = parent(grade="parent")
    right = parent(
        "right", weight="10", grade="right", rule_attributes={"grade_class": "OTHER"}
    )
    allowed = {
        ("left", "p"),
        ("p", "p"),
        ("p", "parent"),
        ("parent", "p"),
        ("p", "right"),
    }
    cap = ConsecutiveVirtualMaterialRule(
        "virtual-run", "virtual-run", RuleScope.CHAIN, True, "1", {"max_count": 2}
    )
    edge = AllowedConnectionsRule(
        "connections",
        "connections",
        RuleScope.EDGE,
        True,
        "1",
        {"allowed_edges": tuple(sorted(allowed))},
    )
    state, context = split_case(
        (left, original, right),
        extra_rules=(cap, edge),
        prototypes=(prototype("p", weight="5"),),
    )
    virtuals = tuple(
        context.factory.materialize(
            context.factory.cache.problem.virtual_prototypes[0],
            left if sequence < 3 else original,
            original if sequence < 3 else right,
            purpose=VirtualPurpose.EDGE_BRIDGE,
            sequence=sequence,
        )
        for sequence in range(1, 5)
    )
    plan = SchedulePlan(
        (
            replace(
                state.current_plan.chains[0],
                nodes=(left,) + virtuals[:2] + (original,) + virtuals[2:] + (right,),
            ),
        )
    )
    state = replace(
        state,
        current_plan=plan,
        virtual_sequence=4,
        current_evaluation=evaluate_plan(
            plan, context.factory.cache.rule_set, context.factory.cache.context
        ),
    )
    replay = suppress_replay(monkeypatch)

    controlled_split.run_controlled_order_split(state, context)

    donor, returned = state.current_plan.chains
    assert (donor.nodes[0], donor.nodes[-1]) == (left, right)
    assert len(donor.nodes) == 3
    bridge = donor.nodes[1]
    assert bridge.virtual_lineage.purpose is VirtualPurpose.EDGE_BRIDGE
    assert bridge.virtual_lineage.accepted_sequence == 5
    assert tuple(
        node.virtual_lineage.accepted_sequence
        for node in returned.nodes
        if node.virtual_lineage is not None
    ) == (6, 7)
    assert not {node.node_id for node in virtuals} & {
        node.node_id for chain in state.current_plan.chains for node in chain.nodes
    }
    assert state.virtual_sequence == 7 and state.split_sequence == 1
    assert state.current_evaluation.quality_key == (0, D(0))
    assert replay == [(state, context)]


def test_split_is_rejected_when_the_remaining_donor_boundary_cannot_be_rebuilt(monkeypatch):
    left = parent("left", weight="10", rule_attributes={"grade_class": "OTHER"})
    original = parent()
    right = parent("right", weight="10", rule_attributes={"grade_class": "OTHER"})
    state, context = split_case((left, original, right))
    before = fingerprint(state)
    replay = suppress_replay(monkeypatch)
    monkeypatch.setattr(VirtualFactory, "bridge", lambda *args, **kwargs: None)

    controlled_split.run_controlled_order_split(state, context)

    assert fingerprint(state) == before and replay == []
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0


def test_existing_pieces_and_generated_separators_are_not_recursive_split_parents(monkeypatch):
    state, context = split_case()
    replay = suppress_replay(monkeypatch)
    controlled_split.run_controlled_order_split(state, context)
    before = fingerprint(state)

    def forbidden(*args):
        raise AssertionError(
            "already split pieces and generated material must not seek authorization"
        )

    monkeypatch.setattr(ProcessRuleSet, "evaluate_controlled_split", forbidden)
    controlled_split.run_controlled_order_split(state, context)
    assert fingerprint(state) == before and replay == [(state, context)]
    assert context.factory.budget.candidate_check_count == 1
    assert context.complete_candidate_evaluation_count == 1


def test_split_piece_identity_prefix_avoids_original_input_id_namespace(monkeypatch):
    state, context = split_case((parent("split-existing"),))
    suppress_replay(monkeypatch)
    controlled_split.run_controlled_order_split(state, context)
    pieces = tuple(item for item in state.current_plan.chains[-1].nodes if item.split_lineage)
    assert len(pieces) == 3
    assert all(item.node_id.startswith("_split-partition-") for item in pieces)
    assert len({item.node_id for item in pieces}) == 3
    assert all(item.node_id != "split-existing" for item in pieces)


def test_zero_allowance_cannot_publish_the_prepared_private_partition(monkeypatch):
    state, context = split_case(runtime=budget(candidate_check_limit=0))
    before = fingerprint(state)
    replay = suppress_replay(monkeypatch)
    calls = []
    original = VirtualFactory.separator

    def record(factory, left, right, **kwargs):
        calls.append((kwargs["first_sequence"], factory.budget.candidate_check_count))
        return original(factory, left, right, **kwargs)

    monkeypatch.setattr(VirtualFactory, "separator", record)
    controlled_split.run_controlled_order_split(state, context)
    assert calls == [(1, 0), (2, 0)]
    assert fingerprint(state) == before and replay == []
    assert context.factory.budget.stop_reason is SearchStopReason.CANDIDATE_LIMIT_REACHED
    assert context.factory.budget.candidate_check_count == 0
    assert context.complete_candidate_evaluation_count == 0


@pytest.mark.parametrize("stop_kind", ("cancel", "time"))
@pytest.mark.parametrize("when", ("before", "authorization", "separator", "evaluation"))
def test_interrupted_split_never_commits_half_partition_or_replays(stop_kind, when, monkeypatch):
    stop = Stop()
    runtime = budget(
        candidate_check_limit=10,
        stop_reason=SearchStopReason.LOCAL_SEARCH_COMPLETE,
        cancellation=stop if stop_kind == "cancel" else None,
        clock=lambda: 100.0 if stop.active and stop_kind == "time" else 1.0,
    )
    state, context = split_case(runtime=runtime)
    before = fingerprint(state)
    replay = suppress_replay(monkeypatch)
    if when == "before":
        stop.active = True
    else:
        owner, attribute = {
            "authorization": (ProcessRuleSet, "evaluate_controlled_split"),
            "separator": (VirtualFactory, "separator"),
            "evaluation": (neighborhoods, "_evaluate_candidate_plan"),
        }[when]
        original = getattr(owner, attribute)

        def stop_after(*args, **kwargs):
            result = original(*args, **kwargs)
            stop.active = True
            return result

        monkeypatch.setattr(owner, attribute, stop_after)
    controlled_split.run_controlled_order_split(state, context)
    assert runtime.stop_reason is (
        SearchStopReason.USER_CANCELLED
        if stop_kind == "cancel"
        else SearchStopReason.SEARCH_TIME_LIMIT_REACHED
    )
    assert runtime.candidate_check_count == int(when == "evaluation")
    assert context.complete_candidate_evaluation_count == int(when == "evaluation")
    assert fingerprint(state) == before and replay == []


@pytest.mark.parametrize("where", ("authorization", "separator", "evaluation"))
def test_split_errors_propagate_without_natural_completion_or_state_changes(where, monkeypatch):
    state, context = split_case(
        runtime=budget(candidate_check_limit=10, stop_reason=SearchStopReason.LOCAL_SEARCH_COMPLETE)
    )
    before = fingerprint(state)
    replay = suppress_replay(monkeypatch)
    owner, attribute = {
        "authorization": (ProcessRuleSet, "evaluate_controlled_split"),
        "separator": (VirtualFactory, "separator"),
        "evaluation": (neighborhoods, "_evaluate_candidate_plan"),
    }[where]

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic split failure")

    monkeypatch.setattr(owner, attribute, fail)
    with pytest.raises(RuntimeError, match="synthetic split failure"):
        controlled_split.run_controlled_order_split(state, context)
    assert context.factory.budget.stop_reason is None
    assert context.factory.budget.candidate_check_count == int(where == "evaluation")
    assert context.complete_candidate_evaluation_count == int(where == "evaluation")
    assert fingerprint(state) == before and replay == []
