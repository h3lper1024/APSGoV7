"""Concrete rule implementations; each rule declares its own input requirements."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal
from itertools import groupby, pairwise
from math import isfinite

from ..contracts import ControlledSplitMode, require_decimal, require_int, require_text, sum_weights
from ..model import MaterialRole
from ..delivery_timing import evaluate_delivery
from ..resource_facts import EvaluationResourceView
from .base import (
    ChainRuleSubject,
    ControlledSplitDecision,
    ControlledSplitRuleSubject,
    EdgeRuleSubject,
    MetricContribution,
    NodeRuleSubject,
    PlanRuleSubject,
    QualityAggregation,
    Rule,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    RuleScope,
    RuleViolation,
    UnsupportedRuleSubjectError,
)
from .helpers import create_controlled_split_decision

WEIGHT_EPSILON = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class DeliveryDuePerformanceRule(Rule):
    supported_scope = RuleScope.PLAN

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled and (
            set(self.parameters) - {"include_backlog_clearance", "score_time_unit"}
            or type(self.parameters.get("include_backlog_clearance", False)) is not bool
        ):
            raise ValueError("delivery performance only accepts boolean include_backlog_clearance")
        if self.enabled and ("score_time_unit" in self.parameters or self.version == "3") and (
            self.parameters.get("score_time_unit") != "second"
            or not self.include_backlog_clearance or self.version != "3"
        ):
            raise ValueError("second precision requires rule version 3, backlog clearance and score_time_unit=second")

    @property
    def include_backlog_clearance(self):
        return self.enabled and self.parameters.get("include_backlog_clearance", False)

    @property
    def second_precision(self):
        return self.enabled and self.parameters.get("score_time_unit") == "second"

    def required_fields(self):
        return ()

    def metric_keys(self):
        if not self.enabled:
            return ()
        return ("newly_late_original_weight", "delivery_wait_tardiness_tonne_hours") + (
            ("old_backlog_last_completion_hours",) if self.include_backlog_clearance else ()
        )

    def evaluate(self, subject, context):
        if not isinstance(subject, PlanRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        result = evaluate_delivery(subject.plan, context.delivery_timing, second_precision=self.second_precision)
        return RuleContribution((), tuple(
            MetricContribution(key, getattr(result, key)) for key in self.metric_keys()
        ))


def _positive_weight_difference(left, right):
    if left <= right:
        return Decimal(0)
    # Preserve all significant digits independently of the caller's Decimal context.
    precision = (
        max(left.adjusted(), right.adjusted())
        - min(left.as_tuple().exponent, right.as_tuple().exponent)
        + 2
    )
    return Context(prec=max(1, precision)).subtract(left, right)


def _continuous_weight_contribution(rule, subject, group_key, metric_key, reason_code):
    """A None key breaks a run; tuples containing None remain ordinary group keys."""
    limit = rule.parameters["max_real_weight"]
    allowed_weight = sum_weights((limit, WEIGHT_EPSILON))
    violations = []
    maximum = Decimal(0)
    for key, segment in groupby(enumerate(subject.chain.nodes), group_key):
        if key is None:
            continue
        members = tuple(segment)
        total = sum_weights(node.weight for _, node in members)
        maximum = max(maximum, total)
        if total > allowed_weight:
            severity = _positive_weight_difference(total, limit)
            violations.append(
                RuleViolation(
                    rule.rule_id,
                    rule.scope,
                    f"{subject.subject_id}:{rule.rule_id}:{members[0][0]}-{members[-1][0]}",
                    reason_code,
                    f"{rule.name}：连续真实重量 {total} 超过上限 {limit}。",
                    RuleDisposition.PROHIBITED,
                    severity,
                )
            )
    return RuleContribution(tuple(violations), (MetricContribution(metric_key, maximum),))


@dataclass(frozen=True, slots=True)
class SyntheticWidthLimitRule(Rule):
    supported_scope = RuleScope.EDGE

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            require_decimal(
                self.parameters.get("maximum_increase"), "maximum_increase", nonnegative=True
            )

    def required_fields(self):
        return ("width",)

    def edge_semantic_fields(self):
        return ("width",) if self.enabled else ()

    def metric_keys(self):
        return ("synthetic_width_increase",)

    def evaluate(self, subject, context):
        if not isinstance(subject, EdgeRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        if subject.left.width is None or subject.right.width is None:
            return RuleContribution(
                (self._violation(subject.subject_id, "missing_width", Decimal(1)),), ()
            )
        increase = _positive_weight_difference(subject.right.width, subject.left.width)
        excess = _positive_weight_difference(increase, self.parameters["maximum_increase"])
        violations = (
            (self._violation(subject.subject_id, "synthetic_width_increase_exceeded", excess),)
            if excess > 0
            else ()
        )
        return RuleContribution(
            violations, (MetricContribution("synthetic_width_increase", increase),)
        )

    def _violation(self, subject_id, reason, severity):
        return RuleViolation(
            self.rule_id,
            self.scope,
            subject_id,
            reason,
            "合成宽度规则未满足。",
            RuleDisposition.PROHIBITED,
            severity,
        )


@dataclass(frozen=True, slots=True)
class SoftHardConnectionRule(Rule):
    supported_scope = RuleScope.EDGE

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            switches = ("virtual_sphc_allows_bridge", "transition_material_breaks_soft_hard")
            if set(self.parameters) != {*switches, "missing_grade_policy"}:
                raise ValueError(
                    "soft/hard rule requires two bridge switches and missing_grade_policy"
                )
            for name in switches:
                if type(self.parameters[name]) is not bool:
                    raise ValueError(f"{name} must be boolean")
            policy = self.parameters["missing_grade_policy"]
            require_text(policy, "missing_grade_policy")
            if policy.strip().lower() not in {
                "deny",
                "allow",
                "pass",
                "ignore",
                "fallback_same_hot_roll_grade",
            }:
                raise ValueError("unsupported missing_grade_policy")

    def required_fields(self):
        # Missing classification may use the explicitly configured hot-roll fallback.
        return ("hot_roll_grade",) if self.enabled else ()

    def edge_semantic_fields(self):
        return (
            ("material_role", "rule_attributes.soft_hard_class", "rule_attributes.hot_roll_grade")
            if self.enabled
            else ()
        )

    def evaluate(self, subject, context):
        if not isinstance(subject, EdgeRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        left, right = subject.left, subject.right
        roles = (left.material_role, right.material_role)
        # These switches decide the edge outright; a disabled bridge is not a fall-through.
        if MaterialRole.GENERATED_VIRTUAL in roles:
            allowed = self.parameters["virtual_sphc_allows_bridge"]
        elif MaterialRole.ACTUAL_TRANSITION in roles:
            allowed = self.parameters["transition_material_breaks_soft_hard"]
        else:
            left_class = _optional_text_attribute(left, "soft_hard_class").strip()
            right_class = _optional_text_attribute(right, "soft_hard_class").strip()
            if left_class and right_class:
                allowed = left_class == right_class
            else:
                policy = self.parameters["missing_grade_policy"].strip().lower()
                if policy == "fallback_same_hot_roll_grade":
                    left_hot = _optional_text_attribute(left, "hot_roll_grade")
                    right_hot = _optional_text_attribute(right, "hot_roll_grade")
                    allowed = bool(left_hot.strip() and right_hot.strip() and left_hot == right_hot)
                else:
                    allowed = policy in {"allow", "pass", "ignore"}
        if allowed:
            return RuleContribution((), ())
        return RuleContribution(
            (
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "soft_hard_connection_not_allowed",
                    f"{self.name}：材料类别、缺失类别策略或过渡材料开关不允许相邻连接。",
                    RuleDisposition.PROHIBITED,
                    Decimal(1),
                ),
            ),
            (),
        )


def _optional_text_attribute(node, name):
    value = node.rule_attributes.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{node.node_id}.{name} must be text or None")
    return value or ""


@dataclass(frozen=True, slots=True)
class TemperatureOverlapRule(Rule):
    supported_scope = RuleScope.EDGE

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            switches = ("ignore_temperature", "virtual_temperature_adaptive")
            if set(self.parameters) != {"min_overlap", *switches}:
                raise ValueError(
                    "temperature rule requires min_overlap and both temperature switches"
                )
            minimum = self.parameters["min_overlap"]
            require_decimal(minimum, "min_overlap", nonnegative=True)
            if not isfinite(float(minimum)):
                raise ValueError("min_overlap must have a finite float projection")
            for name in switches:
                if type(self.parameters[name]) is not bool:
                    raise ValueError(f"{name} must be boolean")

    def required_fields(self):
        if self.enabled and not self.parameters["ignore_temperature"]:
            return ("min_temperature", "max_temperature")
        return ()

    def edge_semantic_fields(self):
        if self.enabled and not self.parameters["ignore_temperature"]:
            return ("material_role", "min_temperature", "max_temperature")
        return ()

    def evaluate(self, subject, context):
        if not isinstance(subject, EdgeRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled or self.parameters["ignore_temperature"]:
            return RuleContribution((), ())
        nodes = (subject.left, subject.right)
        if self.parameters["virtual_temperature_adaptive"] and any(
            node.material_role is MaterialRole.GENERATED_VIRTUAL for node in nodes
        ):
            return RuleContribution((), ())
        fields = ("min_temperature", "max_temperature")
        if any(getattr(node, name) is None for node in nodes for name in fields):
            # Real-input completeness is checked before solving; virtual temperatures are derived.
            return RuleContribution((), ())
        values = []
        for node in nodes:
            for name in fields:
                value = float(getattr(node, name))
                if not isfinite(value):
                    raise ValueError(f"{node.node_id}.{name} must have a finite float projection")
                values.append(value)
        left_min, left_max, right_min, right_max = values
        overlap = min(left_max, right_max) - max(left_min, right_min)
        if not isfinite(overlap):
            raise ValueError(f"{subject.subject_id}: temperature overlap must be finite")
        minimum = float(self.parameters["min_overlap"])
        if overlap + 1e-9 >= minimum:
            return RuleContribution((), ())
        severity = max(1.0, (minimum - overlap) / max(minimum, 1e-9))
        if not isfinite(severity):
            raise ValueError(f"{subject.subject_id}: temperature severity must be finite")
        return RuleContribution(
            (
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "temperature",
                    f"{self.name}：温度交集 {overlap:g} 小于下限 {minimum:g}。",
                    RuleDisposition.PROHIBITED,
                    Decimal(str(severity)),
                ),
            ),
            (),
        )


@dataclass(frozen=True, slots=True)
class ThicknessTransitionRule(Rule):
    supported_scope = RuleScope.EDGE

    def __post_init__(self):
        Rule.__post_init__(self)
        if not self.enabled:
            return
        if set(self.parameters) != {"basis", "ranges", "fallback_tolerance"}:
            raise ValueError("thickness rule requires basis, ranges and fallback_tolerance")
        basis = self.parameters["basis"]
        require_text(basis, "basis")
        if basis.strip().lower() not in {"thinner", "thicker"}:
            raise ValueError("unsupported thickness basis")
        self._validate_tolerance(self.parameters["fallback_tolerance"], "fallback_tolerance")
        ranges = self.parameters["ranges"]
        if not isinstance(ranges, tuple):
            raise ValueError("ranges must be an ordered sequence")
        for index, band in enumerate(ranges):
            prefix = f"ranges[{index}]"
            names = {"min", "max", "include_min", "include_max", "tolerance", "calculation_mode"}
            if not isinstance(band, Mapping) or set(band) != names:
                raise ValueError(f"{prefix} requires bounds, inclusion flags, tolerance and mode")
            for name in ("min", "max"):
                value = band[name]
                require_decimal(value, f"{prefix}.{name}", allow_none=True)
                if value is not None and not isfinite(float(value)):
                    raise ValueError(f"{prefix}.{name} must have a finite float projection")
            if band["min"] is not None and band["max"] is not None and band["min"] > band["max"]:
                raise ValueError(f"{prefix}: thickness range is reversed")
            for name in ("include_min", "include_max"):
                if type(band[name]) is not bool:
                    raise ValueError(f"{prefix}.{name} must be boolean")
            self._validate_tolerance(band["tolerance"], f"{prefix}.tolerance")
            mode = band["calculation_mode"]
            require_text(mode, f"{prefix}.calculation_mode")
            if mode.strip().lower() not in {"absolute", "relative"}:
                raise ValueError(f"{prefix}: unsupported thickness calculation_mode")

    @staticmethod
    def _validate_tolerance(value, name):
        require_decimal(value, name, nonnegative=True)
        if not isfinite(float(value)):
            raise ValueError(f"{name} must have a finite float projection")

    def required_fields(self):
        return ("thickness",) if self.enabled else ()

    def edge_semantic_fields(self):
        return ("thickness",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, EdgeRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        if subject.left.thickness is None or subject.right.thickness is None:
            return RuleContribution((), ())
        values = []
        for node in (subject.left, subject.right):
            value = float(node.thickness)
            if not isfinite(value):
                raise ValueError(f"{node.node_id}.thickness must have a finite float projection")
            values.append(value)
        left, right = values
        basis = (
            max(values) if self.parameters["basis"].strip().lower() == "thicker" else min(values)
        )
        tolerance = float(self.parameters["fallback_tolerance"])
        for band in self.parameters["ranges"]:
            lower = None if band["min"] is None else float(band["min"])
            upper = None if band["max"] is None else float(band["max"])
            lower_ok = lower is None or (basis >= lower if band["include_min"] else basis > lower)
            upper_ok = upper is None or (basis <= upper if band["include_max"] else basis < upper)
            if lower_ok and upper_ok:
                tolerance = float(band["tolerance"])
                if band["calculation_mode"].strip().lower() == "relative":
                    tolerance *= basis
                break
        difference = abs(left - right)
        if not isfinite(tolerance) or not isfinite(difference):
            raise ValueError(f"{subject.subject_id}: thickness calculation must be finite")
        if difference <= tolerance + 1e-9:
            return RuleContribution((), ())
        severity = max(1.0, (difference - tolerance) / max(tolerance, 1e-9))
        if not isfinite(severity):
            raise ValueError(f"{subject.subject_id}: thickness severity must be finite")
        return RuleContribution(
            (
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "thickness",
                    f"{self.name}：厚度变化 {difference:g} 超过允许值 {tolerance:g}。",
                    RuleDisposition.PROHIBITED,
                    Decimal(str(severity)),
                ),
            ),
            (),
        )


@dataclass(frozen=True, slots=True)
class WidthTransitionRule(Rule):
    supported_scope = RuleScope.EDGE

    def __post_init__(self):
        Rule.__post_init__(self)
        _validate_width_parameters(self)

    def required_fields(self):
        return ("width",) if self.enabled else ()

    def edge_semantic_fields(self):
        return ("material_role", "width") if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, EdgeRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        return _width_contribution(
            self, subject.subject_id, subject.left, subject.right, "width_transition_exceeded"
        )


def _validate_width_parameters(rule):
    if not rule.enabled:
        return
    names = ("max_reverse_width", "virtual_width_tolerance")
    if set(rule.parameters) != set(names):
        raise ValueError("width rule requires only max_reverse_width and virtual_width_tolerance")
    for name in names:
        value = rule.parameters[name]
        require_decimal(value, name, nonnegative=True)
        if not isfinite(float(value)):
            raise ValueError(f"{name} must have a finite float projection")


def _width_contribution(rule, subject_id, left, right, reason):
    if left.width is None or right.width is None:
        reason, message, severity = "missing_width", "宽度检查所需的节点宽度缺失。", Decimal(1)
    else:
        left_width, right_width = float(left.width), float(right.width)
        if not isfinite(left_width) or not isfinite(right_width):
            reason, message, severity = (
                "invalid_width",
                "节点宽度超出当前浮点数值语义的有限范围。",
                Decimal(1),
            )
        else:
            virtual = MaterialRole.GENERATED_VIRTUAL in (left.material_role, right.material_role)
            limit = float(
                rule.parameters["virtual_width_tolerance" if virtual else "max_reverse_width"]
            )
            delta = abs(right_width - left_width) if virtual else right_width - left_width
            # Preserve the reference's subtraction order; moving limit to the left changes edges.
            if delta <= limit + 1e-9:
                return RuleContribution((), ())
            severity = Decimal(str(max(1.0, (delta - limit) / max(limit, 1.0))))
            message = f"{rule.name}：宽度变化 {delta:g} 超过上限 {limit:g}。"
    return RuleContribution(
        (
            RuleViolation(
                rule.rule_id,
                rule.scope,
                subject_id,
                reason,
                message,
                RuleDisposition.PROHIBITED,
                severity,
            ),
        ),
        (),
    )


@dataclass(frozen=True, slots=True)
class _VirtualBridgeWidthRule(Rule):
    """Internal chain dispatch of the same width configuration, never a second user rule."""

    supported_scope = RuleScope.CHAIN

    def __post_init__(self):
        Rule.__post_init__(self)
        _validate_width_parameters(self)

    def required_fields(self):
        return ("width",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, ChainRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        anchor = None
        anchor_position = -1
        violations = []
        for position, node in enumerate(subject.chain.nodes):
            if node.material_role is MaterialRole.GENERATED_VIRTUAL:
                continue
            if anchor is not None and position > anchor_position + 1:
                violations.extend(
                    _width_contribution(
                        self,
                        f"{subject.subject_id}:{self.rule_id}:virtual_anchor:"
                        f"{anchor_position}-{position}",
                        anchor,
                        node,
                        "virtual_bridge_reverse_width_exceeded",
                    ).violations
                )
            anchor, anchor_position = node, position
        return RuleContribution(tuple(violations), ())


@dataclass(frozen=True, slots=True)
class SyntheticNodePriorityRule(Rule):
    supported_scope = RuleScope.NODE

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            require_text(self.parameters.get("attribute"), "attribute")

    def required_fields(self):
        return (self.parameters["attribute"],) if self.enabled else ()

    def metric_keys(self):
        return ("synthetic_node_priority",)

    def construction_priority(self, node):
        if not self.enabled:
            return ()
        value = node.rule_attributes.get(self.parameters["attribute"])
        require_int(value, self.parameters["attribute"], minimum=None)
        return (value,)

    def evaluate(self, subject, context):
        if not isinstance(subject, NodeRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        return RuleContribution(
            (),
            (
                MetricContribution(
                    "synthetic_node_priority", self.construction_priority(subject.node)[0]
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class StrategicCustomerPriorityRule(Rule):
    supported_scope = RuleScope.NODE

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            if set(self.parameters) != {"contains_any", "rank", "default_rank"}:
                raise ValueError("customer priority requires contains_any, rank and default_rank")
            keywords = self.parameters["contains_any"]
            if not isinstance(keywords, tuple):
                raise ValueError("contains_any must be an ordered sequence")
            for keyword in keywords:
                require_text(keyword, "contains_any item")
            for name in ("rank", "default_rank"):
                require_int(self.parameters[name], name, minimum=None)

    def required_fields(self):
        return ()

    def metric_keys(self):
        return ("strategic_customer_rank",) if self.enabled else ()

    def construction_priority(self, node):
        if not self.enabled:
            return ()
        customer_name = _optional_text_attribute(node, "customer_name").strip()
        matched = any(keyword in customer_name for keyword in self.parameters["contains_any"])
        return (self.parameters["rank" if matched else "default_rank"],)

    def evaluate(self, subject, context):
        if not isinstance(subject, NodeRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        return RuleContribution(
            (),
            (
                MetricContribution(
                    self.metric_keys()[0], self.construction_priority(subject.node)[0]
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class HighSurfaceRunCountRule(Rule):
    supported_scope = RuleScope.CHAIN
    metric_aggregation = QualityAggregation.MAXIMUM

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            grades = self.parameters.get("surface_grades")
            if not isinstance(grades, tuple) or not grades:
                raise ValueError("surface_grades must be a nonempty ordered sequence")
            for grade in grades:
                require_text(grade, "surface_grades item")
            require_int(self.parameters.get("max_run_count"), "max_run_count")

    def required_fields(self):
        return ()

    def metric_keys(self):
        return ("max_high_surface_run_count",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, ChainRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        grades = frozenset(grade.strip().upper() for grade in self.parameters["surface_grades"])
        limit = self.parameters["max_run_count"]

        def matches(indexed_node):
            _, node = indexed_node
            if node.material_role is not MaterialRole.NORMAL_REAL:
                return False
            grade = _optional_text_attribute(node, "surface_grade")
            return grade.strip().upper() in grades

        violations = []
        maximum = 0
        for matched, segment in groupby(enumerate(subject.chain.nodes), matches):
            if not matched:
                continue
            start, _ = next(segment)
            count = 1 + sum(1 for _ in segment)
            maximum = max(maximum, count)
            if count > limit:
                violations.append(
                    RuleViolation(
                        self.rule_id,
                        self.scope,
                        f"{subject.subject_id}:{self.rule_id}:{start}-{start + count - 1}",
                        "high_surface_run_count",
                        f"高表面连续订单数 {count} 超过上限 {limit}。",
                        RuleDisposition.PROHIBITED,
                        Decimal(count - limit),
                    )
                )
        return RuleContribution(
            tuple(violations), (MetricContribution("max_high_surface_run_count", maximum),)
        )


def _is_narrow_real_node(node, configured_grade_class, width_limit):
    if node.material_role is not MaterialRole.NORMAL_REAL:
        return False
    grade_class = node.rule_attributes.get("grade_class")
    require_text(grade_class, f"{node.node_id}.grade_class")
    return (
        grade_class.strip() == configured_grade_class
        and node.width is not None
        and float(node.width) < width_limit
    )


@dataclass(frozen=True, slots=True)
class ContinuousNarrowSteelWeightRule(Rule):
    supported_scope = RuleScope.CHAIN
    metric_aggregation = QualityAggregation.MAXIMUM

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            require_text(self.parameters.get("grade_class"), "grade_class")
            require_decimal(
                self.parameters.get("width_upper_exclusive"), "width_upper_exclusive", positive=True
            )
            require_decimal(
                self.parameters.get("max_real_weight"), "max_real_weight", nonnegative=True
            )

    def required_fields(self):
        return ("grade_class",) if self.enabled else ()

    def metric_keys(self):
        return ("max_if_narrow_real_run_weight",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, ChainRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        width_limit = float(self.parameters["width_upper_exclusive"])

        def group_key(indexed_node):
            _, node = indexed_node
            return (
                True
                if _is_narrow_real_node(node, self.parameters["grade_class"], width_limit)
                else None
            )

        return _continuous_weight_contribution(
            self, subject, group_key, "max_if_narrow_real_run_weight", "if_narrow_run_weight"
        )


@dataclass(frozen=True, slots=True)
class SameSpecContinuousRealWeightRule(Rule):
    supported_scope = RuleScope.CHAIN
    metric_aggregation = QualityAggregation.MAXIMUM

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            fields = self.parameters.get("group_by_fields")
            if not isinstance(fields, tuple) or not fields:
                raise ValueError("group_by_fields must be a nonempty ordered sequence")
            for name in fields:
                if name not in ("thickness", "width", "grade"):
                    raise ValueError("group_by_fields supports only thickness, width and grade")
            if len(set(fields)) != len(fields):
                raise ValueError("group_by_fields must not contain duplicate fields")
            require_decimal(
                self.parameters.get("max_real_weight"), "max_real_weight", nonnegative=True
            )

    def required_fields(self):
        return ("grade",) if self.enabled else ()

    def metric_keys(self):
        return ("max_same_spec_real_run_weight",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, ChainRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())

        def group_key(indexed_node):
            _, node = indexed_node
            if node.material_role is not MaterialRole.NORMAL_REAL:
                return None
            require_text(node.grade, f"{node.node_id}.grade")
            values = []
            for name in self.parameters["group_by_fields"]:
                value = getattr(node, name)
                values.append(
                    value.strip().upper()
                    if name == "grade"
                    else None
                    if value is None
                    else float(value)
                )
            return tuple(values)

        return _continuous_weight_contribution(
            self, subject, group_key, "max_same_spec_real_run_weight", "same_spec_run_weight"
        )


@dataclass(frozen=True, slots=True)
class ChainWeightRangeRule(Rule):
    supported_scope = RuleScope.CHAIN

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            require_decimal(self.parameters.get("min_weight"), "min_weight", nonnegative=True)
            require_decimal(self.parameters.get("max_weight"), "max_weight", positive=True)
            require_decimal(self.parameters.get("target_weight"), "target_weight", nonnegative=True)
            if self.parameters["min_weight"] > self.parameters["max_weight"]:
                raise ValueError("min_weight must not exceed max_weight")

    def required_fields(self):
        return ()

    def metric_keys(self):
        return (
            (
                "underweight_chain_count",
                "underweight_total_gap",
                "overweight_chain_count",
                "overweight_total_excess",
                "chain_target_weight_deviation",
            )
            if self.enabled
            else ()
        )

    def evaluate(self, subject, context):
        if not isinstance(subject, ChainRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        total = subject.chain.total_weight
        minimum = self.parameters["min_weight"]
        maximum = self.parameters["max_weight"]
        target = self.parameters["target_weight"]
        gap = _positive_weight_difference(minimum, total)
        excess = _positive_weight_difference(total, maximum)
        underweight = gap > WEIGHT_EPSILON
        overweight = excess > WEIGHT_EPSILON
        violations = []
        if underweight:
            violations.append(
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "chain_weight_below_minimum",
                    f"链重 {total} 低于下限 {minimum}。",
                    RuleDisposition.ALLOWED_FINAL_DEVIATION,
                    gap,
                )
            )
        if overweight:
            violations.append(
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "chain_weight_above_maximum",
                    f"链重 {total} 超过上限 {maximum}。",
                    RuleDisposition.PROHIBITED,
                    excess,
                )
            )
        values = (
            int(underweight),
            gap if underweight else Decimal(0),
            int(overweight),
            excess if overweight else Decimal(0),
            _positive_weight_difference(max(total, target), min(total, target)),
        )
        return RuleContribution(
            tuple(violations),
            tuple(MetricContribution(key, value) for key, value in zip(self.metric_keys(), values)),
        )


@dataclass(frozen=True, slots=True)
class ConsecutiveVirtualMaterialRule(Rule):
    supported_scope = RuleScope.CHAIN
    metric_aggregation = QualityAggregation.MAXIMUM

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            if set(self.parameters) != {"max_count"}:
                raise ValueError("consecutive virtual material requires only max_count")
            require_int(self.parameters["max_count"], "max_count")

    def required_fields(self):
        return ()

    def metric_keys(self):
        return ("max_consecutive_virtual_sphc",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, ChainRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        limit = self.parameters["max_count"]
        violations = []
        maximum = 0
        for virtual, segment in groupby(
            enumerate(subject.chain.nodes),
            lambda item: item[1].material_role is MaterialRole.GENERATED_VIRTUAL,
        ):
            if not virtual:
                continue
            start, _ = next(segment)
            count = 1 + sum(1 for _ in segment)
            maximum = max(maximum, count)
            if count > limit:
                violations.append(
                    RuleViolation(
                        self.rule_id,
                        self.scope,
                        f"{subject.subject_id}:{self.rule_id}:{start}-{start + count - 1}",
                        "virtual_sphc_run",
                        f"连续虚拟材料数量 {count} 超过上限 {limit}。",
                        RuleDisposition.PROHIBITED,
                        Decimal(count - limit),
                    )
                )
        return RuleContribution(
            tuple(violations), (MetricContribution(self.metric_keys()[0], maximum),)
        )


@dataclass(frozen=True, slots=True)
class ReverseWidthCountRule(Rule):
    supported_scope = RuleScope.CHAIN

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            if set(self.parameters) != {"max_count"}:
                raise ValueError("reverse width count requires only max_count")
            require_int(self.parameters["max_count"], "max_count")

    def required_fields(self):
        return ()

    def metric_keys(self):
        return ("reverse_width_count",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, ChainRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        count = 0
        baseline = None
        for node in subject.chain.nodes:
            if node.width is None:
                baseline = None
                continue
            width = float(node.width)
            if not isfinite(width):
                raise ValueError(f"{node.node_id}.width must have a finite float projection")
            if baseline is not None and width > baseline + 1e-9:
                # Reference counts against the retained baseline, not the previous node.
                count += 1
            else:
                baseline = width
        limit = self.parameters["max_count"]
        violations = (
            (
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "reverse_width",
                    f"链内逆宽次数 {count} 超过上限 {limit}。",
                    RuleDisposition.PROHIBITED,
                    Decimal(count - limit),
                ),
            )
            if count > limit
            else ()
        )
        return RuleContribution(violations, (MetricContribution(self.metric_keys()[0], count),))


@dataclass(frozen=True, slots=True)
class ConsecutiveReverseWidthRule(Rule):
    supported_scope = RuleScope.CHAIN

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled and self.parameters:
            raise ValueError("consecutive reverse width requires empty parameters")

    def required_fields(self):
        return ()

    def metric_keys(self):
        return ("consecutive_reverse_width_violation_count",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, ChainRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        violations = []
        previous_reverse = False
        for position, (left, right) in enumerate(pairwise(subject.chain.nodes), start=1):
            # Continuous widening concerns adjacent edges, not the earlier reference baseline.
            reverse = (
                left.width is not None
                and right.width is not None
                and float(right.width) > float(left.width) + 1e-9
            )
            if previous_reverse and reverse:
                violations.append(
                    RuleViolation(
                        self.rule_id,
                        self.scope,
                        f"{subject.subject_id}:{self.rule_id}:{position - 1}-{position}",
                        "consecutive_reverse_width",
                        "不允许连续两个相邻连接均由窄到宽。",
                        RuleDisposition.PROHIBITED,
                        Decimal(1),
                    )
                )
            previous_reverse = reverse
        return RuleContribution(
            tuple(violations),
            (MetricContribution(self.metric_keys()[0], len(violations)),),
        )


@dataclass(frozen=True, slots=True)
class LateOriginalPeriodMoveRule(Rule):
    supported_scope = RuleScope.PLAN

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled and self.parameters:
            raise ValueError("late original period rule requires empty parameters")

    def required_fields(self):
        return ("source_period",) if self.enabled else ()

    def metric_keys(self):
        return ("late_original_due_period_move_count",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, PlanRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        if not isinstance(context, RuleEvaluationContext):
            raise ValueError("context must be RuleEvaluationContext")
        violations = []
        for chain in subject.plan.chains:
            assigned = context.period_index.get(chain.assigned_period)
            if assigned is None:
                raise ValueError(f"{chain.chain_id}.assigned_period is unknown")
            for node in chain.nodes:
                if node.material_role is MaterialRole.GENERATED_VIRTUAL:
                    continue
                source = context.period_index.get(node.source_period)
                if source is None:
                    raise ValueError(f"{node.node_id}.source_period is unknown")
                if assigned > source:
                    violations.append(
                        RuleViolation(
                            self.rule_id,
                            self.scope,
                            node.node_id,
                            "late_original_due_period_move",
                            f"订单来源计划期 {node.source_period}，被排至更晚的 {chain.assigned_period}。",
                            RuleDisposition.PROHIBITED,
                            Decimal(1),
                        )
                    )
        return RuleContribution(
            tuple(violations),
            (MetricContribution(self.metric_keys()[0], len(violations)),),
        )


@dataclass(frozen=True, slots=True)
class VirtualOutputRatioRule(Rule):
    supported_scope = RuleScope.PLAN

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            if set(self.parameters) != {"max_ratio"}:
                raise ValueError("virtual output ratio rule requires only max_ratio")
            require_decimal(self.parameters["max_ratio"], "max_ratio", nonnegative=True)

    def required_fields(self):
        return ()

    def metric_keys(self):
        return ("virtual_output_weight_ratio",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, PlanRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        view = subject.resource_view
        if not isinstance(view, EvaluationResourceView):
            raise ValueError(f"{subject.subject_id}.resource_view must be EvaluationResourceView")
        total = sum_weights((view.scheduled_real_weight, view.generated_virtual_weight))
        # Freeze reference division precision without rounding the authoritative weight totals.
        arithmetic = Context(prec=28, rounding=ROUND_HALF_EVEN)
        ratio = arithmetic.divide(view.generated_virtual_weight, total) if total else Decimal(0)
        limit = self.parameters["max_ratio"]
        violations = ()
        if ratio > limit and ratio > arithmetic.add(limit, WEIGHT_EPSILON):
            violations = (
                RuleViolation(
                    self.rule_id,
                    self.scope,
                    subject.subject_id,
                    "virtual_budget",
                    f"{self.name}：虚拟材料占最终总重量的比例 {ratio} 超过上限 {limit}。",
                    RuleDisposition.PROHIBITED,
                    arithmetic.subtract(ratio, limit),
                ),
            )
        return RuleContribution(violations, (MetricContribution(self.metric_keys()[0], ratio),))


@dataclass(frozen=True, slots=True)
class InterChainWidthGapRule(Rule):
    supported_scope = RuleScope.PLAN

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled and self.parameters:
            raise ValueError("inter-chain width gap requires empty parameters")

    def required_fields(self):
        return ("width",) if self.enabled else ()

    def metric_keys(self):
        return ("inter_chain_width_gap",) if self.enabled else ()

    def boundary_contributions(
        self, subject: PlanRuleSubject, context: RuleEvaluationContext
    ) -> tuple[tuple[str, str, MetricContribution], ...]:
        """Derive named boundaries without storing a second plan or changing its order."""
        if not isinstance(subject, PlanRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return ()
        if not isinstance(context, RuleEvaluationContext):
            raise ValueError("context must be RuleEvaluationContext")
        previous_period = -1
        for chain in subject.plan.chains:
            period = context.period_index.get(chain.assigned_period)
            if period is None:
                raise ValueError(f"{chain.chain_id}.assigned_period is unknown")
            if period < previous_period:
                raise ValueError("inter-chain width gap requires production period order")
            previous_period = period
            for node in (chain.nodes[0], chain.nodes[-1]):
                require_decimal(node.width, f"{node.node_id}.width", positive=True)
        return tuple(
            (
                left.chain_id,
                right.chain_id,
                MetricContribution(
                    self.metric_keys()[0],
                    _positive_weight_difference(
                        max(left.nodes[-1].width, right.nodes[0].width),
                        min(left.nodes[-1].width, right.nodes[0].width),
                    ),
                ),
            )
            for left, right in pairwise(subject.plan.chains)
        )

    def evaluate(self, subject, context):
        boundaries = self.boundary_contributions(subject, context)
        if not self.enabled:
            return RuleContribution((), ())
        metrics = tuple(item for _, _, item in boundaries)
        return RuleContribution(
            (), metrics or (MetricContribution(self.metric_keys()[0], Decimal(0)),)
        )


@dataclass(frozen=True, slots=True)
class FutureFillWeightTargetRule(Rule):
    supported_scope = RuleScope.PLAN

    def __post_init__(self):
        Rule.__post_init__(self)
        if self.enabled:
            if set(self.parameters) != {"future_fill_weight_target"}:
                raise ValueError("future fill rule requires only future_fill_weight_target")
            require_decimal(
                self.parameters["future_fill_weight_target"],
                "future_fill_weight_target",
                nonnegative=True,
            )

    def required_fields(self):
        return ()

    def metric_keys(self):
        return ("future_fill_total_gap",) if self.enabled else ()

    def evaluate(self, subject, context):
        if not isinstance(subject, PlanRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not self.enabled:
            return RuleContribution((), ())
        target = self.parameters["future_fill_weight_target"]
        # Clamp each chain before summing: a fuller chain cannot offset another's shortfall.
        gap = sum_weights(
            _positive_weight_difference(target, chain.total_weight) for chain in subject.plan.chains
        )
        return RuleContribution((), (MetricContribution(self.metric_keys()[0], gap),))


@dataclass(frozen=True, slots=True)
class ControlledOrderSplitRule(Rule):
    supported_scope = RuleScope.ACTION_ELIGIBILITY

    def __post_init__(self):
        Rule.__post_init__(self)
        if not self.enabled:
            return
        if set(self.parameters) != {
            "grade_class",
            "width_upper_exclusive",
            "maximum_piece_weight",
            "minimum_piece_weight",
            "maximum_accepted_source_count",
            "maximum_separator_node_count",
            "maximum_separator_weight",
            "allowed_modes",
        }:
            raise ValueError("controlled split requires exactly its eight qualification parameters")
        require_text(self.parameters["grade_class"], "grade_class")
        for key in ("width_upper_exclusive", "maximum_piece_weight", "minimum_piece_weight"):
            require_decimal(self.parameters[key], key, positive=True)
        if not isfinite(float(self.parameters["width_upper_exclusive"])):
            raise ValueError("width_upper_exclusive must have a finite float projection")
        if self.parameters["minimum_piece_weight"] > self.parameters["maximum_piece_weight"]:
            raise ValueError("minimum_piece_weight exceeds maximum_piece_weight")
        for key in ("maximum_accepted_source_count", "maximum_separator_node_count"):
            require_int(self.parameters[key], key)
        require_decimal(
            self.parameters["maximum_separator_weight"],
            "maximum_separator_weight",
            nonnegative=True,
        )
        modes = self.parameters["allowed_modes"]
        if not isinstance(modes, tuple) or any(
            type(mode) is not str or mode not in tuple(item.value for item in ControlledSplitMode)
            for mode in modes
        ):
            raise ValueError("allowed_modes must contain only controlled split mode strings")

    def required_fields(self):
        return ("grade_class",) if self.enabled else ()

    def evaluate_controlled_split(
        self, subject: ControlledSplitRuleSubject, context: RuleEvaluationContext
    ) -> ControlledSplitDecision:
        if not isinstance(subject, ControlledSplitRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not isinstance(context, RuleEvaluationContext):
            raise ValueError("controlled split requires RuleEvaluationContext")

        def reject(reason):
            return create_controlled_split_decision(
                subject,
                context,
                eligible=False,
                rule_id=self.rule_id,
                rule_version=self.version,
                reason_code=reason,
            )

        if not self.enabled:
            return reject("controlled_order_split_disabled")
        node = subject.parent_node
        if node.material_role is not MaterialRole.NORMAL_REAL:
            return reject("unsupported_material_role")
        if node.split_lineage is not None:
            return reject("already_split")
        if (
            subject.source_period != node.source_period
            or subject.source_period not in context.period_index
            or subject.origin_assigned_period not in context.period_index
        ):
            return reject("invalid_period_relation")
        if subject.accepted_split_source_count >= self.parameters["maximum_accepted_source_count"]:
            return reject("split_source_limit")
        source_index = context.period_index[subject.source_period]
        origin_index = context.period_index[subject.origin_assigned_period]
        if source_index < origin_index:
            return reject("source_already_late")
        mode = (
            ControlledSplitMode.SAME_PERIOD_SPLIT
            if source_index == origin_index
            else ControlledSplitMode.FUTURE_BORROW_RETURN
        )
        if mode.value not in self.parameters["allowed_modes"]:
            return reject("split_mode_not_allowed")
        if not _is_narrow_real_node(
            node, self.parameters["grade_class"], float(self.parameters["width_upper_exclusive"])
        ):
            return reject("not_narrow_real_order")
        if node.weight <= sum_weights((self.parameters["maximum_piece_weight"], WEIGHT_EPSILON)):
            return reject("source_weight_within_limit")
        # Qualification returns limits; the action checks the actual tail and separators.
        return create_controlled_split_decision(
            subject,
            context,
            eligible=True,
            rule_id=self.rule_id,
            rule_version=self.version,
            reason_code=mode.value,
            mode=mode,
            target_assigned_period=subject.source_period,
            maximum_piece_weight=self.parameters["maximum_piece_weight"],
            minimum_piece_weight=self.parameters["minimum_piece_weight"],
            maximum_accepted_source_count=self.parameters["maximum_accepted_source_count"],
            maximum_separator_node_count=self.parameters["maximum_separator_node_count"],
            maximum_separator_weight=self.parameters["maximum_separator_weight"],
        )
