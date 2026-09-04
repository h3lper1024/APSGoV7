"""Domain identity, immutable values and accepted-only sequence boundaries."""

from dataclasses import FrozenInstanceError, asdict, fields, replace
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from apsgo_scheduler.core.contracts import (
    AuditedCoreRelease,
    ControlledSplitMode,
    CoreCandidateSnapshot,
    fingerprint,
    sum_decimals,
    sum_weights,
)
from apsgo_scheduler.core.evaluation import PlanEvaluation
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    SchedulingProblem,
    SearchState,
    SplitLineage,
    VirtualLineage,
    VirtualMaterialPrototype,
    VirtualPurpose,
)
from apsgo_scheduler.core.resource_facts import (
    ActualTransitionFact,
    FutureBorrowFact,
    NodeAssignmentFact,
    PlanDerivedFacts,
    SplitPartitionFact,
    VirtualGenerationFact,
    derive_evaluation_resource_view,
)
from apsgo_scheduler.core.rules.base import RuleEvaluationContext

D = Decimal


def node(**changes):
    values = dict(
        node_id="order-node",
        source_order_id="order",
        source_resource_id="resource",
        source_period="p1",
        weight=D("10"),
        width=D("1000"),
        thickness=D("0.8"),
        min_temperature=D("600"),
        max_temperature=D("680"),
        grade="steel",
        material_role=MaterialRole.NORMAL_REAL,
        rule_attributes={"hardness": "soft"},
    )
    return Node(**(values | changes))


def lineage(**changes):
    values = dict(
        partition_id="partition",
        parent_node_id="order-node",
        parent_source_order_id="order",
        source_resource_id="resource",
        source_period="p1",
        origin_assigned_period="p1",
        split_mode=ControlledSplitMode.SAME_PERIOD_SPLIT,
        target_assigned_period="p1",
        accepted_source_sequence=1,
        parent_weight=D("10"),
        piece_index=1,
        piece_count=2,
        authorization_rule_id="split",
        authorization_rule_version="v1",
        authorization_decision_fingerprint="decision",
        reason_code="authorized_split",
    )
    return SplitLineage(**(values | changes))


def virtual(**changes):
    return node(
        **(
            dict(
                node_id="virtual",
                source_order_id=None,
                source_resource_id=None,
                source_period=None,
                material_role=MaterialRole.GENERATED_VIRTUAL,
                virtual_lineage=VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, 1),
            )
            | changes
        )
    )


def partition(**changes):
    values = asdict(lineage())
    values.pop("piece_index")
    values.pop("piece_count")
    values.update(
        partition_fingerprint="partition-hash",
        piece_node_ids=("piece-1", "piece-2"),
        piece_weights=(D("6"), D("4")),
        separator_virtual_node_ids=("separator",),
    )
    return SplitPartitionFact(**(values | changes))


def test_node_attributes_and_collections_are_copied_and_frozen():
    attributes = {"flag": True, "rank": 1, "limit": D("2"), "missing": None}
    real = node(rule_attributes=attributes)
    attributes["rank"] = 100
    assert real.rule_attributes["rank"] == 1
    with pytest.raises(TypeError):
        real.rule_attributes["rank"] = 2
    with pytest.raises(FrozenInstanceError):
        real.weight = D("99")
    original_nodes = [real, virtual()]
    chain = Chain("chain", original_nodes, "p1")
    original_nodes.clear()
    assert len(chain.nodes) == 2
    assert (
        chain.first_node is real and chain.last_node.material_role is MaterialRole.GENERATED_VIRTUAL
    )
    assert chain.total_weight == chain.real_weight + chain.virtual_weight == D("20")
    assert (chain.real_node_count, chain.virtual_node_count, chain.split_piece_count) == (1, 1, 0)
    assert not hasattr(chain, "__dict__")


@pytest.mark.parametrize(
    "changes",
    [
        {"node_id": " "},
        {"source_order_id": None},
        {"source_resource_id": ""},
        {"source_period": None},
        {"weight": D(0)},
        {"weight": 10.0},
        {"width": D("NaN")},
        {"weight": D("Infinity")},
        {"thickness": D("-1")},
        {"min_temperature": D("700")},
        {"rule_attributes": {"nested": {"a": 1}}},
        {"rule_attributes": {"values": []}},
        {"rule_attributes": {"value": 1.5}},
        {"rule_attributes": {"weight": D("15")}},
        {"rule_attributes": {"v": D("NaN")}},
        {"material_role": "normal_real"},
        {"virtual_lineage": VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, 1)},
    ],
)
def test_invalid_node_facts_are_rejected(changes):
    with pytest.raises(ValueError):
        node(**changes)


