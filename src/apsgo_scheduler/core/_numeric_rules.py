"""Compile validated rules once and evaluate their integer decisions.

The configured Rule objects remain the public configuration boundary. Search
code consumes only the compact records and numeric task/plan columns below.
"""

from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache
from hashlib import sha256
from operator import index as integer_index

from ._numeric_state import NumericPlan, NumericPlanOverlay, NumericTask
from ._numeric_units import (
    NumericValueError,
    int64,
    ratio_parts,
    to_ticks,
)
from .contracts import ControlledSplitMode, RuleScope, canonical_json
from .model import MaterialRole
from .rules.concrete import (
    ChainWeightRangeRule,
    ConsecutiveReverseWidthRule,
    ConsecutiveVirtualMaterialRule,
    ContinuousNarrowSteelWeightRule,
    ControlledOrderSplitRule,
    DeliveryDuePerformanceRule,
    EarliestProcessStartRule,
    FutureFillWeightTargetRule,
    HighSurfaceRunCountRule,
    InterChainWidthGapRule,
    LateOriginalPeriodMoveRule,
    ReverseWidthCountRule,
    SameSpecContinuousRealWeightRule,
    SoftHardConnectionRule,
    StrategicCustomerPriorityRule,
    SyntheticNodePriorityRule,
    SyntheticWidthLimitRule,
    TemperatureOverlapRule,
    ThicknessTransitionRule,
    VirtualOutputRatioRule,
    WidthTransitionRule,
)
from .rules.rule_set import ProcessRuleSet


class NumericRuleKind(IntEnum):
    SYNTHETIC_WIDTH = 0
    SOFT_HARD = 1
    TEMPERATURE = 2
    THICKNESS = 3
    WIDTH = 4
    SYNTHETIC_PRIORITY = 5
    STRATEGIC_PRIORITY = 6
    HIGH_SURFACE = 7
    NARROW_WEIGHT = 8
    SAME_SPEC_WEIGHT = 9
    CHAIN_WEIGHT = 10
    CONSECUTIVE_VIRTUAL = 11
    REVERSE_WIDTH_COUNT = 12
    CONSECUTIVE_REVERSE_WIDTH = 13
    LATE_PERIOD = 14
    VIRTUAL_RATIO = 15
    INTER_CHAIN_WIDTH = 16
    FUTURE_FILL = 17
    CONTROLLED_SPLIT = 18
    DELIVERY = 19
    EARLIEST_START = 20


class NumericReason(IntEnum):
    MISSING_WIDTH = 0
    SYNTHETIC_WIDTH_INCREASE = 1
    SOFT_HARD = 2
    TEMPERATURE = 3
    THICKNESS = 4
    WIDTH_TRANSITION = 5
    VIRTUAL_BRIDGE_WIDTH = 6
    HIGH_SURFACE_RUN = 7
    NARROW_WEIGHT_RUN = 8
    SAME_SPEC_WEIGHT_RUN = 9
    CHAIN_UNDERWEIGHT = 10
    CHAIN_OVERWEIGHT = 11
    VIRTUAL_RUN = 12
    REVERSE_WIDTH_COUNT = 13
    CONSECUTIVE_REVERSE_WIDTH = 14
    LATE_PERIOD = 15
    VIRTUAL_RATIO = 16
    EARLY_START = 17


class NumericMetricKind(IntEnum):
    SYNTHETIC_WIDTH_INCREASE = 0
    SYNTHETIC_PRIORITY = 1
    STRATEGIC_PRIORITY = 2
    HIGH_SURFACE_RUN_MAX = 3
    NARROW_WEIGHT_RUN_MAX = 4
    SAME_SPEC_WEIGHT_RUN_MAX = 5
    UNDERWEIGHT_CHAIN_COUNT = 6
    UNDERWEIGHT_TOTAL_GAP = 7
    OVERWEIGHT_CHAIN_COUNT = 8
    OVERWEIGHT_TOTAL_EXCESS = 9
    CHAIN_TARGET_WEIGHT_DEVIATION = 10
    CONSECUTIVE_VIRTUAL_MAX = 11
    REVERSE_WIDTH_COUNT = 12
    CONSECUTIVE_REVERSE_WIDTH_COUNT = 13
    LATE_PERIOD_COUNT = 14
    VIRTUAL_RATIO = 15
    INTER_CHAIN_WIDTH_GAP = 16
    FUTURE_FILL_TOTAL_GAP = 17
    NEWLY_LATE_ORIGINAL_WEIGHT = 18
    OLD_BACKLOG_LAST_COMPLETION_SECONDS = 19
    DELIVERY_WAIT_BURDEN_WEIGHT_SECONDS = 20


