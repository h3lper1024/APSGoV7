"""Frozen a03fdf5 integer evaluator, TEST REFERENCE ONLY. Never import from production."""
from operator import index as integer_index
from apsgo_scheduler.core._numeric_state import NumericPlan, NumericPlanOverlay, NumericTask
from apsgo_scheduler.core._numeric_units import SEVERITY_SCALE, NumericValueError, checked_product, checked_sum, int64, round_half_up_ratio
from apsgo_scheduler.core.contracts import RuleScope, ControlledSplitMode
from apsgo_scheduler.core._numeric_rules import (NumericRuleProgram, NumericRuleKind, NumericReason, NumericMetricKind, NumericMissingGradePolicy, NumericViolation, NumericMetric, NumericRuleResult, NumericSplitDecision, NumericSplitReason, _GENERATED_VIRTUAL, _ACTUAL_TRANSITION, _NORMAL_REAL)

def _severity_ratio(numerator, denominator, path, *, at_least_one=True):
    if numerator <= 0 or denominator <= 0:
        raise NumericValueError(path, 'positive severity ratio required')
    ticks = round_half_up_ratio(checked_product(numerator, SEVERITY_SCALE, path), denominator, path)
    return max(SEVERITY_SCALE, ticks) if at_least_one else ticks


def _physical_severity(excess, scale, path):
    return round_half_up_ratio(checked_product(excess, SEVERITY_SCALE, path), scale, path)


def _violation(rule, reason, chain, start, end, severity, *, prohibited=True):
    return NumericViolation(
        rule.index, reason, chain, start, end, int64(severity, rule.rule_id), prohibited
    )


def _edge_rule_allowed(task, rule, left, right):
    nodes = task.nodes
    if rule.kind is NumericRuleKind.SYNTHETIC_WIDTH:
        return bool(
            nodes.present[left, 0]
            and nodes.present[right, 0]
            and max(0, int(nodes.width[right]) - int(nodes.width[left])) <= rule.values[0]
        )
    if rule.kind is NumericRuleKind.SOFT_HARD:
        roles = (int(nodes.role[left]), int(nodes.role[right]))
        if _GENERATED_VIRTUAL in roles:
            return rule.flags[0]
        if _ACTUAL_TRANSITION in roles:
            return rule.flags[1]
        left_class, right_class = int(nodes.soft_hard_class[left]), int(
            nodes.soft_hard_class[right]
        )
        empty_class, empty_hot = rule.values[1:]
        if left_class != empty_class and right_class != empty_class:
            return left_class == right_class
        if rule.values[0] == NumericMissingGradePolicy.FALLBACK_SAME_HOT_ROLL_GRADE:
            left_hot, right_hot = int(nodes.hot_roll_grade[left]), int(
                nodes.hot_roll_grade[right]
            )
            return left_hot != empty_hot and right_hot != empty_hot and left_hot == right_hot
        return rule.values[0] in (
            NumericMissingGradePolicy.ALLOW,
            NumericMissingGradePolicy.PASS,
            NumericMissingGradePolicy.IGNORE,
        )
    if rule.kind is NumericRuleKind.TEMPERATURE:
        if rule.flags[0] or (
            rule.flags[1]
            and _GENERATED_VIRTUAL in (int(nodes.role[left]), int(nodes.role[right]))
        ):
            return True
        if not nodes.present[left, 2:].all() or not nodes.present[right, 2:].all():
            return True
        overlap = min(int(nodes.max_temperature[left]), int(nodes.max_temperature[right])) - max(
            int(nodes.min_temperature[left]), int(nodes.min_temperature[right])
        )
        return overlap >= rule.values[0]
    if rule.kind is NumericRuleKind.THICKNESS:
        if not nodes.present[left, 1] or not nodes.present[right, 1]:
            return True
        a, b = int(nodes.thickness[left]), int(nodes.thickness[right])
        basis = max(a, b) if rule.values[0] else min(a, b)
        numerator, denominator = rule.values[1], rule.values[2]
        for band in rule.bands:
            lower = not band.has_minimum or (
                basis >= band.minimum if band.include_minimum else basis > band.minimum
            )
            upper = not band.has_maximum or (
                basis <= band.maximum if band.include_maximum else basis < band.maximum
            )
            if lower and upper:
                numerator, denominator = (
                    (
                        checked_product(basis, band.tolerance_numerator, rule.rule_id),
                        band.tolerance_denominator,
                    )
                    if band.relative
                    else (band.tolerance_numerator, 1)
                )
                break
        return checked_product(abs(a - b), denominator, rule.rule_id) <= numerator
    if rule.kind is NumericRuleKind.WIDTH:
        if not nodes.present[left, 0] or not nodes.present[right, 0]:
            return False
        virtual = _GENERATED_VIRTUAL in (int(nodes.role[left]), int(nodes.role[right]))
        delta = (
            abs(int(nodes.width[right]) - int(nodes.width[left]))
            if virtual
            else int(nodes.width[right]) - int(nodes.width[left])
        )
        return delta <= rule.values[1 if virtual else 0]
    return True


