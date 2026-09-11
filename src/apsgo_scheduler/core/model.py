"""Immutable scheduling structure and the private accepted-search state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING
from .delivery_timing import DeliveryTiming

from .contracts import (
    ControlledSplitMode,
    RuleScalar,
    freeze_scalars,
    freeze_tuple,
    require_decimal,
    require_enum,
    require_int,
    require_text,
    sum_weights,
    validate_split_target,
)

if TYPE_CHECKING:
    from .evaluation import PlanEvaluation


class MaterialRole(str, Enum):
    NORMAL_REAL = "normal_real"
    ACTUAL_TRANSITION = "actual_transition"
    GENERATED_VIRTUAL = "virtual_sphc"


class VirtualPurpose(str, Enum):
    EDGE_BRIDGE = "edge_bridge"
    WEIGHT_FILL = "weight_fill"
    SPLIT_SEPARATOR = "split_separator"


def validate_dimensions(width, thickness, minimum, maximum) -> None:
    require_decimal(width, "width", positive=True, allow_none=True)
    require_decimal(thickness, "thickness", positive=True, allow_none=True)
    require_decimal(minimum, "min_temperature", allow_none=True)
    require_decimal(maximum, "max_temperature", allow_none=True)
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("temperature interval is reversed")


@dataclass(frozen=True, slots=True)
class VirtualMaterialPrototype:
    prototype_id: str
    unit_weight: Decimal
    width: Decimal | None
    thickness: Decimal | None
    min_temperature: Decimal | None
    max_temperature: Decimal | None
    grade: str
    rule_attributes: Mapping[str, RuleScalar]

    def __post_init__(self):
        require_text(self.prototype_id, "prototype_id")
        require_decimal(self.unit_weight, "unit_weight", positive=True)
        validate_dimensions(self.width, self.thickness, self.min_temperature, self.max_temperature)
        if not isinstance(self.grade, str):
            raise ValueError("grade must be text")
        object.__setattr__(self, "rule_attributes", freeze_scalars(self.rule_attributes))


@dataclass(frozen=True, slots=True)
class VirtualLineage:
    prototype_id: str
    purpose: VirtualPurpose
    related_partition_id: str | None
    accepted_sequence: int

    def __post_init__(self):
        require_text(self.prototype_id, "prototype_id")
        require_enum(self.purpose, VirtualPurpose, "purpose")
        require_int(self.accepted_sequence, "accepted_sequence", minimum=1)
        if self.purpose is VirtualPurpose.SPLIT_SEPARATOR:
            require_text(self.related_partition_id, "related_partition_id")
        elif self.related_partition_id is not None:
            raise ValueError("only split separators may refer to a partition")


@dataclass(frozen=True, slots=True)
class SplitLineage:
    partition_id: str
    parent_node_id: str
    parent_source_order_id: str
    source_resource_id: str
    source_period: str
    origin_assigned_period: str
    split_mode: ControlledSplitMode
    target_assigned_period: str
    accepted_source_sequence: int
    parent_weight: Decimal
    piece_index: int
    piece_count: int
    authorization_rule_id: str
    authorization_rule_version: str
    authorization_decision_fingerprint: str
    reason_code: str

    def __post_init__(self):
        for name in (
            "partition_id",
            "parent_node_id",
            "parent_source_order_id",
            "source_resource_id",
            "authorization_rule_id",
            "authorization_rule_version",
            "authorization_decision_fingerprint",
            "reason_code",
        ):
            require_text(getattr(self, name), name)
        validate_split_target(
            self.split_mode,
            self.source_period,
            self.origin_assigned_period,
            self.target_assigned_period,
        )
        require_int(self.accepted_source_sequence, "accepted_source_sequence", minimum=1)
        require_int(self.piece_count, "piece_count", minimum=2)
        require_int(self.piece_index, "piece_index", minimum=1)
        if self.piece_index > self.piece_count:
            raise ValueError("piece index is outside its partition")
        require_decimal(self.parent_weight, "parent_weight", positive=True)


@dataclass(frozen=True, slots=True)
class Node:
    node_id: str
    source_order_id: str | None
    source_resource_id: str | None
    source_period: str | None
    weight: Decimal
    width: Decimal | None
    thickness: Decimal | None
    min_temperature: Decimal | None
    max_temperature: Decimal | None
    grade: str
    material_role: MaterialRole
    rule_attributes: Mapping[str, RuleScalar]
    virtual_lineage: VirtualLineage | None = None
    split_lineage: SplitLineage | None = None

    def __post_init__(self):
        require_text(self.node_id, "node_id")
        require_decimal(self.weight, "weight", positive=True)
        validate_dimensions(self.width, self.thickness, self.min_temperature, self.max_temperature)
        require_enum(self.material_role, MaterialRole, "material_role")
        if not isinstance(self.grade, str):
            raise ValueError("grade must be text")
        attributes = freeze_scalars(self.rule_attributes)
        if set(attributes) & {item.name for item in fields(self)}:
            raise ValueError("rule attributes may not shadow core node fields")
        object.__setattr__(self, "rule_attributes", attributes)
        source_names = ("source_order_id", "source_resource_id", "source_period")
        if self.material_role is MaterialRole.GENERATED_VIRTUAL:
            if any(getattr(self, name) is not None for name in source_names):
                raise ValueError("generated virtual nodes have no input source fields")
            if (
                not isinstance(self.virtual_lineage, VirtualLineage)
                or self.split_lineage is not None
            ):
                raise ValueError("generated virtual node requires only virtual lineage")
            return
        for name in source_names:
            require_text(getattr(self, name), name)
        if self.virtual_lineage is not None:
            raise ValueError("input-supported material cannot have virtual lineage")
        lineage = self.split_lineage
        if lineage is not None:
            if self.material_role is not MaterialRole.NORMAL_REAL or not isinstance(
                lineage, SplitLineage
            ):
                raise ValueError("only normal real material may have split lineage")
            if (
                self.source_order_id != lineage.parent_source_order_id
                or self.source_resource_id != lineage.source_resource_id
                or self.source_period != lineage.source_period
            ):
                raise ValueError("piece source fields must equal its lineage")
            if self.node_id == lineage.parent_node_id or self.weight >= lineage.parent_weight:
                raise ValueError("a split piece must have its own identity and a smaller weight")


@dataclass(frozen=True, slots=True)
class SchedulingProblem:
    problem_id: str
    product_line_code: str
    process_code: str
    scenario: str
    nodes: tuple[Node, ...]
    period_order: tuple[str, ...]
    virtual_prototypes: tuple[VirtualMaterialPrototype, ...]
    input_fingerprint: str
    delivery_timing: DeliveryTiming | None = field(default=None, metadata={"omit_none": True})

    def __post_init__(self):
        for name in (
            "problem_id",
            "product_line_code",
            "process_code",
            "scenario",
            "input_fingerprint",
        ):
            require_text(getattr(self, name), name)
        nodes = freeze_tuple(self.nodes, Node, "nodes")
        periods = freeze_tuple(self.period_order, str, "period_order")
        prototypes = freeze_tuple(
            self.virtual_prototypes, VirtualMaterialPrototype, "virtual_prototypes"
        )
        if not nodes or not periods or len(set(periods)) != len(periods):
            raise ValueError("problem needs nodes and distinct ordered periods")
        for period in periods:
            require_text(period, "period_id")
        for node in nodes:
            if (
                node.material_role is MaterialRole.GENERATED_VIRTUAL
                or node.split_lineage is not None
            ):
                raise ValueError("problem contains only unsplit input-supported nodes")
            if node.source_period not in periods:
                raise ValueError("node source period is not in this problem")
        for name in ("node_id", "source_order_id", "source_resource_id"):
            if len({getattr(node, name) for node in nodes}) != len(nodes):
                raise ValueError(f"duplicate input {name}")
        if len({item.prototype_id for item in prototypes}) != len(prototypes):
            raise ValueError("duplicate virtual prototype identity")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "period_order", periods)
        object.__setattr__(self, "virtual_prototypes", prototypes)
        if self.delivery_timing is not None:
            timing = self.delivery_timing
            if not isinstance(timing, DeliveryTiming):
                raise ValueError("delivery_timing must be DeliveryTiming")
            if set(timing.orders) != {node.source_order_id for node in nodes} or any(
                timing.orders[node.source_order_id].weight != node.weight for node in nodes
            ):
                raise ValueError("delivery timing must bind all original order weights")
            if set(timing.virtual_hours_per_tonne) != {p.prototype_id for p in prototypes}:
                raise ValueError("delivery timing must bind all virtual prototypes")


@dataclass(frozen=True, slots=True)
class Chain:
    chain_id: str
    nodes: tuple[Node, ...]
    assigned_period: str

    def __post_init__(self):
        require_text(self.chain_id, "chain_id")
        require_text(self.assigned_period, "assigned_period")
        nodes = freeze_tuple(self.nodes, Node, "nodes")
        if not nodes or all(node.material_role is MaterialRole.GENERATED_VIRTUAL for node in nodes):
            raise ValueError("chain needs at least one input-supported node")
        if len({node.node_id for node in nodes}) != len(nodes):
            raise ValueError("duplicate node identity in chain")
        object.__setattr__(self, "nodes", nodes)

    @property
    def total_weight(self) -> Decimal:
        return sum_weights(node.weight for node in self.nodes)

    @property
    def real_weight(self) -> Decimal:
        return sum_weights(
            (
                node.weight
                for node in self.nodes
                if node.material_role is not MaterialRole.GENERATED_VIRTUAL
            ),
        )

    @property
    def virtual_weight(self) -> Decimal:
        return sum_weights(
            (
                node.weight
                for node in self.nodes
                if node.material_role is MaterialRole.GENERATED_VIRTUAL
            ),
        )

    @property
    def real_node_count(self) -> int:
        return sum(node.material_role is not MaterialRole.GENERATED_VIRTUAL for node in self.nodes)

    @property
    def virtual_node_count(self) -> int:
        return len(self.nodes) - self.real_node_count

    @property
    def split_piece_count(self) -> int:
        return sum(node.split_lineage is not None for node in self.nodes)

    @property
    def first_node(self) -> Node:
        return self.nodes[0]

    @property
    def last_node(self) -> Node:
        return self.nodes[-1]


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    chains: tuple[Chain, ...]

    def __post_init__(self):
        chains = freeze_tuple(self.chains, Chain, "chains")
        if not chains or len({chain.chain_id for chain in chains}) != len(chains):
            raise ValueError("plan needs nonempty, uniquely identified chains")
        node_ids = [node.node_id for chain in chains for node in chain.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node identity must be unique throughout a plan")
        object.__setattr__(self, "chains", chains)


@dataclass(slots=True)
class SearchState:
    current_plan: SchedulePlan
    current_evaluation: PlanEvaluation
    accepted_move_count: int = 0
    virtual_sequence: int = 0
    split_sequence: int = 0
    accepted_same_period_split_count: int = 0
    accepted_future_borrow_return_count: int = 0

    def __post_init__(self):
        from .evaluation import PlanEvaluation

        if not isinstance(self.current_plan, SchedulePlan):
            raise ValueError("search state needs a complete plan")
        if not isinstance(self.current_evaluation, PlanEvaluation):
            raise ValueError("search state needs PlanEvaluation")
        for name in (
            "accepted_move_count",
            "virtual_sequence",
            "split_sequence",
            "accepted_same_period_split_count",
            "accepted_future_borrow_return_count",
        ):
            require_int(getattr(self, name), name)
        if self.split_sequence != (
            self.accepted_same_period_split_count + self.accepted_future_borrow_return_count
        ):
            raise ValueError("split sequence must equal both accepted mode counts")

    def commit_accepted(
        self,
        plan: SchedulePlan,
        evaluation: PlanEvaluation,
        *,
        virtual_sequence: int,
        split_mode: ControlledSplitMode | None = None,
    ) -> None:
        """Commit a caller-approved complete candidate without deciding acceptance."""
        require_int(virtual_sequence, "virtual_sequence")
        if virtual_sequence < self.virtual_sequence:
            raise ValueError("accepted virtual sequence cannot move backwards")
        if split_mode is not None:
            require_enum(split_mode, ControlledSplitMode, "split_mode")
        candidate = replace(
            self,
            current_plan=plan,
            current_evaluation=evaluation,
            accepted_move_count=self.accepted_move_count + 1,
            virtual_sequence=virtual_sequence,
            split_sequence=self.split_sequence + int(split_mode is not None),
            accepted_same_period_split_count=self.accepted_same_period_split_count
            + int(split_mode is ControlledSplitMode.SAME_PERIOD_SPLIT),
            accepted_future_borrow_return_count=self.accepted_future_borrow_return_count
            + int(split_mode is ControlledSplitMode.FUTURE_BORROW_RETURN),
        )
        # All checks run before publishing any accepted state or sequence changes.
        for item in fields(self):
            setattr(self, item.name, getattr(candidate, item.name))