class NumericSplitReason(IntEnum):
    DISABLED = 0
    UNSUPPORTED_MATERIAL_ROLE = 1
    ALREADY_SPLIT = 2
    SOURCE_ALREADY_LATE = 3
    SOURCE_LIMIT = 4
    MODE_NOT_ALLOWED = 5
    NOT_NARROW_REAL = 6
    SOURCE_WITHIN_LIMIT = 7
    SAME_PERIOD_SPLIT = 8
    FUTURE_BORROW_RETURN = 9


class NumericMissingGradePolicy(IntEnum):
    DENY = 0
    ALLOW = 1
    PASS = 2
    IGNORE = 3
    FALLBACK_SAME_HOT_ROLL_GRADE = 4


@dataclass(frozen=True, slots=True)
class NumericThicknessBand:
    minimum: int
    maximum: int
    has_minimum: bool
    has_maximum: bool
    include_minimum: bool
    include_maximum: bool
    relative: bool
    tolerance_numerator: int
    tolerance_denominator: int

    def __post_init__(self):
        for name in ('minimum', 'maximum', 'tolerance_numerator', 'tolerance_denominator'):
            int64(getattr(self, name), name)
        if self.tolerance_denominator <= 0:
            raise NumericValueError('thickness_band', 'positive tolerance denominator required')


@dataclass(frozen=True, slots=True)
class CompiledNumericRule:
    index: int
    rule_id: str
    kind: NumericRuleKind
    scope: RuleScope
    values: tuple[int, ...] = ()
    flags: tuple[bool, ...] = ()
    bands: tuple[NumericThicknessBand, ...] = ()

    def __post_init__(self):
        int64(self.index, 'rule.index')
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise NumericValueError('rule.rule_id', 'nonempty identity required')
        if not isinstance(self.kind, NumericRuleKind) or not isinstance(self.scope, RuleScope):
            raise NumericValueError(self.rule_id, 'known rule kind and scope required')
        if not isinstance(self.values, tuple) or not isinstance(self.flags, tuple) or not isinstance(self.bands, tuple):
            raise NumericValueError(self.rule_id, 'immutable compiled parameter tuples required')
        for value in self.values:
            int64(value, self.rule_id)
        if any(type(value) is not bool for value in self.flags):
            raise NumericValueError(self.rule_id, 'boolean flags required')
        if any(not isinstance(band, NumericThicknessBand) for band in self.bands):
            raise NumericValueError(self.rule_id, 'known thickness bands required')


@dataclass(frozen=True, slots=True)
class NumericViolation:
    rule_index: int
    reason: NumericReason
    chain_index: int
    start_position: int
    end_position: int
    severity: int
    prohibited: bool = True

    def __post_init__(self):
        for name in ('rule_index', 'chain_index', 'start_position', 'end_position', 'severity'):
            int64(getattr(self, name), name)
        if self.severity < 0 or type(self.prohibited) is not bool:
            raise NumericValueError(
                'violation', 'nonnegative severity and boolean disposition required'
            )
        if not isinstance(self.reason, NumericReason):
            raise NumericValueError('violation.reason', 'known numeric reason required')