def test_material_roles_and_split_source_identity():
    assert node(material_role=MaterialRole.ACTUAL_TRANSITION).source_order_id == "order"
    piece = node(node_id="piece-1", weight=D("6"), split_lineage=lineage())
    assert piece.source_resource_id == node().source_resource_id
    for changes in (
        {"material_role": MaterialRole.ACTUAL_TRANSITION},
        {"source_order_id": "other"},
        {"source_resource_id": "other"},
        {"source_period": "p2"},
        {"node_id": "order-node"},
        {"weight": D("10")},
    ):
        with pytest.raises(ValueError):
            replace(piece, **changes)
    for changes in (
        {"source_order_id": "order"},
        {"virtual_lineage": None},
        {"split_lineage": lineage()},
    ):
        with pytest.raises(ValueError):
            virtual(**changes)


@pytest.mark.parametrize(
    "purpose,partition_id,sequence",
    [
        (VirtualPurpose.SPLIT_SEPARATOR, None, 1),
        (VirtualPurpose.EDGE_BRIDGE, "partition", 1),
        (VirtualPurpose.WEIGHT_FILL, None, 0),
        (VirtualPurpose.WEIGHT_FILL, None, True),
    ],
)
def test_virtual_lineage_requires_valid_purpose_partition_and_sequence(
    purpose, partition_id, sequence
):
    with pytest.raises(ValueError):
        VirtualLineage("prototype", purpose, partition_id, sequence)


@pytest.mark.parametrize(
    "changes",
    [
        {"piece_index": 0},
        {"piece_index": 3},
        {"piece_count": 1},
        {"accepted_source_sequence": 0},
        {"accepted_source_sequence": True},
        {"parent_node_id": ""},
        {"reason_code": ""},
        {"parent_weight": D("NaN")},
        {"target_assigned_period": "p2"},
        {"origin_assigned_period": "p0"},
        {"split_mode": ControlledSplitMode.FUTURE_BORROW_RETURN},
    ],
)
def test_split_lineage_rejects_invalid_partition_metadata(changes):
    with pytest.raises(ValueError):
        lineage(**changes)


def test_future_return_mode_records_original_and_authorized_target():
    split = lineage(
        source_period="p2",
        origin_assigned_period="p1",
        target_assigned_period="p2",
        split_mode=ControlledSplitMode.FUTURE_BORROW_RETURN,
    )
    assert split.target_assigned_period == split.source_period != split.origin_assigned_period


def test_problem_preserves_order_and_only_accepts_unsplit_real_input():
    real = node()
    prototype = VirtualMaterialPrototype("proto", D("5"), None, None, None, None, "", {})
    problem = SchedulingProblem(
        "problem", "line", "process", "month", [real], ["p2", "p1"], [prototype], "input-hash"
    )
    assert problem.period_order == ("p2", "p1")
    assert problem.nodes == (real,) and not hasattr(prototype, "node_id")
    for changes in (
        {"nodes": ()},
        {"nodes": (virtual(),)},
        {"nodes": (real, real)},
        {"period_order": ()},
        {"period_order": ("p1", "p1")},
        {"period_order": ("other",)},
        {"virtual_prototypes": (prototype, prototype)},
        {"input_fingerprint": ""},
        {"nodes": (node(node_id="piece-1", weight=D("6"), split_lineage=lineage()),)},
    ):
        with pytest.raises(ValueError):
            replace(problem, **changes)


def test_plan_structure_rejects_empty_pure_virtual_and_duplicate_nodes():
    real = node()
    chain = Chain("chain", (real,), "p1")
    with pytest.raises(ValueError):
        Chain("empty", (), "p1")
    with pytest.raises(ValueError):
        Chain("virtual-only", (virtual(),), "p1")
    with pytest.raises(ValueError):
        SchedulePlan(())
    with pytest.raises(ValueError):
        SchedulePlan((chain, chain))
    with pytest.raises(ValueError):
        SchedulePlan((chain, replace(chain, chain_id="another")))


