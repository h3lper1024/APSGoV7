"""Read-only rule inputs, contributions and the two-level rule contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar

from ..contracts import (
    ControlledSplitMode,
    RuleParameterValue,
    RuleScope,
    freeze_rule_parameters,
    freeze_tuple,
    require_decimal,
    require_enum,
    require_int,
    require_text,
)
from ..model import Chain, Node, SchedulePlan
from ..delivery_timing import DeliveryTiming

if TYPE_CHECKING:
    from ..resource_facts import EvaluationResourceView


class RuleDisposition(str, Enum):
    PROHIBITED = "prohibited"
    ALLOWED_FINAL_DEVIATION = "allowed_final_deviation"


class QualityDirection(str, Enum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class QualityAggregation(str, Enum):
    NAMED_VALUE = "named_value"
    COUNT = "count"
    SUM = "sum"
    MAXIMUM = "maximum"


class NumericProjection(str, Enum):
    EXACT_DECIMAL = "exact_decimal"
    REFERENCE_FLOAT_ROUND_6 = "reference_float_round_6"
    UNDERWEIGHT_GAP_ROUND_2_THEN_SUM = "underweight_gap_round_2_then_sum"


@dataclass(frozen=True, slots=True)
class NodeRuleSubject:
    subject_id: str
    node: Node

    def __post_init__(self):
        require_text(self.subject_id, "subject_id")
        if not isinstance(self.node, Node):
            raise ValueError("node subject requires a Node")


@dataclass(frozen=True, slots=True)
class EdgeRuleSubject:
    subject_id: str
    left: Node
    right: Node

    def __post_init__(self):
        require_text(self.subject_id, "subject_id")
        if not isinstance(self.left, Node) or not isinstance(self.right, Node):
            raise ValueError("edge subject requires two Nodes")


@dataclass(frozen=True, slots=True)
class ChainRuleSubject:
    subject_id: str
    chain: Chain

    def __post_init__(self):
        require_text(self.subject_id, "subject_id")
        if not isinstance(self.chain, Chain):
            raise ValueError("chain subject requires a Chain")


@dataclass(frozen=True, slots=True)
class PlanRuleSubject:
    subject_id: str
    plan: SchedulePlan
    resource_view: EvaluationResourceView

    def __post_init__(self):
        require_text(self.subject_id, "subject_id")
        if not isinstance(self.plan, SchedulePlan):
            raise ValueError("plan subject requires a SchedulePlan")


@dataclass(frozen=True, slots=True)
class ControlledSplitRuleSubject:
    subject_id: str
    parent_node: Node
    origin_assigned_period: str
    source_period: str
    accepted_split_source_count: int

    def __post_init__(self):
        for name in ("subject_id", "origin_assigned_period", "source_period"):
            require_text(getattr(self, name), name)
        if not isinstance(self.parent_node, Node):
            raise ValueError("split subject requires a parent Node")
        require_int(self.accepted_split_source_count, "accepted_split_source_count")


RuleSubject = (
    NodeRuleSubject
    | EdgeRuleSubject
    | ChainRuleSubject
    | PlanRuleSubject
    | ControlledSplitRuleSubject
)


@dataclass(frozen=True, slots=True)
class RuleEvaluationContext:
    period_order: tuple[str, ...]
    period_index: Mapping[str, int]
    virtual_prototype_ids: tuple[str, ...]
    delivery_timing: DeliveryTiming | None = field(default=None, metadata={"omit_none": True})

    def __post_init__(self):
        periods = freeze_tuple(self.period_order, str, "period_order")
        prototypes = freeze_tuple(self.virtual_prototype_ids, str, "virtual_prototype_ids")
        if not periods or len(set(periods)) != len(periods):
            raise ValueError("context requires nonempty distinct ordered periods")
        if len(set(prototypes)) != len(prototypes):
            raise ValueError("duplicate virtual prototype identity")
        for name, values in (("period_id", periods), ("prototype_id", prototypes)):
            for value in values:
                require_text(value, name)
        if not isinstance(self.period_index, Mapping):
            raise ValueError("period_index must be a mapping")
        index = dict(self.period_index)
        for value in index.values():
            require_int(value, "period_index")
        if index != {period: ordinal for ordinal, period in enumerate(periods)}:
            raise ValueError("period_index must exactly enumerate period_order from zero")
        object.__setattr__(self, "period_order", periods)
        object.__setattr__(self, "period_index", MappingProxyType(index))
        object.__setattr__(self, "virtual_prototype_ids", prototypes)
        if self.delivery_timing is not None and (
            not isinstance(self.delivery_timing, DeliveryTiming)
            or set(self.delivery_timing.virtual_hours_per_tonne) != set(prototypes)
        ):
            raise ValueError("context delivery timing must match virtual prototypes")


@dataclass(frozen=True, slots=True)
class RuleViolation:
    rule_id: str
    scope: RuleScope
    subject_id: str
    reason_code: str
    message: str
    disposition: RuleDisposition
    severity: Decimal

    def __post_init__(self):
        for name in ("rule_id", "subject_id", "reason_code", "message"):
            require_text(getattr(self, name), name)
        require_enum(self.scope, RuleScope, "scope")
        require_enum(self.disposition, RuleDisposition, "disposition")
        require_decimal(self.severity, "severity", nonnegative=True)


@dataclass(frozen=True, slots=True)
class MetricContribution:
    metric_key: str
    value: Decimal | int

    def __post_init__(self):
        require_text(self.metric_key, "metric_key")
        if isinstance(self.value, Decimal):
            require_decimal(self.value, "value")
        else:
            require_int(self.value, "value", minimum=None)


@dataclass(frozen=True, slots=True)
class RuleContribution:
    violations: tuple[RuleViolation, ...]
    metrics: tuple[MetricContribution, ...]

    def __post_init__(self):
        object.__setattr__(
            self, "violations", freeze_tuple(self.violations, RuleViolation, "violations")
        )
        object.__setattr__(
            self, "metrics", freeze_tuple(self.metrics, MetricContribution, "metrics")
        )


@dataclass(frozen=True, slots=True)
class QualityCriterion:
    criterion_id: str
    metric_key: str
    direction: QualityDirection
    aggregation: QualityAggregation
    numeric_projection: NumericProjection

    def __post_init__(self):
        require_text(self.criterion_id, "criterion_id")
        require_text(self.metric_key, "metric_key")
        require_enum(self.direction, QualityDirection, "direction")
        require_enum(self.aggregation, QualityAggregation, "aggregation")
        require_enum(self.numeric_projection, NumericProjection, "numeric_projection")
        if self.numeric_projection is NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM and (
            self.metric_key != "underweight_total_gap"
            or self.aggregation is not QualityAggregation.SUM
            or self.direction is not QualityDirection.MINIMIZE
        ):
            raise ValueError(
                "underweight_gap_round_2_then_sum requires underweight_total_gap + sum + minimize"
            )


@dataclass(frozen=True, slots=True)
class ControlledSplitDecision:
    eligible: bool
    rule_id: str
    rule_version: str
    reason_code: str
    mode: ControlledSplitMode | None
    target_assigned_period: str | None
    maximum_piece_weight: Decimal | None
    minimum_piece_weight: Decimal | None
    maximum_accepted_source_count: int
    maximum_separator_node_count: int
    maximum_separator_weight: Decimal
    decision_fingerprint: str

    def __post_init__(self):
        if type(self.eligible) is not bool:
            raise ValueError("eligible must be boolean")
        for name in ("rule_id", "rule_version", "reason_code", "decision_fingerprint"):
            require_text(getattr(self, name), name)
        require_int(self.maximum_accepted_source_count, "maximum_accepted_source_count")
        require_int(self.maximum_separator_node_count, "maximum_separator_node_count")
        require_decimal(self.maximum_separator_weight, "maximum_separator_weight", nonnegative=True)
        if self.eligible:
            require_enum(self.mode, ControlledSplitMode, "mode")
            require_text(self.target_assigned_period, "target_assigned_period")
            require_decimal(self.maximum_piece_weight, "maximum_piece_weight", positive=True)
            require_decimal(self.minimum_piece_weight, "minimum_piece_weight", positive=True)
            if self.minimum_piece_weight > self.maximum_piece_weight:
                raise ValueError("minimum piece weight exceeds maximum piece weight")
        elif (
            any(
                value is not None
                for value in (
                    self.mode,
                    self.target_assigned_period,
                    self.maximum_piece_weight,
                    self.minimum_piece_weight,
                )
            )
            or self.maximum_accepted_source_count != 0
            or self.maximum_separator_node_count != 0
            or self.maximum_separator_weight != 0
        ):
            raise ValueError(
                "rejected split decision must have no mode, target or authorized limits"
            )


class UnsupportedRuleSubjectError(TypeError):
    """A rule was used through a subject or entry point it does not support."""


@dataclass(frozen=True, slots=True)
class Rule(ABC):
    rule_id: str
    name: str
    scope: RuleScope
    enabled: bool
    version: str
    parameters: Mapping[str, RuleParameterValue]
    supported_scope: ClassVar[RuleScope]
    metric_aggregation: ClassVar[QualityAggregation] = QualityAggregation.SUM

    def __post_init__(self):
        for name in ("rule_id", "name", "version"):
            require_text(getattr(self, name), name)
        require_enum(self.scope, RuleScope, "scope")
        if self.scope is not getattr(type(self), "supported_scope", None):
            raise ValueError("rule scope does not match its declared supported_scope")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        object.__setattr__(self, "parameters", freeze_rule_parameters(self.parameters))

    @property
    def rule_type(self) -> str:
        return type(self).__name__

    @abstractmethod
    def required_fields(self) -> tuple[str, ...]:
        """Declare the input node fields needed by this concrete rule."""

    def metric_keys(self) -> tuple[str, ...]:
        return ()

    def edge_semantic_fields(self) -> tuple[str, ...] | None:
        """Declare all output-affecting edge reads, including metrics and messages.

        These are not nonempty input requirements; None forbids caching. Temporary
        identities belong only in subject_id, never in cached messages or decisions.
        """
        return None

    def evaluate(self, subject: RuleSubject, context: RuleEvaluationContext) -> RuleContribution:
        raise UnsupportedRuleSubjectError(type(subject))

    def construction_priority(self, node: Node) -> tuple:
        raise UnsupportedRuleSubjectError(type(node))

    def evaluate_controlled_split(
        self, subject: ControlledSplitRuleSubject, context: RuleEvaluationContext
    ) -> ControlledSplitDecision:
        raise UnsupportedRuleSubjectError(type(subject))