@dataclass(frozen=True, slots=True)
class NumericMetric:
    rule_index: int
    kind: NumericMetricKind
    numerator: int
    denominator: int = 1

    def __post_init__(self):
        int64(self.rule_index, 'metric.rule_index')
        int64(self.numerator, 'metric.numerator')
        int64(self.denominator, 'metric.denominator')
        if self.denominator <= 0:
            raise NumericValueError('metric.denominator', 'positive denominator required')
        if not isinstance(self.kind, NumericMetricKind):
            raise NumericValueError('metric.kind', 'known numeric metric required')


@dataclass(frozen=True, slots=True)
class NumericRuleResult:
    violations: tuple[NumericViolation, ...] = ()
    metrics: tuple[NumericMetric, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.violations, tuple)
                or any(not isinstance(value, NumericViolation) for value in self.violations)
                or not isinstance(self.metrics, tuple)
                or any(not isinstance(value, NumericMetric) for value in self.metrics)):
            raise NumericValueError('rule_result', 'immutable numeric violations and metrics required')


@dataclass(frozen=True, slots=True)
class NumericSplitDecision:
    eligible: bool
    rule_index: int
    reason: NumericSplitReason
    mode: int = -1
    target_period: int = -1
    maximum_piece_weight: int = 0
    minimum_piece_weight: int = 0
    maximum_accepted_source_count: int = 0
    maximum_separator_node_count: int = 0
    maximum_separator_weight: int = 0

    def __post_init__(self):
        if type(self.eligible) is not bool or not isinstance(self.reason, NumericSplitReason):
            raise NumericValueError('split_decision', 'boolean eligibility and known reason required')
        for name in ('rule_index', 'mode', 'target_period', 'maximum_piece_weight',
                     'minimum_piece_weight', 'maximum_accepted_source_count',
                     'maximum_separator_node_count', 'maximum_separator_weight'):
            int64(getattr(self, name), name)
        if self.eligible:
            if (self.rule_index < 0 or self.mode < 0 or self.target_period < 0
                    or self.minimum_piece_weight <= 0
                    or self.maximum_piece_weight < self.minimum_piece_weight
                    or min(self.maximum_accepted_source_count,
                           self.maximum_separator_node_count,
                           self.maximum_separator_weight) < 0):
                raise NumericValueError('split_decision', 'invalid authorized split limits')
        elif (self.mode != -1 or self.target_period != -1
              or any((self.maximum_piece_weight, self.minimum_piece_weight,
                      self.maximum_accepted_source_count, self.maximum_separator_node_count,
                      self.maximum_separator_weight))):
            raise NumericValueError('split_decision', 'rejected split cannot grant authority')


_RULE_KINDS = {
    SyntheticWidthLimitRule: NumericRuleKind.SYNTHETIC_WIDTH,
    SoftHardConnectionRule: NumericRuleKind.SOFT_HARD,
    TemperatureOverlapRule: NumericRuleKind.TEMPERATURE,
    ThicknessTransitionRule: NumericRuleKind.THICKNESS,
    WidthTransitionRule: NumericRuleKind.WIDTH,
    SyntheticNodePriorityRule: NumericRuleKind.SYNTHETIC_PRIORITY,
    StrategicCustomerPriorityRule: NumericRuleKind.STRATEGIC_PRIORITY,
    HighSurfaceRunCountRule: NumericRuleKind.HIGH_SURFACE,
    ContinuousNarrowSteelWeightRule: NumericRuleKind.NARROW_WEIGHT,
    SameSpecContinuousRealWeightRule: NumericRuleKind.SAME_SPEC_WEIGHT,
    ChainWeightRangeRule: NumericRuleKind.CHAIN_WEIGHT,
    ConsecutiveVirtualMaterialRule: NumericRuleKind.CONSECUTIVE_VIRTUAL,
    ReverseWidthCountRule: NumericRuleKind.REVERSE_WIDTH_COUNT,
    ConsecutiveReverseWidthRule: NumericRuleKind.CONSECUTIVE_REVERSE_WIDTH,
    LateOriginalPeriodMoveRule: NumericRuleKind.LATE_PERIOD,
    VirtualOutputRatioRule: NumericRuleKind.VIRTUAL_RATIO,
    InterChainWidthGapRule: NumericRuleKind.INTER_CHAIN_WIDTH,
    FutureFillWeightTargetRule: NumericRuleKind.FUTURE_FILL,
    ControlledOrderSplitRule: NumericRuleKind.CONTROLLED_SPLIT,
    DeliveryDuePerformanceRule: NumericRuleKind.DELIVERY,
    EarliestProcessStartRule: NumericRuleKind.EARLIEST_START,
}

