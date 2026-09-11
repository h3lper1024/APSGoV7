"""Opt-in backend delivery specification; never mutate the active rules database."""

from dataclasses import replace
from collections.abc import Mapping
from decimal import Decimal

from ..api.request import QualityCriterionSpec, RuleDefinitionSpec
from ..core.contracts import RuleScope, require_decimal
from ..core.delivery_timing import DeliveryTimingInput, OrderTimingInput, production_hours
from .input_normalizer import normalize_input
from .rule_set_loader import fingerprint_rule_set_spec, load_rule_set


def with_delivery_objective(spec, *, delivery_first=False):
    if not isinstance(delivery_first, bool):
        raise ValueError("delivery_first must be a boolean")
    load_rule_set(spec)
    expected = (
        "prohibited_violation_count", "prohibited_violation_severity",
        "underweight_chain_count", "underweight_total_gap", "inter_chain_width_gap",
        "generated_virtual_weight", "chain_count",
    )
    if tuple(item.metric_key for item in spec.quality_spec) != expected:
        raise ValueError("delivery extension requires the existing seven-level quality specification")
    criteria = tuple(QualityCriterionSpec(key, key, "minimize", "sum", "exact_decimal") for key in (
        "newly_late_original_weight", "delivery_wait_tardiness_tonne_hours",
    ))
    position = 2 if delivery_first else 4
    result = replace(
        spec, version=spec.version + ("+delivery-first-v1" if delivery_first else "+delivery-v1"),
        rules=(*spec.rules, RuleDefinitionSpec(
            "delivery_due_performance", "DeliveryDuePerformanceRule", "交期表现",
            RuleScope.PLAN, True, "1", {},
        )),
        quality_spec=(*spec.quality_spec[:position], *criteria, *spec.quality_spec[position:]),
    )
    result = replace(result, fingerprint=fingerprint_rule_set_spec(result))
    load_rule_set(result)
    return result


def prepare_delivery_request(request, *, schedule_start_at, order_timing, virtual_speed_mpm, delivery_first=False):
    """Typed backend input: speed selection happens once, before any search."""
    problem = normalize_input(request)
    if not isinstance(order_timing, Mapping) or set(order_timing) != {n.source_order_id for n in problem.nodes}:
        raise ValueError("order_timing must match all original order identities")
    require_decimal(virtual_speed_mpm, "virtual_speed_mpm", positive=True)
    inputs = []
    for node in problem.nodes:
        values = order_timing[node.source_order_id]
        if not isinstance(values, Mapping) or set(values) != {"due_date", "furnace_speed_mpm", "process_speed_mpm"}:
            raise ValueError(f"{node.source_order_id}: timing requires due_date and both speed fields")
        speeds = (values["furnace_speed_mpm"], values["process_speed_mpm"])
        for name, value in zip(("furnace_speed_mpm", "process_speed_mpm"), speeds):
            require_decimal(value, f"{node.source_order_id}.{name}", allow_none=True)
        speed = next((value for value in speeds if value is not None and value > 0), None)
        if speed is None:
            raise ValueError(f"{node.source_order_id}: no positive production speed")
        inputs.append(OrderTimingInput(node.source_order_id, values["due_date"], production_hours(node.weight, node.width, node.thickness, speed)))
    timing = DeliveryTimingInput(schedule_start_at, tuple(inputs), {
        item.prototype_id: production_hours(Decimal(1), item.width, item.thickness, virtual_speed_mpm)
        for item in problem.virtual_prototypes
    })
    result = replace(request, contract_version="delivery-backend-v1", delivery_timing=timing,
                     rule_set_spec=with_delivery_objective(request.rule_set_spec, delivery_first=delivery_first))
    normalize_input(result)
    return result
