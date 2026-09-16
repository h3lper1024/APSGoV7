"""Read-only backend report, bound to the exact request and returned schedule."""

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, localcontext
from zoneinfo import ZoneInfo

from ..api.request import fingerprint_public_request
from ..core._numeric_state import NumericTask
from ..core._numeric_units import (
    allocate_piece_milliseconds,
    choose_scale,
    due_milliseconds,
    hours_to_milliseconds,
    score_seconds,
    start_milliseconds,
    to_ticks,
)
from ..core.chain_order import (
    has_backlog_priority,
    has_delivery_objective,
    has_second_precision_delivery,
)
from ..core.contracts import (
    INTEGER_NUMERIC_SEMANTICS_KEY,
    fingerprint,
    sum_weights,
)
from ..core.delivery_timing import (
    evaluate_delivery,
    score_seconds_to_hours,
    score_time_seconds,
    weighted_wait_seconds,
)
from .input_normalizer import normalize_input
from .rule_set_loader import load_rule_set


def delivery_plan_report(plan, timing, *, include_backlog_clearance=False, second_precision=False):
    """Also usable for a clearly labelled historical-plan comparison, not publication."""
    if second_precision and not include_backlog_clearance:
        raise ValueError("second precision report requires backlog clearance")
    performance = evaluate_delivery(plan, timing, details=True, second_precision=second_precision)
    start = datetime.fromisoformat(timing.schedule_start_at)
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        def at(hours):
            microseconds = int((hours * Decimal(3600000000)).to_integral_value())
            return (start + timedelta(microseconds=microseconds)).isoformat()

        orders = []
        for key, order in timing.orders.items():
            completion = performance.original_completion_hours[key]
            backlog = order.due_hours <= 0
            tardiness = max(Decimal(0), completion - order.due_hours)
            wait = max(Decimal(0), completion - max(Decimal(0), order.due_hours))
            late = completion > order.due_hours
            orders.append(dict(
                source_order_id=key, due_date=order.due_date, original_weight=order.weight,
                due_hours=order.due_hours, was_backlog_at_start=backlog,
                newly_late=late and not backlog, is_late=late,
                classification="backlog" if backlog else "newly_late" if late else "on_time",
                completion_hours=completion, completion_at=at(completion),
                actual_tardiness_hours=tardiness, wait_tardiness_hours=wait,
                newly_late_original_weight=order.weight if late and not backlog else Decimal(0),
                delivery_wait_tardiness_tonne_hours=order.weight * wait,
            ))
            if second_precision:
                tonne_seconds = weighted_wait_seconds(order.weight, wait)
                orders[-1].update(
                    raw_delivery_wait_tardiness_tonne_hours=order.weight * wait,
                    score_wait_tardiness_seconds=score_time_seconds(wait),
                    score_wait_tardiness_tonne_seconds=tonne_seconds,
                    delivery_wait_tardiness_tonne_hours=score_seconds_to_hours(tonne_seconds),
                )
        nodes = [dict(node_id=key, start_hours=before, completion_hours=after,
                      start_at=at(before), completion_at=at(after))
                 for key, before, after in performance.node_times]
        summary = dict(
            schedule_start_at=timing.schedule_start_at, timezone="Asia/Shanghai",
            timing_semantics=timing.semantics, timing_input_complete=True,
            newly_late_original_weight=performance.newly_late_original_weight,
            delivery_wait_tardiness_tonne_hours=performance.delivery_wait_tardiness_tonne_hours,
            maximum_actual_tardiness_hours=max(row["actual_tardiness_hours"] for row in orders),
            old_backlog_last_completion_at=max((row["completion_at"] for row in orders if row["was_backlog_at_start"]), default=None),
            production_end_at=nodes[-1]["completion_at"],
            production_end_hours=nodes[-1]["completion_hours"],
        )
        if include_backlog_clearance:
            summary["old_backlog_last_completion_hours"] = performance.old_backlog_last_completion_hours
        if second_precision:
            summary.update(
                delivery_score_time_unit="second", delivery_score_rounding="half_even",
                raw_old_backlog_last_completion_hours=max(
                    (row["completion_hours"] for row in orders if row["was_backlog_at_start"]), default=Decimal(0)),
                score_wait_tardiness_tonne_seconds=sum_weights(row["score_wait_tardiness_tonne_seconds"] for row in orders),
            )
        for name, field in (("old_backlog", "was_backlog_at_start"), ("newly_late", "newly_late"), ("total_late", "is_late")):
            group = [row for row in orders if row[field]]
            summary[name + "_order_count"] = len(group)
            summary[name + "_weight"] = sum_weights(row["original_weight"] for row in group)
    return dict(delivery_summary=summary, delivery_orders=orders, delivery_nodes=nodes)