_TEXT_FIELDS = ('grade', 'hot_roll_grade', 'soft_hard_class', 'grade_class', 'surface_grade')
_SAME_SPEC_DERIVED_GROUP = len(('priority', 'narrow', 'surface'))
_NORMAL_REAL = tuple(MaterialRole).index(MaterialRole.NORMAL_REAL)
_ACTUAL_TRANSITION = tuple(MaterialRole).index(MaterialRole.ACTUAL_TRANSITION)
_GENERATED_VIRTUAL = tuple(MaterialRole).index(MaterialRole.GENERATED_VIRTUAL)


def _derived_index(task, group, rule_id):
    try:
        return task.derived_rule_ids[group].index(rule_id)
    except ValueError as error:
        raise NumericValueError(rule_id, 'required derived numeric column is missing') from error


def _text_code(task, field, text):
    labels = task.text_labels[_TEXT_FIELDS.index(field)]
    normalized = text.strip().upper() if field in ('grade', 'hot_roll_grade', 'surface_grade') else text.strip()
    try:
        return labels.index(normalized)
    except ValueError:
        return -1


def _thickness_band(task, rule, index, band):
    def bound(name):
        value = band[name]
        return 0 if value is None else to_ticks(value, task.units.thickness, f'{rule.rule_id}.ranges[{index}].{name}')

    relative = band['calculation_mode'].strip().lower() == 'relative'
    numerator, denominator = (
        ratio_parts(band['tolerance'], f'{rule.rule_id}.ranges[{index}].tolerance')
        if relative else
        (to_ticks(band['tolerance'], task.units.thickness,
                  f'{rule.rule_id}.ranges[{index}].tolerance'), 1)
    )
    return NumericThicknessBand(
        bound('min'), bound('max'), band['min'] is not None, band['max'] is not None,
        band['include_min'], band['include_max'], relative, numerator, denominator,
    )