def test_accepted_state_commit_validates_before_changing_any_sequence():
    original = SchedulePlan((Chain("chain", (node(),), "p1"),))
    changed = SchedulePlan((Chain("next-chain", (node(),), "p1"),))
    before_evaluation = PlanEvaluation((), (), {}, ())
    next_evaluation = PlanEvaluation((), (), {}, ())
    state = SearchState(original, before_evaluation)
    candidate = replace(
        state, current_plan=changed, current_evaluation=next_evaluation, virtual_sequence=2
    )
    assert candidate.virtual_sequence == 2 and state.virtual_sequence == 0
    assert state.current_plan is original  # A discarded candidate has no effects.
    state.commit_accepted(
        changed,
        next_evaluation,
        virtual_sequence=2,
        split_mode=ControlledSplitMode.SAME_PERIOD_SPLIT,
    )
    assert state.current_plan is changed and state.current_evaluation is next_evaluation
    assert (
        state.accepted_move_count,
        state.split_sequence,
        state.accepted_same_period_split_count,
        state.accepted_future_borrow_return_count,
    ) == (1, 1, 1, 0)
    snapshot = replace(state)
    with pytest.raises(ValueError):
        state.commit_accepted(original, before_evaluation, virtual_sequence=1)
    assert state == snapshot
    with pytest.raises(ValueError):
        replace(state, split_sequence=10)
    for bad_plan, bad_evaluation in ((object(), next_evaluation), (changed, object())):
        with pytest.raises(ValueError):
            state.commit_accepted(
                bad_plan,
                bad_evaluation,
                virtual_sequence=3,
                split_mode=ControlledSplitMode.SAME_PERIOD_SPLIT,
            )
        assert state == snapshot
    state.commit_accepted(
        original,
        before_evaluation,
        virtual_sequence=2,
        split_mode=ControlledSplitMode.FUTURE_BORROW_RETURN,
    )
    assert (
        state.split_sequence,
        state.accepted_same_period_split_count,
        state.accepted_future_borrow_return_count,
    ) == (2, 1, 1)
    assert not hasattr(state, "global_best")


@pytest.mark.parametrize(
    "changes",
    [
        {"piece_weights": (D("6"), D("3"))},
        {"piece_weights": (D("10"),)},
        {"piece_node_ids": ("piece-1", "piece-1")},
        {"piece_node_ids": ("order-node", "piece-2")},
        {"separator_virtual_node_ids": ("piece-1",)},
        {"accepted_source_sequence": 0},
        {"target_assigned_period": "other"},
    ],
)
def test_partition_fact_rejects_invalid_identity_and_conservation(changes):
    with pytest.raises(ValueError):
        partition(**changes)


def test_partition_fingerprint_payload_covers_all_semantics_without_cycle():
    fact = partition()
    payload = dict(fact.fingerprint_payload())
    assert set(payload) == {field.name for field in fields(fact)} - {"partition_fingerprint"}
    assert (
        replace(fact, partition_fingerprint="different").fingerprint_payload()
        == fact.fingerprint_payload()
    )
    for changes in (
        {"reason_code": "different"},
        {"accepted_source_sequence": 2},
        {
            "source_period": "p2",
            "target_assigned_period": "p2",
            "split_mode": ControlledSplitMode.FUTURE_BORROW_RETURN,
        },
        {"piece_node_ids": ("piece-2", "piece-1")},
        {"separator_virtual_node_ids": ("other-separator",)},
    ):
        assert replace(fact, **changes).fingerprint_payload() != fact.fingerprint_payload()
    assert "result_fingerprint" not in payload


def test_weight_conservation_and_chain_totals_do_not_inherit_rounding_context():
    large = D("9999999999999999999999999999")
    small = D("1.1")
    with localcontext() as context:
        context.prec = 3
        with pytest.raises(ValueError, match="conserve"):
            partition(
                parent_weight=D("10000000000000000000000000000"), piece_weights=(large, small)
            )
        exact = D("10000000000000000000000000000.1")
        valid = partition(parent_weight=exact, piece_weights=(large, small))
        chain = Chain("large-chain", (node(weight=large), virtual(weight=small)), "p1")
        assert valid.parent_weight == chain.total_weight == exact
        assert chain.real_weight == large and chain.virtual_weight == small