def _edge_rule(task, rule, left, right, chain, position):
    nodes = task.nodes
    if rule.kind is NumericRuleKind.SYNTHETIC_WIDTH:
        if not nodes.present[left, 0] or not nodes.present[right, 0]:
            return NumericRuleResult((_violation(rule, NumericReason.MISSING_WIDTH, chain, position, position + 1, SEVERITY_SCALE),))
        increase = max(0, int(nodes.width[right]) - int(nodes.width[left]))
        excess = increase - rule.values[0]
        violations = () if _edge_rule_allowed(task, rule, left, right) else (_violation(
            rule, NumericReason.SYNTHETIC_WIDTH_INCREASE, chain, position, position + 1,
            _physical_severity(excess, task.units.width, rule.rule_id)),)
        return NumericRuleResult(violations, (NumericMetric(rule.index, NumericMetricKind.SYNTHETIC_WIDTH_INCREASE, increase),))
    if rule.kind is NumericRuleKind.SOFT_HARD:
        allowed = _edge_rule_allowed(task, rule, left, right)
        return NumericRuleResult(() if allowed else (_violation(
            rule, NumericReason.SOFT_HARD, chain, position, position + 1, SEVERITY_SCALE),))
    if rule.kind is NumericRuleKind.TEMPERATURE:
        if _edge_rule_allowed(task, rule, left, right):
            return NumericRuleResult()
        overlap = min(int(nodes.max_temperature[left]), int(nodes.max_temperature[right])) - max(
            int(nodes.min_temperature[left]), int(nodes.min_temperature[right]))
        minimum = rule.values[0]
        if overlap >= minimum:
            return NumericRuleResult()
        severity = _severity_ratio(minimum - overlap, max(minimum, task.units.temperature), rule.rule_id)
        return NumericRuleResult((_violation(rule, NumericReason.TEMPERATURE, chain, position, position + 1, severity),))
    if rule.kind is NumericRuleKind.THICKNESS:
        if _edge_rule_allowed(task, rule, left, right):
            return NumericRuleResult()
        a, b = int(nodes.thickness[left]), int(nodes.thickness[right])
        basis = max(a, b) if rule.values[0] else min(a, b)
        tolerance_numerator, tolerance_denominator = rule.values[1], rule.values[2]
        for band in rule.bands:
            lower = not band.has_minimum or (
                basis >= band.minimum if band.include_minimum else basis > band.minimum
            )
            upper = not band.has_maximum or (
                basis <= band.maximum if band.include_maximum else basis < band.maximum
            )
            if lower and upper:
                tolerance_numerator, tolerance_denominator = (
                    (checked_product(basis, band.tolerance_numerator, rule.rule_id),
                     band.tolerance_denominator)
                    if band.relative else
                    (band.tolerance_numerator, 1)
                )
                break
        difference = abs(a - b)
        scaled_difference = checked_product(difference, tolerance_denominator, rule.rule_id)
        excess = int64(scaled_difference - tolerance_numerator, rule.rule_id)
        severity = (SEVERITY_SCALE if tolerance_numerator == 0 else
                    _severity_ratio(excess, tolerance_numerator, rule.rule_id))
        return NumericRuleResult((_violation(rule, NumericReason.THICKNESS, chain, position, position + 1, severity),))
    if rule.kind is NumericRuleKind.WIDTH:
        if not nodes.present[left, 0] or not nodes.present[right, 0]:
            return NumericRuleResult((_violation(rule, NumericReason.MISSING_WIDTH, chain, position, position + 1, SEVERITY_SCALE),))
        virtual = _GENERATED_VIRTUAL in (int(nodes.role[left]), int(nodes.role[right]))
        delta = (abs(int(nodes.width[right]) - int(nodes.width[left])) if virtual
                 else int(nodes.width[right]) - int(nodes.width[left]))
        limit = rule.values[1 if virtual else 0]
        if _edge_rule_allowed(task, rule, left, right):
            return NumericRuleResult()
        severity = _severity_ratio(delta - limit, max(limit, task.units.width), rule.rule_id)
        return NumericRuleResult((_violation(rule, NumericReason.WIDTH_TRANSITION, chain, position, position + 1, severity),))
    return NumericRuleResult()