def numeric_delivery_plan_report(
    plan,
    timing,
    timing_input,
    *,
    include_backlog_clearance=False,
    numeric_units=None,
):
    """Report the audited integer clock without reintroducing the old Decimal evaluator."""
    inputs = {item.source_order_id: item for item in timing_input.orders}
    if set(inputs) != set(timing.orders):
        raise ValueError("numeric delivery report timing input does not match the task")
    real_nodes = {
        source: [
            node
            for chain in plan.chains
            for node in chain.nodes
            if node.virtual_lineage is None and node.source_order_id == source
        ]
        for source in timing.orders
    }
    weights = [order.weight for order in timing.orders.values()]
    weights.extend(node.weight for nodes in real_nodes.values() for node in nodes)
    weight_scale = choose_scale(weights, "delivery_report.weight", minimum_scale=100)
    duration_by_node, original_errors, split_errors, split_node_ids = {}, [], [], set()
    for source, nodes in real_nodes.items():
        order = timing.orders[source]
        if sum_weights(node.weight for node in nodes) != order.weight:
            raise ValueError("numeric delivery report requires conserved original order weights")
        total_duration = hours_to_milliseconds(
            inputs[source].duration_hours, f"delivery_report.orders[{source}].duration"
        )
        original_errors.append(
            abs(Decimal(total_duration) - inputs[source].duration_hours * Decimal(3_600_000))
        )
        split = [node for node in nodes if node.split_lineage is not None]
        if split:
            if len(split) != len(nodes):
                raise ValueError("numeric delivery report found mixed split and original nodes")
            ordered = sorted(split, key=lambda node: node.split_lineage.piece_index)
            if [node.split_lineage.piece_index for node in ordered] != list(
                range(1, len(ordered) + 1)
            ):
                raise ValueError("numeric delivery report found incomplete split piece indices")
            piece_weights = tuple(
                to_ticks(node.weight, weight_scale, f"delivery_report.nodes[{node.node_id}].weight")
                for node in ordered
            )
            parent_weight = to_ticks(order.weight, weight_scale, f"delivery_report.orders[{source}].weight")
            durations = allocate_piece_milliseconds(
                parent_weight,
                total_duration,
                piece_weights,
                f"delivery_report.orders[{source}].split_duration",
            )
            duration_by_node.update(
                (node.node_id, duration) for node, duration in zip(ordered, durations)
            )
            split_node_ids.update(node.node_id for node in ordered)
            split_errors.extend(
                abs(
                    Decimal(duration)
                    - Decimal(total_duration) * Decimal(weight) / Decimal(parent_weight)
                )
                for duration, weight in zip(durations, piece_weights)
            )
        elif len(nodes) == 1:
            duration_by_node[nodes[0].node_id] = total_duration
        else:
            raise ValueError("numeric delivery report found an untracked order partition")

    start_ms = start_milliseconds(timing_input.schedule_start_at, "schedule_start_at")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def hours(milliseconds):
        with localcontext(Context(prec=28, rounding=ROUND_HALF_UP)):
            return Decimal(milliseconds) / Decimal(3_600_000)

    def ratio(numerator, denominator):
        with localcontext(Context(prec=28, rounding=ROUND_HALF_UP)):
            return Decimal(numerator) / Decimal(denominator)

    def at(milliseconds):
        return (epoch + timedelta(milliseconds=start_ms + milliseconds)).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).isoformat(timespec="milliseconds")

    completion, node_rows, seen, virtual_errors = {}, [], set(), []
    clock = 0
    for chain in plan.chains:
        for node in chain.nodes:
            if node.node_id in seen:
                raise ValueError("duplicate node in numeric delivery report")
            seen.add(node.node_id)
            before = clock
            if node.virtual_lineage is None:
                duration = duration_by_node[node.node_id]
            else:
                rate = timing_input.virtual_hours_per_tonne.get(
                    node.virtual_lineage.prototype_id
                )
                if rate is None:
                    raise ValueError("numeric delivery report is missing virtual timing")
                duration = hours_to_milliseconds(
                    rate,
                    f"delivery_report.nodes[{node.node_id}].duration",
                    weight=node.weight,
                )
                virtual_errors.append(
                    abs(
                        Decimal(duration)
                        - rate * node.weight * Decimal(3_600_000)
                    )
                )
            clock += duration
            if node.virtual_lineage is None:
                completion[node.source_order_id] = clock
            node_rows.append(
                dict(
                    node_id=node.node_id,
                    start_milliseconds=before,
                    completion_milliseconds=clock,
                    start_hours=hours(before),
                    completion_hours=hours(clock),
                    start_at=at(before),
                    completion_at=at(clock),
                )
            )
            if node.virtual_lineage is None:
                earliest = inputs[node.source_order_id].earliest_start_at
                if earliest is not None:
                    lower = start_milliseconds(earliest, "earliest_start_at") - start_ms
                    early = max(0, lower - before)
                    node_rows[-1].update(source_order_id=node.source_order_id,
                        earliest_start_at=earliest, earliest_start_milliseconds=lower,
                        early_start_milliseconds=early, early_start_seconds=ratio(early, 1000))
    if set(completion) != set(timing.orders):
        raise ValueError("numeric delivery report does not complete every original order")

    orders, burden = [], 0
    for source, order in timing.orders.items():
        due = due_milliseconds(order.due_date, start_ms, f"delivery_report.orders[{source}].due")
        finished = completion[source]
        backlog = due <= 0
        late = finished > due
        wait_ms = max(0, finished - max(0, due))
        wait_seconds = score_seconds(wait_ms, f"delivery_report.orders[{source}].wait")
        weight_ticks = to_ticks(order.weight, weight_scale, f"delivery_report.orders[{source}].weight")
        burden += weight_ticks * wait_seconds
        orders.append(
            dict(
                source_order_id=source,
                due_date=order.due_date,
                original_weight=order.weight,
                due_milliseconds=due,
                due_hours=hours(due),
                was_backlog_at_start=backlog,
                newly_late=late and not backlog,
                is_late=late,
                classification="backlog" if backlog else "newly_late" if late else "on_time",
                completion_milliseconds=finished,
                completion_hours=hours(finished),
                completion_at=at(finished),
                actual_tardiness_hours=hours(max(0, finished - due)),
                wait_tardiness_hours=hours(wait_ms),
                score_wait_tardiness_seconds=wait_seconds,
                score_wait_tardiness_tonne_seconds=ratio(
                    weight_ticks * wait_seconds, weight_scale
                ),
                newly_late_original_weight=order.weight if late and not backlog else Decimal(0),
                raw_delivery_wait_tardiness_tonne_hours=order.weight * hours(wait_ms),
                delivery_wait_tardiness_tonne_hours=ratio(
                    weight_ticks * wait_seconds, weight_scale * 3600
                ),
            )
        )
    old_completion = max(
        (completion[row["source_order_id"]] for row in orders if row["was_backlog_at_start"]),
        default=0,
    )
    summary = dict(
        schedule_start_at=timing_input.schedule_start_at,
        timezone="Asia/Shanghai",
        timing_semantics=INTEGER_NUMERIC_SEMANTICS_KEY,
        input_timing_semantics=timing.semantics,
        timing_input_complete=True,
        delivery_score_time_unit="second",
        delivery_score_rounding="half_up",
        newly_late_original_weight=sum_weights(
            row["newly_late_original_weight"] for row in orders
        ),
        delivery_wait_tardiness_tonne_hours=ratio(burden, weight_scale * 3600),
        maximum_actual_tardiness_hours=max(
            row["actual_tardiness_hours"] for row in orders
        ),
        old_backlog_last_completion_at=None if old_completion == 0 else at(old_completion),
        raw_old_backlog_last_completion_hours=hours(old_completion),
        score_wait_tardiness_tonne_seconds=ratio(burden, weight_scale),
        production_end_at=at(clock),
        production_end_milliseconds=clock,
        production_end_hours=hours(clock),
    )
    if include_backlog_clearance:
        summary["old_backlog_last_completion_hours"] = hours(
            score_seconds(old_completion, "delivery_report.old_backlog") * 1000
        )
    if any(item.earliest_start_at is not None for item in inputs.values()):
        early_rows = [row for row in node_rows if row.get("early_start_milliseconds", 0) > 0]
        summary.update(early_start_node_count=len(early_rows),
            early_start_original_count=len({row["source_order_id"] for row in early_rows}),
            early_start_total_seconds=ratio(sum(row["early_start_milliseconds"] for row in early_rows), 1000))
    for name, field in (
        ("old_backlog", "was_backlog_at_start"),
        ("newly_late", "newly_late"),
        ("total_late", "is_late"),
    ):
        group = [row for row in orders if row[field]]
        summary[name + "_order_count"] = len(group)
        summary[name + "_weight"] = sum_weights(row["original_weight"] for row in group)
    errors = (*original_errors, *virtual_errors)
    conversion = dict(
        numeric_standard=INTEGER_NUMERIC_SEMANTICS_KEY,
        physical_units=None
        if numeric_units is None
        else {
            name: getattr(numeric_units, name)
            for name in ("width", "thickness", "temperature", "weight")
        },
        time_unit="millisecond",
        score_time_unit="second",
        rounding="ROUND_HALF_UP",
        original_and_virtual_duration_quantization_count=len(errors),
        total_absolute_duration_quantization_error_milliseconds=sum(errors, Decimal(0)),
        maximum_absolute_duration_quantization_error_milliseconds=max(
            errors, default=Decimal(0)
        ),
        split_piece_allocation_count=len(split_errors),
        maximum_absolute_split_allocation_error_milliseconds=max(
            split_errors, default=Decimal(0)
        ),
        zero_duration_split_piece_count=sum(
            duration == 0
            for node_id, duration in duration_by_node.items()
            if node_id in split_node_ids
        ),
        int64_range_checks_passed=True,
        unsupported_items=(),
    )
    return dict(
        delivery_summary=summary,
        delivery_orders=orders,
        delivery_nodes=node_rows,
        numeric_conversion=conversion,
    )


