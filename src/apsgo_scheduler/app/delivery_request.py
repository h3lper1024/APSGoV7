"""Opt-in backend delivery specification; never mutate the active rules database."""

from dataclasses import replace

from ..api.request import QualityCriterionSpec, RuleDefinitionSpec
from ..core.contracts import RuleScope
from .rule_set_loader import fingerprint_rule_set_spec, load_rule_set


def with_delivery_objective(spec):
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
    result = replace(
        spec, version=spec.version + "+delivery-v1",
        rules=(*spec.rules, RuleDefinitionSpec(
            "delivery_due_performance", "DeliveryDuePerformanceRule", "交期表现",
            RuleScope.PLAN, True, "1", {},
        )),
        quality_spec=(*spec.quality_spec[:4], *criteria, *spec.quality_spec[4:]),
    )
    result = replace(result, fingerprint=fingerprint_rule_set_spec(result))
    load_rule_set(result)
    return result
