"""Opt-in backend delivery specification; never mutate the active rules database."""

from dataclasses import replace
from collections.abc import Mapping
from decimal import Decimal

from ..api.request import QualityCriterionSpec, RuleDefinitionSpec
from ..core.contracts import RuleScope, require_decimal
from ..core.delivery_timing import DeliveryTimingInput, OrderTimingInput, production_hours
from .input_normalizer import normalize_input
from .rule_set_loader import fingerprint_rule_set_spec, load_rule_set


def with_delivery_objective(spec, *, include_backlog_clearance=False, second_precision=False):
    if type(include_backlog_clearance) is not bool:
        raise ValueError("include_backlog_clearance must be boolean")
    if type(second_precision) is not bool or (second_precision and not include_backlog_clearance):
        raise ValueError("second_precision must be boolean and requires backlog clearance")
    load_rule_set(spec)
    expected = (
        "prohibited_violation_count", "prohibited_violation_severity",
        "underweight_chain_count", "underweight_total_gap", "inter_chain_width_gap",
        "generated_virtual_weight", "chain_count",
    )
    if tuple(item.metric_key for item in spec.quality_spec) != expected:
        raise ValueError("delivery extension requires the existing seven-level quality specification")
    criteria = tuple(QualityCriterionSpec(key, key, "minimize", "sum", "exact_decimal") for key in (
        "old_backlog_last_completion_hours" if include_backlog_clearance else "newly_late_original_weight",
        "delivery_wait_tardiness_tonne_hours",
    ))
    parameters = {"include_backlog_clearance": True} if include_backlog_clearance else {}
    suffix = "+delivery-backlog-priority-v1" if include_backlog_clearance else "+delivery-v1"
    if second_precision:
        parameters["score_time_unit"] = "second"
        suffix = "+delivery-backlog-seconds-v1"
    insertion = 2 if include_backlog_clearance and not second_precision else 4
    result = replace(
        spec, version=spec.version + suffix,
        rules=(*spec.rules, RuleDefinitionSpec(
            "delivery_due_performance", "DeliveryDuePerformanceRule", "交期表现",
            RuleScope.PLAN, True, "3" if second_precision else "2" if include_backlog_clearance else "1",
            parameters,
        )),
        quality_spec=(*spec.quality_spec[:insertion], *criteria, *spec.quality_spec[insertion:]),
    )
    result = replace(result, fingerprint=fingerprint_rule_set_spec(result))
    load_rule_set(result)
    return result


def prepare_delivery_request(request, *, schedule_start_at, order_timing, virtual_speed_mpm,
                             include_backlog_clearance=False, second_precision=False):
    """Typed backend input: speed selection happens once, before any search."""
    problem = normalize_input(request)
    if not isinstance(order_timing, Mapping) or set(order_timing) != {n.source_order_id for n in problem.nodes}:
        raise ValueError("order_timing must match all original order identities")
    require_decimal(virtual_speed_mpm, "virtual_speed_mpm", positive=True)
    inputs = []
    for node in problem.nodes:
        values = order_timing[node.source_order_id]
        required = {"due_date", "furnace_speed_mpm", "process_speed_mpm"}
        if (not isinstance(values, Mapping) or not required <= set(values)
                or set(values) - required - {"earliest_start_at"}):
            raise ValueError(f"{node.source_order_id}: timing requires due_date and both speed fields")
        speeds = (values["furnace_speed_mpm"], values["process_speed_mpm"])
        for name, value in zip(("furnace_speed_mpm", "process_speed_mpm"), speeds):
            require_decimal(value, f"{node.source_order_id}.{name}", allow_none=True)
        speed = next((value for value in speeds if value is not None and value > 0), None)
        if speed is None:
            raise ValueError(f"{node.source_order_id}: no positive production speed")
        inputs.append(OrderTimingInput(node.source_order_id, values["due_date"],
            production_hours(node.weight, node.width, node.thickness, speed),
            values.get("earliest_start_at")))
    timing = DeliveryTimingInput(schedule_start_at, tuple(inputs), {
        item.prototype_id: production_hours(Decimal(1), item.width, item.thickness, virtual_speed_mpm)
        for item in problem.virtual_prototypes
    })
    result = replace(request, contract_version="delivery-backend-v1", delivery_timing=timing,
                     rule_set_spec=with_delivery_objective(request.rule_set_spec,
                                                         include_backlog_clearance=include_backlog_clearance,
                                                         second_precision=second_precision))
    normalize_input(result)
    return result
