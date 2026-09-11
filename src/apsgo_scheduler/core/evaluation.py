"""Pure complete evaluation; raw rule metrics and comparison values stay separate."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from math import isfinite

from .contracts import (
    freeze_scalars,
    freeze_tuple,
    require_decimal,
    require_int,
    require_text,
    sum_decimals,
    sum_weights,
)
from .model import Chain, SchedulePlan, SearchState
from .resource_facts import derive_evaluation_resource_view
from .rules import concrete
from .rules.base import (
    ChainRuleSubject,
    NodeRuleSubject,
    NumericProjection,
    PlanRuleSubject,
    QualityAggregation,
    QualityDirection,
    RuleContribution,
    RuleDisposition,
    RuleEvaluationContext,
    RuleScope,
    RuleViolation,
)
from .rules.rule_set import ProcessRuleSet


def _require_number(value, name):
    if isinstance(value, Decimal):
        require_decimal(value, name)
    else:
        require_int(value, name, minimum=None)


def _freeze_metrics(metrics):
    values = freeze_scalars(metrics)
    for key, value in values.items():
        _require_number(value, key)
    return values


@dataclass(frozen=True, slots=True)
class ChainSummary:
    assigned_period: str
    total_weight: Decimal
    real_weight: Decimal
    virtual_weight: Decimal
    real_node_count: int
    virtual_node_count: int
    split_piece_count: int

    def __post_init__(self):
        require_text(self.assigned_period, "assigned_period")
        for name in ("total_weight", "real_weight", "virtual_weight"):
            require_decimal(getattr(self, name), name, nonnegative=True)
        require_int(self.real_node_count, "real_node_count", minimum=1)
        require_int(self.virtual_node_count, "virtual_node_count")
        require_int(self.split_piece_count, "split_piece_count")
        if self.total_weight != sum_weights((self.real_weight, self.virtual_weight)):
            raise ValueError("summary total must equal real plus virtual weight")
        if self.split_piece_count > self.real_node_count:
            raise ValueError("split pieces must be real nodes")


@dataclass(frozen=True, slots=True)
class ChainEvaluation:
    chain_id: str
    summary: ChainSummary
    violations: tuple[RuleViolation, ...]
    metrics: Mapping[str, Decimal | int]

    def __post_init__(self):
        require_text(self.chain_id, "chain_id")
        if not isinstance(self.summary, ChainSummary):
            raise ValueError("chain evaluation requires ChainSummary")
        object.__setattr__(
            self, "violations", freeze_tuple(self.violations, RuleViolation, "violations")
        )
        object.__setattr__(self, "metrics", _freeze_metrics(self.metrics))


@dataclass(frozen=True, slots=True)
class PlanEvaluation:
    chain_evaluations: tuple[ChainEvaluation, ...]
    violations: tuple[RuleViolation, ...]
    metrics: Mapping[str, Decimal | int]
    quality_key: tuple[Decimal | int, ...]

    def __post_init__(self):
        chains = freeze_tuple(self.chain_evaluations, ChainEvaluation, "chain_evaluations")
        if len({chain.chain_id for chain in chains}) != len(chains):
            raise ValueError("duplicate evaluated chain identity")
        quality = freeze_tuple(self.quality_key, (Decimal, int), "quality_key")
        for value in quality:
            _require_number(value, "quality_key")
        object.__setattr__(self, "chain_evaluations", chains)
        object.__setattr__(
            self, "violations", freeze_tuple(self.violations, RuleViolation, "violations")
        )
        object.__setattr__(self, "metrics", _freeze_metrics(self.metrics))
        object.__setattr__(self, "quality_key", quality)


@dataclass(frozen=True, slots=True)
class _ChainEvaluationEntry:
    chain: Chain
    contribution: RuleContribution
    node_contributions: tuple[RuleContribution, ...]
    evaluation: ChainEvaluation


@dataclass(frozen=True, slots=True, eq=False)
class _AcceptedPlanEvaluation:
    state: SearchState
    plan: SchedulePlan
    rule_set: ProcessRuleSet
    rule_context: RuleEvaluationContext
    entries: Mapping[str, _ChainEvaluationEntry]

    def matches(self, state, rule_set, rule_context):
        return (
            self.state is state
            and self.plan is state.current_plan
            and self.rule_set is rule_set
            and self.rule_context is rule_context
        )


# Only audited concrete implementations; unknown extensions keep uncached evaluation.
_REUSABLE_RULE_TYPES = frozenset((
    concrete.SyntheticWidthLimitRule,
    concrete.SoftHardConnectionRule,
    concrete.TemperatureOverlapRule,
    concrete.ThicknessTransitionRule,
    concrete.WidthTransitionRule,
    concrete.SyntheticNodePriorityRule,
    concrete.StrategicCustomerPriorityRule,
    concrete.HighSurfaceRunCountRule,
    concrete.ContinuousNarrowSteelWeightRule,
    concrete.SameSpecContinuousRealWeightRule,
    concrete.ChainWeightRangeRule,
    concrete.ConsecutiveVirtualMaterialRule,
    concrete.ReverseWidthCountRule,
    concrete.ConsecutiveReverseWidthRule,
    concrete._VirtualBridgeWidthRule,
    concrete.LateOriginalPeriodMoveRule,
    concrete.VirtualOutputRatioRule,
    concrete.InterChainWidthGapRule,
    concrete.DeliveryDuePerformanceRule,
    concrete.FutureFillWeightTargetRule,
    concrete.ControlledOrderSplitRule,
))


def _evaluate_candidate_plan(plan, state, rule_set, rule_context, previous):
    if type(rule_set) is not ProcessRuleSet or any(
        type(rule) not in _REUSABLE_RULE_TYPES
        for scope in RuleScope
        for rule in rule_set.rules_for_scope(scope)
    ):
        return evaluate_plan(plan, rule_set, rule_context), None
    entries = (
        previous.entries
        if previous is not None and previous.matches(state, rule_set, rule_context)
        else {}
    )
    evaluation, candidate_entries = _evaluate_plan(plan, rule_set, rule_context, entries)
    # Prepared before acceptance; a rejected candidate never becomes long-lived state.
    return evaluation, _AcceptedPlanEvaluation(
        state, plan, rule_set, rule_context, candidate_entries
    )


def _validate_inputs(subject, subject_type, rule_set, context):
    if not isinstance(subject, subject_type):
        raise ValueError(f"evaluation requires {subject_type.__name__}")
    if not isinstance(rule_set, ProcessRuleSet):
        raise ValueError("evaluation requires ProcessRuleSet")
    if not isinstance(context, RuleEvaluationContext):
        raise ValueError("evaluation requires RuleEvaluationContext")


def _aggregate(values, aggregation):
    if aggregation is QualityAggregation.COUNT:
        return len(values)
    if aggregation is QualityAggregation.MAXIMUM:
        return max(values, default=0)
    if aggregation is not QualityAggregation.SUM:
        raise ValueError("unsupported metric aggregation")
    if all(type(value) is int for value in values):
        return sum(values)
    return sum_decimals(Decimal(value) for value in values)


def _group_metrics(contributions):
    grouped = defaultdict(list)
    for item in contributions:
        grouped[item.metric_key].append(item.value)
    return grouped


def _raw_metrics(grouped, declarations):
    return {key: _aggregate(values, declarations[key]) for key, values in grouped.items()}


def _reference_round_6(value):
    try:
        projected = float(value)
    except OverflowError as exc:
        raise ValueError("non-finite reference numeric projection") from exc
    if not isfinite(projected):
        raise ValueError("non-finite reference numeric projection")
    return Decimal(str(round(projected, 6)))


def _prohibited_profile(violations):
    count, severity = 0, 0.0
    for item in violations:
        if item.disposition is RuleDisposition.PROHIBITED:
            count += 1
            # Preserve Python 3.10 reference addition order, even on newer runtimes.
            severity += float(item.severity)
    return count, _reference_round_6(severity)


def _underweight_gap_score(values):
    rounded = []
    quantum = Decimal("0.01")
    for value in values:
        value = Decimal(value)
        require_decimal(value, "underweight_total_gap", nonnegative=True)
        # Keep the fractional places and one possible carry into the next integer digit.
        precision = max(1, value.adjusted() - quantum.as_tuple().exponent + 2)
        with localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN)):
            rounded.append(value.quantize(quantum))
    return sum_weights(rounded)


def _quality_key(criteria, metrics, grouped, violations):
    quality = []
    for criterion in criteria:
        key = criterion.metric_key
        values = grouped.get(key, ())
        projection = criterion.numeric_projection
        if projection is NumericProjection.UNDERWEIGHT_GAP_ROUND_2_THEN_SUM:
            value = _underweight_gap_score(values)
        else:
            value = (
                metrics.get(key, 0)
                if criterion.aggregation is QualityAggregation.NAMED_VALUE
                else _aggregate(values, criterion.aggregation)
            )
            if projection is NumericProjection.REFERENCE_FLOAT_ROUND_6:
                # Severity has a reference-specific ordered accumulation, not sum-then-cast.
                value = (
                    _prohibited_profile(violations)[1]
                    if key == "prohibited_violation_severity"
                    and criterion.aggregation
                    in (QualityAggregation.NAMED_VALUE, QualityAggregation.SUM)
                    else _reference_round_6(value)
                )
        if criterion.direction is QualityDirection.MAXIMIZE:
            value = value.copy_negate() if isinstance(value, Decimal) else -value
        quality.append(value)
    return tuple(quality)


def _chain_result(chain, contribution, declarations):
    return ChainEvaluation(
        chain.chain_id,
        ChainSummary(
            chain.assigned_period,
            chain.total_weight,
            chain.real_weight,
            chain.virtual_weight,
            chain.real_node_count,
            chain.virtual_node_count,
            chain.split_piece_count,
        ),
        contribution.violations,
        _raw_metrics(_group_metrics(contribution.metrics), declarations),
    )


def evaluate_chain(
    chain: Chain, rule_set: ProcessRuleSet, context: RuleEvaluationContext
) -> ChainEvaluation:
    _validate_inputs(chain, Chain, rule_set, context)
    contribution = rule_set.evaluate_complete_chain(
        ChainRuleSubject(chain.chain_id, chain), context
    )
    return _chain_result(chain, contribution, rule_set.metric_aggregations())


def quick_chain_prohibited_profile(
    chain: Chain, rule_set: ProcessRuleSet, context: RuleEvaluationContext
) -> tuple[int, Decimal]:
    _validate_inputs(chain, Chain, rule_set, context)
    contribution = rule_set.evaluate_complete_chain(
        ChainRuleSubject(chain.chain_id, chain), context
    )
    return _prohibited_profile(contribution.violations)


def evaluate_plan(
    plan: SchedulePlan, rule_set: ProcessRuleSet, context: RuleEvaluationContext
) -> PlanEvaluation:
    """Evaluate without reuse, including when called by the independent final audit."""
    return _evaluate_plan(plan, rule_set, context)[0]


def _evaluate_plan(plan, rule_set, context, previous_entries=None):
    # None is the uncached path; an empty mapping captures a cold candidate.
    _validate_inputs(plan, SchedulePlan, rule_set, context)
    resources = derive_evaluation_resource_view(plan, context)
    declarations = rule_set.metric_aggregations()
    chain_evaluations, violations, contributions = [], [], []
    entries = None if previous_entries is None else {}
    chain_contributions = [] if entries is not None else None
    for chain in plan.chains:
        entry = None if previous_entries is None else previous_entries.get(chain.chain_id)
        if entry is not None and entry.chain is chain:
            result, evaluated = entry.contribution, entry.evaluation
        else:
            result = rule_set.evaluate_complete_chain(
                ChainRuleSubject(chain.chain_id, chain), context
            )
            evaluated = _chain_result(chain, result, declarations)
        chain_evaluations.append(evaluated)
        if chain_contributions is not None:
            chain_contributions.append(result)
        violations.extend(result.violations)
        contributions.extend(result.metrics)
    # Preserve all-chain, then all-node execution and contribution order.
    for index, chain in enumerate(plan.chains):
        entry = None if previous_entries is None else previous_entries.get(chain.chain_id)
        reused = entry is not None and entry.chain is chain
        node_contributions = [] if entries is not None and not reused else None
        results = (
            entry.node_contributions
            if reused
            else (
                rule_set.evaluate_node(NodeRuleSubject(node.node_id, node), context)
                for node in chain.nodes
            )
        )
        for result in results:
            violations.extend(result.violations)
            contributions.extend(result.metrics)
            if node_contributions is not None:
                node_contributions.append(result)
        if entries is not None:
            entries[chain.chain_id] = entry if reused else _ChainEvaluationEntry(
                chain,
                chain_contributions[index],
                tuple(node_contributions),
                chain_evaluations[index],
            )
    result = rule_set.evaluate_plan(PlanRuleSubject("plan", plan, resources), context)
    violations.extend(result.violations)
    contributions.extend(result.metrics)
    prohibited = tuple(v for v in violations if v.disposition is RuleDisposition.PROHIBITED)
    grouped = _group_metrics(contributions)
    metrics = _raw_metrics(grouped, declarations)
    structural = {
        "prohibited_violation_count": len(prohibited),
        "prohibited_violation_severity": sum_weights(v.severity for v in prohibited),
        "chain_count": len(plan.chains),
        "generated_virtual_weight": resources.generated_virtual_weight,
        "borrowed_future_weight": resources.borrowed_future_weight,
    }
    metrics.update(structural)
    for key, value in structural.items():
        grouped[key] = [value]
    grouped["prohibited_violation_count"] = [1 for _ in prohibited]
    grouped["prohibited_violation_severity"] = [v.severity for v in prohibited]
    grouped["chain_count"] = [1 for _ in plan.chains]
    evaluation = PlanEvaluation(
        tuple(chain_evaluations),
        tuple(violations),
        metrics,
        _quality_key(rule_set.quality_spec, metrics, grouped, violations),
    )
    return evaluation, entries