def evaluate_numeric_edge(task, program, left_row, right_row, *, chain_index=-1, position=-1):
    if program.task_fingerprint != task.fingerprint:
        raise NumericValueError('rules', 'program belongs to another numeric task')
    size = task.nodes.weight.size
    for value, name in ((left_row, 'left_row'), (right_row, 'right_row')):
        if type(value) is not int or not 0 <= value < size:
            raise NumericValueError(name, 'row is outside numeric task')
    results = tuple(_edge_rule(task, rule, left_row, right_row, chain_index, position)
                    for rule in program.rules if rule.scope is RuleScope.EDGE)
    return NumericRuleResult(
        tuple(v for result in results for v in result.violations),
        tuple(m for result in results for m in result.metrics),
    )


def numeric_edge_allowed(task, program, left_row, right_row):
    if program.task_fingerprint != task.fingerprint:
        raise NumericValueError("rules", "program belongs to another numeric task")
    size = task.nodes.weight.size
    for value, name in ((left_row, "left_row"), (right_row, "right_row")):
        if type(value) is not int or not 0 <= value < size:
            raise NumericValueError(name, "row is outside numeric task")
    return all(
        _edge_rule_allowed(task, rule, left_row, right_row)
        for rule in program.rules
        if rule.scope is RuleScope.EDGE
    )


def _runs(mask, rows):
    start = None
    for position, row in enumerate(rows):
        matched = bool(mask[int(row)])
        if matched and start is None:
            start = position
        elif not matched and start is not None:
            yield start, position
            start = None
    if start is not None:
        yield start, len(rows)