def test_resource_fact_shapes_are_immutable_and_release_preserves_identity():
    assignment = NodeAssignmentFact(
        "node", "order", "resource", MaterialRole.NORMAL_REAL, "chain", "p1", 0, D("10")
    )
    transition = ActualTransitionFact("transition", "order-t", "resource-t", "chain", "p1", D("2"))
    borrow = FutureBorrowFact("node-b", "order-b", "resource-b", "p2", "p1", D("4"))
    generation = VirtualGenerationFact(
        "separator", "proto", VirtualPurpose.SPLIT_SEPARATOR, "partition", 1, "chain", "p1", D("1")
    )
    assignments = [assignment]
    facts = PlanDerivedFacts(
        assignments,
        [borrow],
        [generation],
        [partition()],
        [transition],
        D("10"),
        D("10"),
        D("1"),
        D("4"),
        D("4"),
        "resource-facts",
    )
    assignments.clear()
    assert facts.assignments == (assignment,)
    with pytest.raises(FrozenInstanceError):
        facts.generated_virtual_weight = D("8")
    plan = SchedulePlan((Chain("chain", (node(),), "p1"),))
    release = AuditedCoreRelease(
        plan,
        PlanEvaluation((), (), {}, ()),
        facts,
        "plan",
        "evaluation",
        "resource-facts",
        "audit",
        "release",
    )
    assert release.resource_facts is facts
    with pytest.raises(ValueError):
        replace(release, resource_fingerprint="another")
    for changes in (
        {"canonical_plan": object()},
        {"audited_evaluation": object()},
        {"audited_evaluation": None},
        {"resource_facts": object()},
    ):
        with pytest.raises(ValueError):
            replace(release, **changes)
    with pytest.raises(ValueError):
        replace(assignment, position=-1)
    with pytest.raises(ValueError):
        replace(borrow, source_period="p1")
    with pytest.raises(ValueError):
        replace(generation, related_partition_id=None)


@pytest.mark.parametrize("invalid", (None, object(), {"quality_key": ()}))
def test_search_and_candidate_require_real_plan_evaluation(invalid):
    plan = SchedulePlan((Chain("chain", (node(),), "p1"),))
    with pytest.raises(ValueError, match="PlanEvaluation"):
        SearchState(plan, invalid)
    with pytest.raises(ValueError, match="PlanEvaluation"):
        CoreCandidateSnapshot(plan, invalid)
    evaluation = PlanEvaluation((), (), {}, ())
    assert CoreCandidateSnapshot(plan, evaluation).search_evaluation is evaluation
    with pytest.raises(ValueError):
        CoreCandidateSnapshot(invalid, evaluation)


def test_signed_decimal_sum_preserves_cancellation_and_weight_contract():
    values = (D("9999999999999999999999999999"), D("-9999999999999999999999999999"), D("-0.001"))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = context.traps[Rounded] = True
        assert sum_decimals(iter(values)) == D("-0.001")
        assert sum_decimals(reversed(values)) == D("-0.001")
        assert sum_decimals(()) == sum_weights(()) == D(0)
        with pytest.raises(ValueError):
            sum_weights(values)
    for invalid in (None, 1, 1.0, D("NaN"), D("Infinity")):
        with pytest.raises(ValueError):
            sum_decimals((invalid,))