def _compile_rule(task, index, rule):
    kind = _RULE_KINDS.get(type(rule))
    if kind is None:
        raise NumericValueError(rule.rule_id, f'unsupported enabled rule type {type(rule).__name__}')
    p, values, flags, bands = rule.parameters, (), (), ()
    if kind is NumericRuleKind.SYNTHETIC_WIDTH:
        values = (to_ticks(p['maximum_increase'], task.units.width, rule.rule_id),)
    elif kind is NumericRuleKind.SOFT_HARD:
        policies = {item.name.lower(): int(item) for item in NumericMissingGradePolicy}
        policies['fallback_same_hot_roll_grade'] = int(NumericMissingGradePolicy.FALLBACK_SAME_HOT_ROLL_GRADE)
        values = (policies[p['missing_grade_policy'].strip().lower()],
                  _text_code(task, 'soft_hard_class', ''), _text_code(task, 'hot_roll_grade', ''))
        flags = (p['virtual_sphc_allows_bridge'], p['transition_material_breaks_soft_hard'])
    elif kind is NumericRuleKind.TEMPERATURE:
        values = (to_ticks(p['min_overlap'], task.units.temperature, rule.rule_id),)
        flags = (p['ignore_temperature'], p['virtual_temperature_adaptive'])
    elif kind is NumericRuleKind.THICKNESS:
        fallback = to_ticks(p['fallback_tolerance'], task.units.thickness, rule.rule_id)
        values = (int(p['basis'].strip().lower() == 'thicker'), fallback, 1)
        bands = tuple(_thickness_band(task, rule, i, band) for i, band in enumerate(p['ranges']))
        flags = (any(band.relative for band in bands),)
    elif kind is NumericRuleKind.WIDTH:
        values = (to_ticks(p['max_reverse_width'], task.units.width, rule.rule_id),
                  to_ticks(p['virtual_width_tolerance'], task.units.width, rule.rule_id))
    elif kind in (NumericRuleKind.SYNTHETIC_PRIORITY, NumericRuleKind.STRATEGIC_PRIORITY):
        values = (_derived_index(task, 0, rule.rule_id),)
    elif kind is NumericRuleKind.HIGH_SURFACE:
        values = (_derived_index(task, 2, rule.rule_id), int64(p['max_run_count'], rule.rule_id))
    elif kind is NumericRuleKind.NARROW_WEIGHT:
        values = (_derived_index(task, 1, rule.rule_id),
                  to_ticks(p['max_real_weight'], task.units.weight, rule.rule_id))
    elif kind is NumericRuleKind.SAME_SPEC_WEIGHT:
        values = (_derived_index(task, _SAME_SPEC_DERIVED_GROUP, rule.rule_id),
                  to_ticks(p['max_real_weight'], task.units.weight, rule.rule_id))
    elif kind is NumericRuleKind.CHAIN_WEIGHT:
        values = tuple(to_ticks(p[name], task.units.weight, rule.rule_id)
                       for name in ('min_weight', 'max_weight', 'target_weight'))
    elif kind in (NumericRuleKind.CONSECUTIVE_VIRTUAL,
                  NumericRuleKind.REVERSE_WIDTH_COUNT):
        values = (int64(p['max_count'], rule.rule_id),)
    elif kind is NumericRuleKind.VIRTUAL_RATIO:
        values = ratio_parts(p['max_ratio'], rule.rule_id)
    elif kind is NumericRuleKind.FUTURE_FILL:
        values = (to_ticks(p['future_fill_weight_target'], task.units.weight, rule.rule_id),)
    elif kind is NumericRuleKind.CONTROLLED_SPLIT:
        mode_bits = 0
        for mode in p['allowed_modes']:
            mode_bits |= 1 << list(ControlledSplitMode).index(ControlledSplitMode(mode))
        values = (
            _text_code(task, 'grade_class', p['grade_class']),
            to_ticks(p['width_upper_exclusive'], task.units.width, rule.rule_id),
            to_ticks(p['maximum_piece_weight'], task.units.weight, rule.rule_id),
            to_ticks(p['minimum_piece_weight'], task.units.weight, rule.rule_id),
            int64(p['maximum_accepted_source_count'], rule.rule_id),
            int64(p['maximum_separator_node_count'], rule.rule_id),
            to_ticks(p['maximum_separator_weight'], task.units.weight, rule.rule_id),
            mode_bits,
        )
    elif kind is NumericRuleKind.DELIVERY:
        if not rule.second_precision:
            raise NumericValueError(rule.rule_id, 'only second-precision delivery scoring is supported')
        flags = (rule.include_backlog_clearance, rule.second_precision)
    elif kind is NumericRuleKind.EARLIEST_START:
        if task.start_ms is None or not task.originals.has_earliest_start.all():
            raise NumericValueError(rule.rule_id, 'earliest start requires complete original timing')
    return CompiledNumericRule(index, rule.rule_id, kind, rule.scope, values, flags, bands)


