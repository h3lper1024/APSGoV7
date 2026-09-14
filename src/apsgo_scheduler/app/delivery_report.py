"""Read-only backend report, bound to the exact request and returned schedule."""

from datetime import datetime, timedelta
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from ..api.request import fingerprint_public_request
from ..core.chain_order import has_backlog_priority, has_delivery_objective, has_second_precision_delivery
from ..core.contracts import fingerprint, sum_weights
from ..core.delivery_timing import evaluate_delivery, score_time_seconds, weighted_wait_seconds, score_seconds_to_hours
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
    report = delivery_plan_report(plan, problem.delivery_timing,
                                  include_backlog_clearance=backlog_priority,
                                  second_precision=has_second_precision_delivery(rules))
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