def _chain_rule(task, chain, rows, rule):
    nodes, violations, metrics = task.nodes, [], []
    if rule.kind in (NumericRuleKind.HIGH_SURFACE, NumericRuleKind.NARROW_WEIGHT,
                     NumericRuleKind.SAME_SPEC_WEIGHT):
        derived, limit = rule.values
        if rule.kind is NumericRuleKind.HIGH_SURFACE:
            mask = task.surface_matches[derived]
            metric_kind, reason = NumericMetricKind.HIGH_SURFACE_RUN_MAX, NumericReason.HIGH_SURFACE_RUN
            totals = ((start, stop, stop - start) for start, stop in _runs(mask, rows))
        elif rule.kind is NumericRuleKind.NARROW_WEIGHT:
            mask = task.narrow_matches[derived]
            metric_kind, reason = NumericMetricKind.NARROW_WEIGHT_RUN_MAX, NumericReason.NARROW_WEIGHT_RUN
            totals = ((start, stop, checked_sum((int(nodes.weight[int(row)]) for row in rows[start:stop]), rule.rule_id))
                      for start, stop in _runs(mask, rows))
        else:
            group = task.same_spec_groups[derived]
            metric_kind, reason = NumericMetricKind.SAME_SPEC_WEIGHT_RUN_MAX, NumericReason.SAME_SPEC_WEIGHT_RUN
            spans, start, previous = [], None, -1
            for position, row in enumerate(rows):
                value = int(group[int(row)])
                if value < 0:
                    if start is not None:
                        spans.append((start, position))
                    start, previous = None, -1
                elif start is None or value != previous:
                    if start is not None:
                        spans.append((start, position))
                    start, previous = position, value
            if start is not None:
                spans.append((start, len(rows)))
            totals = ((start, stop, checked_sum((int(nodes.weight[int(row)]) for row in rows[start:stop]), rule.rule_id))
                      for start, stop in spans)
        maximum = 0
        for start, stop, total in totals:
            maximum = max(maximum, total)
            if total > limit:
                severity = (checked_product(total - limit, SEVERITY_SCALE, rule.rule_id)
                            if rule.kind is NumericRuleKind.HIGH_SURFACE
                            else _physical_severity(total - limit, task.units.weight, rule.rule_id))
                violations.append(_violation(rule, reason, chain, start, stop - 1, severity))
        metrics.append(NumericMetric(rule.index, metric_kind, maximum))
    elif rule.kind is NumericRuleKind.CHAIN_WEIGHT:
        total = checked_sum((int(nodes.weight[int(row)]) for row in rows), rule.rule_id)
        minimum, maximum, target = rule.values
        under, over = total < minimum, total > maximum
        gap, excess = max(0, minimum - total), max(0, total - maximum)
        if under:
            violations.append(_violation(rule, NumericReason.CHAIN_UNDERWEIGHT, chain, 0, len(rows)-1,
                                         _physical_severity(gap, task.units.weight, rule.rule_id),
                                         prohibited=False))
        if over:
            violations.append(_violation(rule, NumericReason.CHAIN_OVERWEIGHT, chain, 0, len(rows)-1,
                                         _physical_severity(excess, task.units.weight, rule.rule_id)))
        metrics.extend((
            NumericMetric(rule.index, NumericMetricKind.UNDERWEIGHT_CHAIN_COUNT, int(under)),
            NumericMetric(rule.index, NumericMetricKind.UNDERWEIGHT_TOTAL_GAP, gap),
            NumericMetric(rule.index, NumericMetricKind.OVERWEIGHT_CHAIN_COUNT, int(over)),
            NumericMetric(rule.index, NumericMetricKind.OVERWEIGHT_TOTAL_EXCESS, excess),
            NumericMetric(rule.index, NumericMetricKind.CHAIN_TARGET_WEIGHT_DEVIATION, abs(total-target)),
        ))
    elif rule.kind is NumericRuleKind.CONSECUTIVE_VIRTUAL:
        mask = nodes.role == _GENERATED_VIRTUAL
        maximum, limit = 0, rule.values[0]
        for start, stop in _runs(mask, rows):
            count = stop - start
            maximum = max(maximum, count)
            if count > limit:
                violations.append(_violation(rule, NumericReason.VIRTUAL_RUN, chain, start, stop-1,
                                             checked_product(count-limit, SEVERITY_SCALE, rule.rule_id)))
        metrics.append(NumericMetric(rule.index, NumericMetricKind.CONSECUTIVE_VIRTUAL_MAX, maximum))
    elif rule.kind is NumericRuleKind.REVERSE_WIDTH_COUNT:
        count, baseline = 0, None
        for row in rows:
            row = int(row)
            if not nodes.present[row, 0]:
                baseline = None
            elif baseline is not None and int(nodes.width[row]) > baseline:
                count += 1
            else:
                baseline = int(nodes.width[row])
        if count > rule.values[0]:
            violations.append(_violation(rule, NumericReason.REVERSE_WIDTH_COUNT, chain, 0, len(rows)-1,
                                         checked_product(count-rule.values[0], SEVERITY_SCALE, rule.rule_id)))
        metrics.append(NumericMetric(rule.index, NumericMetricKind.REVERSE_WIDTH_COUNT, count))
    elif rule.kind is NumericRuleKind.CONSECUTIVE_REVERSE_WIDTH:
        count, previous = 0, False
        for position in range(len(rows)-1):
            left, right = int(rows[position]), int(rows[position+1])
            reverse = (nodes.present[left, 0] and nodes.present[right, 0]
                       and int(nodes.width[right]) > int(nodes.width[left]))
            if previous and reverse:
                count += 1
                violations.append(_violation(rule, NumericReason.CONSECUTIVE_REVERSE_WIDTH,
                                             chain, position-1, position, SEVERITY_SCALE))
            previous = reverse
        metrics.append(NumericMetric(rule.index, NumericMetricKind.CONSECUTIVE_REVERSE_WIDTH_COUNT, count))
    elif rule.kind is NumericRuleKind.WIDTH:
        anchor, anchor_position = None, -1
        for position, row in enumerate(rows):
            row = int(row)
            if int(nodes.role[row]) == _GENERATED_VIRTUAL:
                continue
            if anchor is not None and position > anchor_position + 1:
                if not nodes.present[anchor, 0] or not nodes.present[row, 0]:
                    violations.append(_violation(rule, NumericReason.MISSING_WIDTH, chain,
                                                 anchor_position, position, SEVERITY_SCALE))
                else:
                    delta = int(nodes.width[row]) - int(nodes.width[anchor])
                    if delta > rule.values[0]:
                        severity = _severity_ratio(delta-rule.values[0],
                                                   max(rule.values[0], task.units.width), rule.rule_id)
                        violations.append(_violation(rule, NumericReason.VIRTUAL_BRIDGE_WIDTH,
                                                     chain, anchor_position, position, severity))
            anchor, anchor_position = row, position
    return NumericRuleResult(tuple(violations), tuple(metrics))