@dataclass(frozen=True, slots=True)
class NumericRuleProgram:
    task_fingerprint: str
    rule_set_fingerprint: str
    rules: tuple[CompiledNumericRule, ...]
    fingerprint: str

    def __post_init__(self):
        if (not isinstance(self.task_fingerprint, str) or not self.task_fingerprint
                or not isinstance(self.rule_set_fingerprint, str) or not self.rule_set_fingerprint
                or not isinstance(self.fingerprint, str) or not self.fingerprint):
            raise NumericValueError('rules', 'nonempty task, rule-set and program identities required')
        if (not isinstance(self.rules, tuple)
                or any(not isinstance(rule, CompiledNumericRule) for rule in self.rules)
                or tuple(rule.index for rule in self.rules) != tuple(range(len(self.rules)))):
            raise NumericValueError('rules', 'ordered compiled rules required')

    @classmethod
    def compile(cls, task, rule_set):
        if not isinstance(task, NumericTask) or not isinstance(rule_set, ProcessRuleSet):
            raise NumericValueError('rules', 'numeric task and validated rule set required')
        if task.rule_set_fingerprint != rule_set.fingerprint:
            raise NumericValueError('rules', 'rule set does not match numeric task preparation')
        rules = tuple(_compile_rule(task, index, rule) for index, rule in enumerate(rule_set.rules))
        if len(rules) != len(rule_set.rules):
            raise NumericValueError('rules', 'an enabled rule was skipped')
        identity = _program_fingerprint(task.fingerprint, rule_set.fingerprint, rules)
        return cls(task.fingerprint, rule_set.fingerprint, rules, identity)

    def for_kind(self, kind):
        if not isinstance(kind, NumericRuleKind):
            raise NumericValueError('rule_kind', 'known numeric rule kind required')
        return tuple(rule for rule in self.rules if rule.kind is kind)

    def rebind(self, task):
        """Bind unchanged compiled parameters to an accepted expanded task catalog."""
        if not isinstance(task, NumericTask) or task.rule_set_fingerprint != self.rule_set_fingerprint:
            raise NumericValueError('rules', 'expanded task does not match compiled rules')
        identity = _program_fingerprint(
            task.fingerprint, self.rule_set_fingerprint, self.rules
        )
        return NumericRuleProgram(
            task.fingerprint, self.rule_set_fingerprint, self.rules, identity
        )


_TASK_MARKER = "\0numeric-task-fingerprint\0"


@lru_cache(maxsize=128)
def _program_fingerprint_template(rule_set_fingerprint, rules):
    encoded = canonical_json(
        {
            "compiler": 1,
            "task": _TASK_MARKER,
            "rule_set": rule_set_fingerprint,
            "rules": tuple(
                (
                    rule.index,
                    rule.rule_id,
                    int(rule.kind),
                    rule.scope.value,
                    rule.values,
                    rule.flags,
                    rule.bands,
                )
                for rule in rules
            ),
        }
    )
    prefix, marker, suffix = encoded.rpartition(canonical_json(_TASK_MARKER))
    if not marker:
        raise NumericValueError("rules", "task identity marker is missing")
    return prefix, suffix


def _program_fingerprint(task_fingerprint, rule_set_fingerprint, rules):
    prefix, suffix = _program_fingerprint_template(rule_set_fingerprint, rules)
    encoded = prefix + canonical_json(task_fingerprint) + suffix
    return sha256(encoded.encode("utf-8")).hexdigest()


def _native_rule_result(output):
    from ._numeric_evaluation import _check_kernel_status
    _check_kernel_status(output)
    violations = tuple(NumericViolation(
        int(v[0]), NumericReason(int(v[1])), int(v[2]), int(v[3]), int(v[4]),
        int(v[5]), bool(v[6]),
    ) for v in output.violations[:int(output.counts[0])])
    metrics = tuple(NumericMetric(
        int(m[0]), NumericMetricKind(int(m[1])), int(m[3]), int(m[4]),
    ) for m in output.metrics[:int(output.counts[1])])
    return NumericRuleResult(violations, metrics)


def _validate_edge(task, program, left_row, right_row):
    if program.task_fingerprint != task.fingerprint:
        raise NumericValueError('rules', 'program belongs to another numeric task')
    for value, name in ((left_row, 'left_row'), (right_row, 'right_row')):
        if type(value) is not int or not 0 <= value < task.nodes.weight.size:
            raise NumericValueError(name, 'row is outside numeric task')


def evaluate_numeric_edge(task, program, left_row, right_row, *, chain_index=-1, position=-1):
    from ._numeric_kernel import task_columns, rule_tables, edge_kernel
    _validate_edge(task, program, left_row, right_row)
    return _native_rule_result(edge_kernel(task_columns(task), rule_tables(program.rules),
                                          left_row, right_row, chain_index, position, True))


