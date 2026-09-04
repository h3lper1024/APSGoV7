"""Per-evaluation resource view and immutable final-audit fact carriers."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from typing import TYPE_CHECKING

from apsgo_scheduler.core.contracts import (
    ControlledSplitMode,
    fingerprint,
    freeze_tuple,
    require_decimal,
    require_enum,
    require_int,
    require_text,
    sum_weights,
    validate_split_target,
)
from apsgo_scheduler.core.model import MaterialRole, SchedulePlan, SchedulingProblem, VirtualPurpose

if TYPE_CHECKING:
    from .budget import SolveRuntimeBudget
    from .rules.base import RuleEvaluationContext


@dataclass(frozen=True, slots=True)
class NodeAssignmentFact:
    node_id: str
    source_order_id: str | None
    source_resource_id: str | None
    material_role: MaterialRole
    chain_id: str
    assigned_period: str
    position: int
    weight: Decimal

    def __post_init__(self) -> None:
        for name in ("node_id", "chain_id", "assigned_period"):
            require_text(getattr(self, name), name)
        require_enum(self.material_role, MaterialRole, "material_role")
        virtual = self.material_role is MaterialRole.GENERATED_VIRTUAL
        for name in ("source_order_id", "source_resource_id"):
            value = getattr(self, name)
            require_text(value, name, allow_none=virtual)
            if virtual and value is not None:
                raise ValueError(f"{name} must be absent for generated virtual nodes")
        require_int(self.position, "position")
        require_decimal(self.weight, "weight", positive=True)


@dataclass(frozen=True, slots=True)
class FutureBorrowFact:
    node_id: str
    source_order_id: str
    source_resource_id: str
    source_period: str
    assigned_period: str
    weight: Decimal

    def __post_init__(self) -> None:
        for name in (
            "node_id",
            "source_order_id",
            "source_resource_id",
            "source_period",
            "assigned_period",
        ):
            require_text(getattr(self, name), name)
        if self.source_period == self.assigned_period:
            raise ValueError("future borrowing requires different source and assigned periods")
        require_decimal(self.weight, "weight", positive=True)


@dataclass(frozen=True, slots=True)
class VirtualGenerationFact:
    node_id: str
    prototype_id: str
    purpose: VirtualPurpose
    related_partition_id: str | None
    accepted_sequence: int
    chain_id: str
    assigned_period: str
    weight: Decimal

    def __post_init__(self) -> None:
        for name in ("node_id", "prototype_id", "chain_id", "assigned_period"):
            require_text(getattr(self, name), name)
        require_enum(self.purpose, VirtualPurpose, "purpose")
        separator = self.purpose is VirtualPurpose.SPLIT_SEPARATOR
        require_text(self.related_partition_id, "related_partition_id", allow_none=not separator)
        if not separator and self.related_partition_id is not None:
            raise ValueError("only split separators may refer to a partition")
        require_int(self.accepted_sequence, "accepted_sequence", minimum=1)
        require_decimal(self.weight, "weight", positive=True)


@dataclass(frozen=True, slots=True)
class SplitPartitionFact:
    partition_id: str
    partition_fingerprint: str
    parent_node_id: str
    parent_source_order_id: str
    source_resource_id: str
    source_period: str
    origin_assigned_period: str
    split_mode: ControlledSplitMode
    target_assigned_period: str
    accepted_source_sequence: int
    authorization_rule_id: str
    authorization_rule_version: str
    authorization_decision_fingerprint: str
    reason_code: str
    piece_node_ids: tuple[str, ...]
    piece_weights: tuple[Decimal, ...]
    separator_virtual_node_ids: tuple[str, ...]
    parent_weight: Decimal

    def __post_init__(self) -> None:
        for name in (
            "partition_id",
            "partition_fingerprint",
            "parent_node_id",
            "parent_source_order_id",
            "source_resource_id",
            "source_period",
            "origin_assigned_period",
            "target_assigned_period",
            "authorization_rule_id",
            "authorization_rule_version",
            "authorization_decision_fingerprint",
            "reason_code",
        ):
            require_text(getattr(self, name), name)
        require_enum(self.split_mode, ControlledSplitMode, "split_mode")
        validate_split_target(
            self.split_mode,
            self.source_period,
            self.origin_assigned_period,
            self.target_assigned_period,
        )
        require_int(self.accepted_source_sequence, "accepted_source_sequence", minimum=1)
        require_decimal(self.parent_weight, "parent_weight", positive=True)
        for name in ("piece_node_ids", "separator_virtual_node_ids"):
            values = freeze_tuple(getattr(self, name), str, name)
            for value in values:
                require_text(value, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique node identities")
            if self.parent_node_id in values:
                raise ValueError(f"{name} must not contain the replaced parent node")
            object.__setattr__(self, name, values)
        weights = freeze_tuple(self.piece_weights, Decimal, "piece_weights")
        for weight in weights:
            require_decimal(weight, "piece_weights", positive=True)
        object.__setattr__(self, "piece_weights", weights)
        if len(self.piece_node_ids) < 2 or len(self.piece_node_ids) != len(weights):
            raise ValueError("a split requires at least two pieces with one weight per node")
        if set(self.piece_node_ids).intersection(self.separator_virtual_node_ids):
            raise ValueError("split pieces and virtual separators must have different identities")
        if sum_weights(weights) != self.parent_weight:
            raise ValueError("split piece weights must exactly conserve the parent weight")

    def fingerprint_payload(self) -> tuple[tuple[str, object], ...]:
        """Expose all ordered partition semantics, without a self-referential hash."""
        return tuple(
            (field.name, getattr(self, field.name))
            for field in fields(self)
            if field.name != "partition_fingerprint"
        )


@dataclass(frozen=True, slots=True)
class ActualTransitionFact:
    node_id: str
    source_order_id: str
    source_resource_id: str
    chain_id: str
    assigned_period: str
    weight: Decimal

    def __post_init__(self) -> None:
        for name in (
            "node_id",
            "source_order_id",
            "source_resource_id",
            "chain_id",
            "assigned_period",
        ):
            require_text(getattr(self, name), name)
        require_decimal(self.weight, "weight", positive=True)


@dataclass(frozen=True, slots=True)
class PlanDerivedFacts:
    """Audited value carrier, not a mutable ledger or a second derivation path."""

    assignments: tuple[NodeAssignmentFact, ...]
    future_borrows: tuple[FutureBorrowFact, ...]
    virtual_generations: tuple[VirtualGenerationFact, ...]
    split_partitions: tuple[SplitPartitionFact, ...]
    actual_transitions: tuple[ActualTransitionFact, ...]
    input_real_weight: Decimal
    scheduled_real_weight: Decimal
    generated_virtual_weight: Decimal
    future_pool_weight: Decimal
    borrowed_future_weight: Decimal
    facts_fingerprint: str

    def __post_init__(self) -> None:
        for name, fact_type in (
            ("assignments", NodeAssignmentFact),
            ("future_borrows", FutureBorrowFact),
            ("virtual_generations", VirtualGenerationFact),
            ("split_partitions", SplitPartitionFact),
            ("actual_transitions", ActualTransitionFact),
        ):
            object.__setattr__(self, name, freeze_tuple(getattr(self, name), fact_type, name))
        for name in (
            "input_real_weight",
            "scheduled_real_weight",
            "generated_virtual_weight",
            "future_pool_weight",
            "borrowed_future_weight",
        ):
            require_decimal(getattr(self, name), name, nonnegative=True)
        require_text(self.facts_fingerprint, "facts_fingerprint")


@dataclass(frozen=True, slots=True)
class EvaluationResourceView:
    """Values derived for one evaluation, never a mutable or final resource ledger."""

    borrowed_node_ids: tuple[str, ...]
    generated_virtual_node_ids: tuple[str, ...]
    split_partition_ids: tuple[str, ...]
    scheduled_real_weight: Decimal
    generated_virtual_weight: Decimal
    future_pool_weight: Decimal
    borrowed_future_weight: Decimal

    def __post_init__(self) -> None:
        for name in (
            "borrowed_node_ids",
            "generated_virtual_node_ids",
            "split_partition_ids",
        ):
            values = freeze_tuple(getattr(self, name), str, name)
            for value in values:
                require_text(value, name)
            object.__setattr__(self, name, values)
        for name in (
            "scheduled_real_weight",
            "generated_virtual_weight",
            "future_pool_weight",
            "borrowed_future_weight",
        ):
            require_decimal(getattr(self, name), name, nonnegative=True)


def derive_evaluation_resource_view(
    plan: SchedulePlan, context: RuleEvaluationContext
) -> EvaluationResourceView:
    """Read actual assignments without normalizing periods or deriving audited facts."""
    from .rules.base import RuleEvaluationContext

    if not isinstance(plan, SchedulePlan) or not isinstance(context, RuleEvaluationContext):
        raise ValueError("resource evaluation requires SchedulePlan and RuleEvaluationContext")
    real, virtual, future, borrowed, partitions = [], [], [], [], []
    for chain_index, chain in enumerate(plan.chains):
        if chain.assigned_period not in context.period_index:
            raise ValueError(f"chains[{chain_index}].assigned_period is unknown")
        assigned_index = context.period_index[chain.assigned_period]
        for position, node in enumerate(chain.nodes):
            if node.material_role is MaterialRole.GENERATED_VIRTUAL:
                virtual.append(node)
                continue
            if node.source_period not in context.period_index:
                raise ValueError(
                    f"chains[{chain_index}].nodes[{position}].source_period is unknown"
                )
            real.append(node)
            source_index = context.period_index[node.source_period]
            if source_index > 0:
                future.append(node)
            if source_index > assigned_index:
                borrowed.append(node)
            if node.split_lineage is not None:
                partitions.append(node.split_lineage.partition_id)
    return EvaluationResourceView(
        borrowed_node_ids=tuple(node.node_id for node in borrowed),
        generated_virtual_node_ids=tuple(node.node_id for node in virtual),
        split_partition_ids=tuple(dict.fromkeys(partitions)),
        scheduled_real_weight=sum_weights(node.weight for node in real),
        generated_virtual_weight=sum_weights(node.weight for node in virtual),
        future_pool_weight=sum_weights(node.weight for node in future),
        borrowed_future_weight=sum_weights(node.weight for node in borrowed),
    )


def _derive_audited_resource_facts(
    plan: SchedulePlan,
    problem: SchedulingProblem,
    context: RuleEvaluationContext,
    split_partitions: tuple[SplitPartitionFact, ...],
    budget: SolveRuntimeBudget,
) -> PlanDerivedFacts | None:
    """Called only by final audit after structure and split authorization checks."""
    split_partitions = freeze_tuple(split_partitions, SplitPartitionFact, "split_partitions")
    assignments, borrows, virtuals, transitions, future_weights = [], [], [], [], []
    for chain in plan.chains:
        if not budget.allows_finalization():
            return None
        assigned_index = context.period_index[chain.assigned_period]
        for position, node in enumerate(chain.nodes):
            if not budget.allows_finalization():
                return None
            assignments.append(
                NodeAssignmentFact(
                    node.node_id,
                    node.source_order_id,
                    node.source_resource_id,
                    node.material_role,
                    chain.chain_id,
                    chain.assigned_period,
                    position,
                    node.weight,
                )
            )
            if node.material_role is MaterialRole.GENERATED_VIRTUAL:
                lineage = node.virtual_lineage
                virtuals.append(
                    VirtualGenerationFact(
                        node.node_id,
                        lineage.prototype_id,
                        lineage.purpose,
                        lineage.related_partition_id,
                        lineage.accepted_sequence,
                        chain.chain_id,
                        chain.assigned_period,
                        node.weight,
                    )
                )
                continue
            source_index = context.period_index[node.source_period]
            if source_index > 0:
                future_weights.append(node.weight)
            if source_index > assigned_index:
                borrows.append(
                    FutureBorrowFact(
                        node.node_id,
                        node.source_order_id,
                        node.source_resource_id,
                        node.source_period,
                        chain.assigned_period,
                        node.weight,
                    )
                )
            if node.material_role is MaterialRole.ACTUAL_TRANSITION:
                transitions.append(
                    ActualTransitionFact(
                        node.node_id,
                        node.source_order_id,
                        node.source_resource_id,
                        chain.chain_id,
                        chain.assigned_period,
                        node.weight,
                    )
                )
    if not budget.allows_finalization():
        return None
    values = dict(
        assignments=tuple(assignments),
        future_borrows=tuple(borrows),
        virtual_generations=tuple(virtuals),
        split_partitions=split_partitions,
        actual_transitions=tuple(transitions),
        input_real_weight=sum_weights(node.weight for node in problem.nodes),
        scheduled_real_weight=sum_weights(
            item.weight
            for item in assignments
            if item.material_role is not MaterialRole.GENERATED_VIRTUAL
        ),
        generated_virtual_weight=sum_weights(item.weight for item in virtuals),
        future_pool_weight=sum_weights(future_weights),
        borrowed_future_weight=sum_weights(item.weight for item in borrows),
    )
    facts = PlanDerivedFacts(**values, facts_fingerprint=fingerprint(values))
    return facts if budget.allows_finalization() else None