def _evaluate_numeric_rows(task, program, rows, chain_index):
    results = []
    edge_rules = tuple(rule for rule in program.rules if rule.scope is RuleScope.EDGE)
    first_edge = min((rule.index for rule in edge_rules), default=len(program.rules))
    results.extend(_chain_rule(task, chain_index, rows, rule)
                   for rule in program.rules
                   if rule.scope is RuleScope.CHAIN and rule.index < first_edge)
    for position in range(len(rows)-1):
        results.extend(_edge_rule(task, rule, int(rows[position]), int(rows[position+1]),
                                  chain_index, position) for rule in edge_rules)
    results.extend(_chain_rule(task, chain_index, rows, rule)
                   for rule in program.rules
                   if ((rule.scope is RuleScope.CHAIN and rule.index >= first_edge)
                       or rule.kind is NumericRuleKind.WIDTH))
    return NumericRuleResult(tuple(v for result in results for v in result.violations),
                             tuple(m for result in results for m in result.metrics))


def evaluate_numeric_rows(task, program, rows, *, chain_index=0):
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
    return _evaluate_numeric_rows(task, program, values, chain_index)


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
    return _evaluate_numeric_rows(task, program, _numeric_chain_rows(plan, chain_index), chain_index)


def evaluate_numeric_static_plan_rules(
        task, program, plan, *, chain_total_weights=None,
        real_weight=None, virtual_weight=None):
    if (not isinstance(plan, (NumericPlan, NumericPlanOverlay)) or program.task_fingerprint != task.fingerprint
            or plan.task_fingerprint != task.fingerprint):
        raise NumericValueError('plan', 'matching numeric task, program and plan required')
    violations, metrics = [], []
    nodes = task.nodes
    if any(int(plan.chain_periods[index]) < int(plan.chain_periods[index-1])
           for index in range(1, plan.chain_periods.size)):
        raise NumericValueError('chain_periods', 'production chains must follow task period order')
    provided = tuple(value is not None for value in
                     (chain_total_weights, real_weight, virtual_weight))
    if any(provided) and not all(provided):
        raise NumericValueError('plan_facts', 'complete chain and resource facts required')
    if all(provided):
        try:
            chain_weights = tuple(int64(integer_index(value), 'chain_total_weight')
                                  for value in chain_total_weights)
            real_weight = int64(integer_index(real_weight), 'scheduled_real_weight')
            virtual_weight = int64(integer_index(virtual_weight), 'generated_virtual_weight')
        except TypeError as error:
            raise NumericValueError('plan_facts', 'integer chain and resource facts required') from error
        if (len(chain_weights) != plan.chain_ids.size
                or min((*chain_weights, real_weight, virtual_weight), default=0) < 0
                or checked_sum(chain_weights, 'scheduled_total_weight')
                != checked_sum((real_weight, virtual_weight), 'scheduled_total_weight')):
            raise NumericValueError('plan_facts', 'chain and resource facts do not match')
    else:
        real_weight = checked_sum(
            (
                int(nodes.weight[int(row)])
                for chain in range(plan.chain_ids.size)
                for row in _numeric_chain_rows(plan, chain)
                if int(nodes.role[int(row)]) != _GENERATED_VIRTUAL
            ),
            'scheduled_real_weight',
        )
        virtual_weight = checked_sum(
            (
                int(nodes.weight[int(row)])
                for chain in range(plan.chain_ids.size)
                for row in _numeric_chain_rows(plan, chain)
                if int(nodes.role[int(row)]) == _GENERATED_VIRTUAL
            ),
            'generated_virtual_weight',
        )
        chain_weights = tuple(
            checked_sum((int(nodes.weight[int(row)])
                         for row in _numeric_chain_rows(plan, chain)),
                        'chain_total_weight')
            for chain in range(plan.chain_ids.size))
    total_weight = checked_sum((real_weight, virtual_weight), 'scheduled_total_weight')
    for rule in program.rules:
        if rule.kind is NumericRuleKind.LATE_PERIOD:
            count = 0
            for chain in range(plan.chain_ids.size):
                for position, row in enumerate(_numeric_chain_rows(plan, chain)):
                    row = int(row)
                    if int(nodes.role[row]) != _GENERATED_VIRTUAL and int(plan.chain_periods[chain]) > int(nodes.source_period[row]):
                        count += 1
                        violations.append(_violation(rule, NumericReason.LATE_PERIOD, chain,
                                                     position, position, SEVERITY_SCALE))
            metrics.append(NumericMetric(rule.index, NumericMetricKind.LATE_PERIOD_COUNT, count))
        elif rule.kind is NumericRuleKind.VIRTUAL_RATIO:
            numerator, denominator = rule.values
            virtual_cross = checked_product(virtual_weight, denominator, rule.rule_id)
            total_cross = checked_product(total_weight, numerator, rule.rule_id)
            exceeded = total_weight and virtual_cross > total_cross
            if exceeded:
                excess = int64(virtual_cross - total_cross, rule.rule_id)
                severity = (SEVERITY_SCALE if numerator == 0 else
                            _severity_ratio(excess, checked_product(total_weight, denominator, rule.rule_id),
                                            rule.rule_id, at_least_one=False))
                violations.append(_violation(rule, NumericReason.VIRTUAL_RATIO, -1, -1, -1,
                                             max(1, severity)))
            metrics.append(NumericMetric(rule.index, NumericMetricKind.VIRTUAL_RATIO,
                                         virtual_weight, total_weight or 1))
        elif rule.kind is NumericRuleKind.INTER_CHAIN_WIDTH:
            gap = 0
            for left_chain in range(plan.chain_ids.size-1):
                left_row = int(_numeric_chain_rows(plan, left_chain)[-1])
                right_row = int(_numeric_chain_rows(plan, left_chain + 1)[0])
                if not nodes.present[left_row, 0] or not nodes.present[right_row, 0]:
                    raise NumericValueError(rule.rule_id, 'inter-chain boundary width is missing')
                gap = int64(gap + abs(int(nodes.width[left_row])-int(nodes.width[right_row])), rule.rule_id)
            metrics.append(NumericMetric(rule.index, NumericMetricKind.INTER_CHAIN_WIDTH_GAP, gap))
        elif rule.kind is NumericRuleKind.FUTURE_FILL:
            target, gap = rule.values[0], 0
            for chain in range(plan.chain_ids.size):
                gap = int64(gap + max(0, target-chain_weights[chain]), rule.rule_id)
            metrics.append(NumericMetric(rule.index, NumericMetricKind.FUTURE_FILL_TOTAL_GAP, gap))
    return NumericRuleResult(tuple(violations), tuple(metrics))


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