def numeric_edge_allowed(task, program, left_row, right_row):
    from ._numeric_kernel import task_columns, rule_tables, edges_allowed_kernel, OK
    _validate_edge(task, program, left_row, right_row)
    allowed, status = edges_allowed_kernel(task_columns(task), rule_tables(program.rules),
                                           left_row, right_row)
    if status[0] != OK:
        raise NumericValueError(f"rules[{int(status[1])}]", "integer is outside signed int64")
    return bool(allowed)


def _validated_rows(task, program, rows, chain_index):
    if not isinstance(task, NumericTask) or not isinstance(program, NumericRuleProgram):
        raise NumericValueError('chain', 'numeric task and rule program required')
    if program.task_fingerprint != task.fingerprint:
        raise NumericValueError('chain', 'program belongs to another numeric task')
    if type(chain_index) is not int or chain_index < 0:
        raise NumericValueError('chain_index', 'nonnegative chain index required')
    supplied = tuple(rows)
    try:
        values = tuple(integer_index(row) for row in supplied)
    except TypeError as error:
        raise NumericValueError('chain_rows', 'nonempty unique task rows required') from error
    if (not values or any(type(row) is bool or not 0 <= value < task.nodes.weight.size
                          for row, value in zip(supplied, values)) or len(set(values)) != len(values)):
        raise NumericValueError('chain_rows', 'nonempty unique task rows required')
    return values


def numeric_rows_prohibited_profile(task, program, rows):
    from ._numeric_kernel import task_columns, rule_tables, pack_rows, evaluate_chain_kernel
    from ._numeric_evaluation import _check_kernel_status
    rows = _validated_rows(task, program, rows, 0)
    result = evaluate_chain_kernel(task_columns(task), rule_tables(program.rules), pack_rows(rows))
    _check_kernel_status(result)
    return int(result.scores[0, 0]), int(result.scores[0, 1])


def evaluate_numeric_rows(task, program, rows, *, chain_index=0):
    from ._numeric_kernel import (
        task_columns, rule_tables, pack_rows, evaluate_chain_kernel, CAPACITY,
    )
    from ._numeric_evaluation import _check_kernel_status
    rows = pack_rows(_validated_rows(task, program, rows, chain_index))
    vc, mc = 16, 32
    while True:
        output = evaluate_chain_kernel(task_columns(task), rule_tables(program.rules),
                                       rows, chain_index, True, vc, mc)
        _check_kernel_status(output)
        if output.status[0] != CAPACITY:
            return _native_rule_result(output)
        vc, mc = map(int, output.counts[:2])


def _numeric_chain_rows(plan, chain_index):
    if isinstance(plan, NumericPlanOverlay):
        return plan.chains[chain_index]
    start, stop = int(plan.chain_offsets[chain_index]), int(plan.chain_offsets[chain_index + 1])
    return plan.node_rows[start:stop]


def evaluate_numeric_chain(task, program, plan, chain_index):
    if (not isinstance(plan, (NumericPlan, NumericPlanOverlay)) or program.task_fingerprint != task.fingerprint
            or plan.task_fingerprint != task.fingerprint):
        raise NumericValueError('chain', 'matching numeric task, program and plan required')
    if type(chain_index) is not int or not 0 <= chain_index < plan.chain_ids.size:
        raise NumericValueError('chain_index', 'chain is outside numeric plan')
    return evaluate_numeric_rows(task, program, _numeric_chain_rows(plan, chain_index),
                                 chain_index=chain_index)