def test_resource_view_reads_actual_periods_roles_pieces_and_preserves_order():
    periods = ("p3", "p1", "p2")
    context = RuleEvaluationContext(
        periods, dict(zip(periods, range(len(periods)))), ("prototype",)
    )
    piece_lineage = lineage(
        source_period="p2",
        origin_assigned_period="p3",
        target_assigned_period="p2",
        split_mode=ControlledSplitMode.FUTURE_BORROW_RETURN,
    )
    first_piece = node(
        node_id="piece-1", weight=D(3), source_period="p2", split_lineage=piece_lineage
    )
    second_piece = replace(
        first_piece,
        node_id="piece-2",
        weight=D(7),
        split_lineage=replace(piece_lineage, piece_index=2),
    )
    transition = node(
        node_id="transition",
        source_order_id="transition-order",
        source_resource_id="transition-resource",
        material_role=MaterialRole.ACTUAL_TRANSITION,
        weight=D(2),
    )
    current = node(
        node_id="current",
        source_order_id="current-order",
        source_resource_id="current-resource",
        source_period="p3",
    )
    late = node(
        node_id="late-node",
        source_order_id="late-order",
        source_resource_id="late-resource",
        source_period="p3",
        weight=D(4),
    )
    separator = virtual(
        weight=D(1),
        virtual_lineage=VirtualLineage(
            "prototype", VirtualPurpose.SPLIT_SEPARATOR, "not-a-piece-partition", 1
        ),
    )
    plan = SchedulePlan(
        (
            Chain("early", (separator, current, transition, first_piece), "p3"),
            Chain(
                "late",
                (second_piece, late),
                "p2",
            ),
        )
    )
    before = fingerprint((plan, context))
    view = derive_evaluation_resource_view(plan, context)
    assert view.borrowed_node_ids == ("transition", "piece-1")
    assert view.generated_virtual_node_ids == ("virtual",)
    assert view.split_partition_ids == ("partition",)
    assert (
        view.scheduled_real_weight,
        view.generated_virtual_weight,
        view.future_pool_weight,
        view.borrowed_future_weight,
    ) == (D(26), D(1), D(12), D(5))
    assert fingerprint((plan, context)) == before
    with pytest.raises(FrozenInstanceError):
        view.borrowed_future_weight = D(0)


def test_resource_view_partition_order_uses_first_piece_not_sorted_ids():
    pieces = tuple(
        node(
            node_id=f"{partition_id}-{index}",
            source_order_id=f"order-{partition_id}",
            source_resource_id=f"resource-{partition_id}",
            weight=D(1),
            split_lineage=lineage(
                partition_id=partition_id,
                parent_node_id=f"parent-{partition_id}",
                parent_source_order_id=f"order-{partition_id}",
                source_resource_id=f"resource-{partition_id}",
                accepted_source_sequence=1 if partition_id == "z" else 2,
                parent_weight=D(2),
                piece_index=index,
            ),
        )
        for partition_id, index in (("z", 1), ("a", 1), ("z", 2), ("a", 2))
    )
    plan = SchedulePlan((Chain("chain", pieces, "p1"),))
    context = RuleEvaluationContext(("p1",), {"p1": 0}, ())
    view = derive_evaluation_resource_view(plan, context)
    assert view.split_partition_ids == ("z", "a")
    assert view.borrowed_node_ids == view.generated_virtual_node_ids == ()
    assert view.future_pool_weight == view.borrowed_future_weight == D(0)


@pytest.mark.parametrize("invalid_field", ("plan", "context", "assigned_period", "source_period"))
def test_resource_view_rejects_invalid_types_and_unknown_periods(invalid_field):
    plan = SchedulePlan((Chain("chain", (node(),), "p1"),))
    context = RuleEvaluationContext(("p1",), {"p1": 0}, ())
    if invalid_field == "plan":
        plan = object()
    elif invalid_field == "context":
        context = object()
    elif invalid_field == "assigned_period":
        plan = SchedulePlan((replace(plan.chains[0], assigned_period="unknown"),))
    else:
        plan = SchedulePlan((Chain("chain", (node(source_period="unknown"),), "p1"),))
    with pytest.raises(ValueError):
        derive_evaluation_resource_view(plan, context)


def test_resource_view_weight_sums_do_not_inherit_callers_decimal_context():
    large = D("9999999999999999999999999999")
    plan = SchedulePlan(
        (Chain("chain", (node(weight=large), node(node_id="small", weight=D("1.1"))), "p0"),)
    )
    context = RuleEvaluationContext(("p0", "p1"), {"p0": 0, "p1": 1}, ())
    with localcontext() as arithmetic:
        arithmetic.prec = 2
        arithmetic.traps[Inexact] = arithmetic.traps[Rounded] = True
        view = derive_evaluation_resource_view(plan, context)
    assert (
        view.scheduled_real_weight
        == view.future_pool_weight
        == view.borrowed_future_weight
        == D("10000000000000000000000000000.1")
    )
