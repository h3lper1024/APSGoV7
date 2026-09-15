"""Immutable enabled-rule dispatch, declarations and task-independent identity."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from types import MappingProxyType

from ..contracts import freeze_tuple, require_decimal, require_text
from ..model import Node
from .base import (
    ChainRuleSubject,
    ControlledSplitDecision,
    ControlledSplitRuleSubject,
    EdgeRuleSubject,
    NodeRuleSubject,
    PlanRuleSubject,
    QualityAggregation,
    QualityCriterion,
    QualityDirection,
    NumericProjection,
    Rule,
    RuleContribution,
    RuleEvaluationContext,
    RuleScope,
    UnsupportedRuleSubjectError,
)
from .concrete import (ControlledOrderSplitRule, DeliveryDuePerformanceRule, WidthTransitionRule,
                       _VirtualBridgeWidthRule, HighSurfaceRunCountRule, ConsecutiveVirtualMaterialRule,
                       ContinuousNarrowSteelWeightRule, SameSpecContinuousRealWeightRule)
from .helpers import create_controlled_split_decision

PROHIBITED_METRIC_KEYS = ("prohibited_violation_count", "prohibited_violation_severity")
STRUCTURAL_METRIC_KEYS = frozenset(
    (*PROHIBITED_METRIC_KEYS, "chain_count", "generated_virtual_weight", "borrowed_future_weight")
)


@dataclass(frozen=True, slots=True)
class ProcessRuleSet:
    product_line_code: str
    process_code: str
    scenario: str
    version: str
    rules: tuple[Rule, ...]
    quality_spec: tuple[QualityCriterion, ...]
    allowed_final_deviation_codes: frozenset[str]
    # The application loader verifies the complete specification, including disabled entries.
    fingerprint: str
    _by_scope: Mapping[RuleScope, tuple[Rule, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        for name in ("product_line_code", "process_code", "scenario", "version", "fingerprint"):
            require_text(getattr(self, name), name)
        rules = freeze_tuple(self.rules, Rule, "rules")
        if any(isinstance(rule, _VirtualBridgeWidthRule) for rule in rules):
            raise ValueError(
                "virtual bridge width checks are derived, not independently configured"
            )
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise ValueError("duplicate rule identity")
        if any(Rule not in type(rule).__bases__ for rule in rules):
            raise ValueError("concrete rules must inherit directly from Rule")
        criteria = freeze_tuple(self.quality_spec, QualityCriterion, "quality_spec")
        if len({item.criterion_id for item in criteria}) != len(criteria):
            raise ValueError("duplicate quality criterion identity")
        if tuple(item.metric_key for item in criteria[:2]) != PROHIBITED_METRIC_KEYS or any(
            item.direction is not QualityDirection.MINIMIZE for item in criteria[:2]
        ):
            raise ValueError("prohibited count and severity must be the first minimizing criteria")
        codes = self.allowed_final_deviation_codes
        if not isinstance(codes, (tuple, list, frozenset, set)):
            raise ValueError("allowed_final_deviation_codes must be a collection")
        for code in codes:
            require_text(code, "allowed_final_deviation_code")
        enabled = tuple(rule for rule in rules if rule.enabled)
        if any(isinstance(rule, DeliveryDuePerformanceRule) for rule in enabled):
            backlog_priority = any(
                isinstance(rule, DeliveryDuePerformanceRule) and rule.include_backlog_clearance
                for rule in enabled
            )
            delivery_metrics = (
                "old_backlog_last_completion_hours" if backlog_priority else "newly_late_original_weight",
                "delivery_wait_tardiness_tonne_hours",
            )
            second_precision = any(isinstance(rule, DeliveryDuePerformanceRule) and rule.second_precision
                                   for rule in enabled)
            prefix = (
                *PROHIBITED_METRIC_KEYS, *delivery_metrics,
                "underweight_chain_count", "underweight_total_gap",
            ) if backlog_priority and not second_precision else (
                *PROHIBITED_METRIC_KEYS, "underweight_chain_count", "underweight_total_gap",
                *delivery_metrics,
            )
            keys = tuple(item.metric_key for item in criteria)
            suffix = tuple(key for key in ("inter_chain_width_gap", "generated_virtual_weight", "chain_count") if key in keys)
            if keys != (*prefix, *suffix) or any(
                item.direction is not QualityDirection.MINIMIZE for item in criteria
            ) or any(
                item.aggregation is not QualityAggregation.SUM
                or item.numeric_projection is not NumericProjection.EXACT_DECIMAL
                for item in criteria if item.metric_key in delivery_metrics
            ):
                raise ValueError("delivery optimization requires the approved nine-level quality order")
        producers = set(STRUCTURAL_METRIC_KEYS)
        for rule in enabled:
            if not isinstance(
                rule.metric_aggregation, QualityAggregation
            ) or rule.metric_aggregation not in (
                QualityAggregation.SUM,
                QualityAggregation.MAXIMUM,
            ):
                raise ValueError("rule metric_aggregation must be SUM or MAXIMUM")
            keys = freeze_tuple(rule.metric_keys(), str, "metric_keys")
            for key in keys:
                require_text(key, "metric_key")
                if key in producers:
                    raise ValueError(f"duplicate metric producer: {key}")
                producers.add(key)
        for item in criteria:
            if item.metric_key not in producers:
                raise ValueError(f"quality metric has no enabled producer: {item.metric_key}")
        by_scope = {scope: [] for scope in RuleScope}
        for rule in enabled:
            by_scope[rule.scope].append(rule)
            if isinstance(rule, WidthTransitionRule):
                # One business identity/configuration, two existing scope dispatch entry points.
                by_scope[RuleScope.CHAIN].append(
                    _VirtualBridgeWidthRule(
                        rule.rule_id,
                        rule.name,
                        RuleScope.CHAIN,
                        True,
                        rule.version,
                        rule.parameters,
                    )
                )
        actions = by_scope[RuleScope.ACTION_ELIGIBILITY]
        if len(actions) > 1:
            raise ValueError("controlled_order_split requires at most one enabled producer")
        if actions and type(actions[0]) is not ControlledOrderSplitRule:
            raise ValueError("controlled_order_split requires ControlledOrderSplitRule")
        object.__setattr__(self, "rules", enabled)
        object.__setattr__(self, "quality_spec", criteria)
        object.__setattr__(self, "allowed_final_deviation_codes", frozenset(codes))
        object.__setattr__(
            self,
            "_by_scope",
            MappingProxyType({scope: tuple(rules) for scope, rules in by_scope.items()}),
        )

    def rules_for_scope(self, scope: RuleScope) -> tuple[Rule, ...]:
        if not isinstance(scope, RuleScope):
            raise ValueError("scope must be RuleScope")
        return self._by_scope[scope]

    def metric_aggregations(self) -> Mapping[str, QualityAggregation]:
        return MappingProxyType(
            {key: rule.metric_aggregation for rule in self.rules for key in rule.metric_keys()}
        )

    def _evaluate(self, subject, context, expected_type, scope, *, rules=None, _run_cache=None,
                  _delivery_cache=None):
        if not isinstance(subject, expected_type):
            raise UnsupportedRuleSubjectError(type(subject))
        if not isinstance(context, RuleEvaluationContext):
            raise ValueError("context must be RuleEvaluationContext")
        violations, metrics = [], []
        for rule in self._by_scope[scope] if rules is None else rules:
            if _run_cache is not None and type(rule) in (
                HighSurfaceRunCountRule, ConsecutiveVirtualMaterialRule,
                ContinuousNarrowSteelWeightRule, SameSpecContinuousRealWeightRule,
            ):
                contribution = rule.evaluate(subject, context, _run_cache=_run_cache)
            elif _delivery_cache is not None and type(rule) is DeliveryDuePerformanceRule:
                contribution = rule.evaluate(subject, context, _delivery_cache=_delivery_cache)
            else:
                contribution = rule.evaluate(subject, context)
            if not isinstance(contribution, RuleContribution):
                raise ValueError("rule must return RuleContribution")
            if any(
                item.rule_id != rule.rule_id or item.scope is not scope
                for item in contribution.violations
            ):
                raise ValueError("rule violation identity does not match its producer and scope")
            declared = rule.metric_keys()
            if any(item.metric_key not in declared for item in contribution.metrics):
                raise ValueError("rule produced an undeclared metric")
            violations.extend(contribution.violations)
            metrics.extend(contribution.metrics)
        return RuleContribution(tuple(violations), tuple(metrics))

    def evaluate_node(self, subject: NodeRuleSubject, context: RuleEvaluationContext):
        return self._evaluate(subject, context, NodeRuleSubject, RuleScope.NODE)

    def evaluate_edge(self, subject: EdgeRuleSubject, context: RuleEvaluationContext):
        return self._evaluate(subject, context, EdgeRuleSubject, RuleScope.EDGE)

    def evaluate_chain(self, subject: ChainRuleSubject, context: RuleEvaluationContext):
        return self._evaluate(subject, context, ChainRuleSubject, RuleScope.CHAIN)

    def evaluate_complete_chain(
        self, subject: ChainRuleSubject, context: RuleEvaluationContext, *, _run_cache=None
    ) -> RuleContribution:
        if not self._by_scope[RuleScope.EDGE]:
            if _run_cache is None:
                return self.evaluate_chain(subject, context)
            return self._evaluate(subject, context, ChainRuleSubject, RuleScope.CHAIN, _run_cache=_run_cache)
        first_edge = next(
            index for index, rule in enumerate(self.rules) if rule.scope is RuleScope.EDGE
        )
        split_index = sum(rule.scope is RuleScope.CHAIN for rule in self.rules[:first_edge])
        chain_rules = self._by_scope[RuleScope.CHAIN]
        contributions = [
            self._evaluate(
                subject, context, ChainRuleSubject, RuleScope.CHAIN, rules=chain_rules[:split_index],
                _run_cache=_run_cache
            )
        ]
        for left, right in pairwise(subject.chain.nodes):
            edge = EdgeRuleSubject(
                f"{subject.subject_id}:{left.node_id}>{right.node_id}", left, right
            )
            contributions.append(self.evaluate_edge(edge, context))
        contributions.append(
            self._evaluate(
                subject, context, ChainRuleSubject, RuleScope.CHAIN, rules=chain_rules[split_index:],
                _run_cache=_run_cache
            )
        )
        return RuleContribution(
            tuple(item for contribution in contributions for item in contribution.violations),
            tuple(item for contribution in contributions for item in contribution.metrics),
        )

    def evaluate_plan(self, subject: PlanRuleSubject, context: RuleEvaluationContext, *, _delivery_cache=None):
        # This entry dispatches PLAN rules only; complete plan evaluation belongs to function 7.
        return self._evaluate(subject, context, PlanRuleSubject, RuleScope.PLAN, _delivery_cache=_delivery_cache)

    def construction_priority(self, node: Node) -> tuple[Decimal | int, ...]:
        if not isinstance(node, Node):
            raise ValueError("node must be Node")
        priority = []
        for rule in self._by_scope[RuleScope.NODE]:
            values = rule.construction_priority(node)
            if not isinstance(values, tuple):
                raise ValueError("construction priority must be an immutable tuple")
            for value in values:
                if isinstance(value, Decimal):
                    require_decimal(value, "construction_priority")
                elif type(value) is not int:
                    raise ValueError("construction priority must contain Decimal or int")
            priority.extend(values)
        return tuple(priority)

    def evaluate_controlled_split(
        self, subject: ControlledSplitRuleSubject, context: RuleEvaluationContext
    ) -> ControlledSplitDecision:
        if not isinstance(subject, ControlledSplitRuleSubject):
            raise UnsupportedRuleSubjectError(type(subject))
        if not isinstance(context, RuleEvaluationContext):
            raise ValueError("context must be RuleEvaluationContext")
        producers = self._by_scope[RuleScope.ACTION_ELIGIBILITY]
        if not producers:
            return create_controlled_split_decision(
                subject,
                context,
                eligible=False,
                rule_id="controlled_order_split",
                rule_version=self.version,
                reason_code="controlled_order_split_disabled",
            )
        producer = producers[0]
        decision = producer.evaluate_controlled_split(subject, context)
        if not isinstance(decision, ControlledSplitDecision):
            raise ValueError("split producer must return ControlledSplitDecision")
        if decision.rule_id != producer.rule_id or decision.rule_version != producer.version:
            raise ValueError("split decision identity does not match its producer")
        return decision