def evaluate_numeric_static_plan_rules(task, program, plan, *, chain_total_weights=None,
                                       real_weight=None, virtual_weight=None):
    from ._numeric_kernel import task_columns, rule_tables, static_plan_kernel
    from ._numeric_evaluation import _kernel_inputs
    if (not isinstance(plan, (NumericPlan, NumericPlanOverlay)) or program.task_fingerprint != task.fingerprint
            or plan.task_fingerprint != task.fingerprint):
        raise NumericValueError('plan', 'matching numeric task, program and plan required')
    if any(int(plan.chain_periods[i]) < int(plan.chain_periods[i-1])
           for i in range(1, plan.chain_periods.size)):
        raise NumericValueError('chain_periods', 'production chains must follow task period order')
    supplied = tuple(v is not None for v in (chain_total_weights, real_weight, virtual_weight))
    if any(supplied) and not all(supplied):
        raise NumericValueError('plan_facts', 'complete chain and resource facts required')
    rows, offsets = _kernel_inputs(plan)
    output = static_plan_kernel(task_columns(task), rule_tables(program.rules), rows, offsets,
                                plan.chain_periods)
    if all(supplied):
        try:
            weights = tuple(int64(integer_index(v), 'chain_total_weight') for v in chain_total_weights)
            real = int64(integer_index(real_weight), 'scheduled_real_weight')
            virtual = int64(integer_index(virtual_weight), 'generated_virtual_weight')
        except TypeError as error:
            raise NumericValueError('plan_facts', 'integer chain and resource facts required') from error
        if (weights != tuple(map(int, output.facts[:, 0])) or real != int(output.totals[0])
                or virtual != int(output.totals[1])):
            raise NumericValueError('plan_facts', 'chain and resource facts do not match')
    return _native_rule_result(output)


def evaluate_numeric_split(task, program, parent_row, origin_period, accepted_source_count):
    rules = program.for_kind(NumericRuleKind.CONTROLLED_SPLIT)
    if len(rules) != 1:
        return NumericSplitDecision(False, -1, NumericSplitReason.DISABLED)
    rule, nodes = rules[0], task.nodes
    (grade_class, width_limit, maximum_piece_weight, minimum_piece_weight,
     maximum_source_count, maximum_separator_count, maximum_separator_weight,
     mode_bits) = rule.values
    if any(type(value) is not int for value in (parent_row, origin_period, accepted_source_count)):
        raise NumericValueError(rule.rule_id, 'integer split inputs required')
    if not 0 <= parent_row < task.nodes.weight.size or not 0 <= origin_period < len(task.period_ids):
        raise NumericValueError(rule.rule_id, 'split row or period is outside the task')
    def reject(reason): return NumericSplitDecision(False, rule.index, reason)
    if int(nodes.role[parent_row]) != _NORMAL_REAL:
        return reject(NumericSplitReason.UNSUPPORTED_MATERIAL_ROLE)
    if int(nodes.split_group[parent_row]) >= 0:
        return reject(NumericSplitReason.ALREADY_SPLIT)
    source_period = int(nodes.source_period[parent_row])
    if source_period < origin_period:
        return reject(NumericSplitReason.SOURCE_ALREADY_LATE)
    if accepted_source_count >= maximum_source_count:
        return reject(NumericSplitReason.SOURCE_LIMIT)
    mode = (ControlledSplitMode.SAME_PERIOD_SPLIT if source_period == origin_period
            else ControlledSplitMode.FUTURE_BORROW_RETURN)
    mode_index = list(ControlledSplitMode).index(mode)
    if not mode_bits & (1 << mode_index):
        return reject(NumericSplitReason.MODE_NOT_ALLOWED)
    if (int(nodes.grade_class[parent_row]) != grade_class or not nodes.present[parent_row, 0]
            or int(nodes.width[parent_row]) >= width_limit):
        return reject(NumericSplitReason.NOT_NARROW_REAL)
    if int(nodes.weight[parent_row]) <= maximum_piece_weight:
        return reject(NumericSplitReason.SOURCE_WITHIN_LIMIT)
    reason = (NumericSplitReason.SAME_PERIOD_SPLIT if mode is ControlledSplitMode.SAME_PERIOD_SPLIT
              else NumericSplitReason.FUTURE_BORROW_RETURN)
    return NumericSplitDecision(True, rule.index, reason, mode_index, source_period,
                                maximum_piece_weight, minimum_piece_weight,
                                maximum_source_count, maximum_separator_count,
                                maximum_separator_weight)