def build_delivery_report(request, result):
    """Recompute dates; never turn a diagnostic candidate into a published schedule."""
    if result.run_manifest.request_fingerprint != fingerprint_public_request(request):
        raise ValueError("delivery report result belongs to another request")
    rules = load_rule_set(request.rule_set_spec)
    if not has_delivery_objective(rules):
        raise ValueError("delivery report requires an explicitly enabled delivery objective")
    problem = normalize_input(request, rules)
    if result.run_manifest.problem_fingerprint != problem.input_fingerprint:
        raise ValueError("delivery report problem binding mismatch")
    if result.release is not None:
        if not result.core_audit.passed or not result.audit_report.passed:
            raise ValueError("delivery release requires both audits to pass")
        plan, evaluation = result.release.plan, result.release.evaluation
        kind = "audited_release"
    elif result.diagnostic_candidate is not None:
        plan, evaluation = result.diagnostic_candidate.plan, result.diagnostic_candidate.search_evaluation
        kind = "diagnostic_candidate_not_publishable"
    else:
        return None
    backlog_priority = has_backlog_priority(rules)
    report = (
        numeric_delivery_plan_report(
            plan,
            problem.delivery_timing,
            request.delivery_timing,
            include_backlog_clearance=backlog_priority,
            numeric_units=NumericTask.build(
                problem, rules, request.delivery_timing
            ).units,
        )
        if request.policy.numeric_semantics_key == INTEGER_NUMERIC_SEMANTICS_KEY
        else delivery_plan_report(
            plan,
            problem.delivery_timing,
            include_backlog_clearance=backlog_priority,
            second_precision=has_second_precision_delivery(rules),
        )
    )
    delivery_metrics = ("newly_late_original_weight", "delivery_wait_tardiness_tonne_hours") + (
        ("old_backlog_last_completion_hours",) if backlog_priority else ()
    )
    # Raw statistics remain audited even when the configured score deliberately omits them.
    for key in delivery_metrics:
        if report["delivery_summary"][key] != evaluation.metrics.get(key):
            raise ValueError("delivery report differs from the returned statistics")
    for index, metric in enumerate(rules.quality_spec):
        if metric.metric_key in delivery_metrics:
            if report["delivery_summary"][metric.metric_key] != evaluation.quality_key[index]:
                raise ValueError("delivery report differs from the returned evaluation")
    if backlog_priority:
        scored = {metric.metric_key for metric in rules.quality_spec}
        report["delivery_metric_roles"] = {
            key: "score" if key in scored else "statistics_only" for key in delivery_metrics
        }
    report.update(kind=kind, request_fingerprint=fingerprint_public_request(request),
                  problem_fingerprint=problem.input_fingerprint, rule_set_fingerprint=rules.fingerprint,
                  plan_fingerprint=fingerprint(plan))
    report["report_fingerprint"] = fingerprint(report)
    return report
