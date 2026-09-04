"""Optional reference projection check; not part of production or portable tests."""

import json
import runpy
import sys
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import Chain, MaterialRole, Node, VirtualLineage, VirtualPurpose
from apsgo_scheduler.core.rules.base import ChainRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import ChainWeightRangeRule

source = Path(sys.argv[1])
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_chain_weight_differential")
D = Decimal
rule_id = "chain_weight_range"
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
roles_to_material = {
    "normal": MaterialRole.NORMAL_REAL,
    "actual": MaterialRole.ACTUAL_TRANSITION,
    "virtual": MaterialRole.GENERATED_VIRTUAL,
}
role_patterns = (
    ("normal",),
    ("actual",),
    ("normal", "actual"),
    ("normal", "virtual"),
    ("virtual", "normal"),
    ("normal", "actual", "virtual"),
)


def rules_for(minimum, maximum, target):
    return reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [
                    {
                        "rule_id": rule_id,
                        "enabled": True,
                        "params": {
                            "min_weight": minimum,
                            "max_weight": maximum,
                            "target_weight": target,
                        },
                    }
                ]
            }
        },
        {},
        {"period_order": ["period"]},
    )


def old_node(index, weight, role="normal"):
    return replace(
        reference["_sentinel_node"](),
        node_id=f"n-{index}",
        source_order_id=f"o-{index}",
        source_period="period",
        weight=weight,
        actual_transition_material=role == "actual",
        node_type="virtual_sphc" if role == "virtual" else "real",
    )


def compare(total, roles, minimum, maximum, target):
    fractions = (
        (D(1),)
        if len(roles) == 1
        else ((D("0.5"), D("0.5")) if len(roles) == 2 else (D("0.25"), D("0.25"), D("0.5")))
    )
    old_nodes, new_nodes = [], []
    for index, (role, fraction) in enumerate(zip(roles, fractions)):
        weight = total * fraction
        virtual = role == "virtual"
        old_nodes.append(old_node(index, weight, role))
        new_nodes.append(
            Node(
                node_id=f"n-{index}",
                source_order_id=None if virtual else f"o-{index}",
                source_resource_id=None if virtual else f"r-{index}",
                source_period=None if virtual else "period",
                weight=weight,
                width=None,
                thickness=None,
                min_temperature=None,
                max_temperature=None,
                grade="",
                material_role=roles_to_material[role],
                rule_attributes={},
                virtual_lineage=VirtualLineage(
                    "prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1
                )
                if virtual
                else None,
            )
        )
    old = reference["evaluate_chain"](
        reference["Chain"](old_nodes, "period"), 0, rules_for(minimum, maximum, target), "C"
    )
    rule = ChainWeightRangeRule(
        rule_id=rule_id,
        name="链重范围与目标",
        scope=RuleScope.CHAIN,
        enabled=True,
        version="1",
        parameters={"min_weight": minimum, "max_weight": maximum, "target_weight": target},
    )
    new = rule.evaluate(ChainRuleSubject("C", Chain("C", tuple(new_nodes), "period")), context)
    expected_violations = tuple(
        (
            item["subject_id"],
            "chain_weight_above_maximum" if item["prohibited"] else "chain_weight_below_minimum",
            item["severity_value"],
            RuleDisposition.PROHIBITED
            if item["prohibited"]
            else RuleDisposition.ALLOWED_FINAL_DEVIATION,
        )
        for item in old.violations
        if item["rule_id"] == rule_id
    )
    observed_violations = tuple(
        (item.subject_id, item.reason_code, float(item.severity), item.disposition)
        for item in new.violations
    )
    assert observed_violations == expected_violations, (
        total,
        roles,
        minimum,
        maximum,
        target,
        expected_violations,
        observed_violations,
    )
    metrics = {item.metric_key: item.value for item in new.metrics}
    assert len(metrics) == len(new.metrics)
    under, over = old.summary["underweight"], old.summary["overweight"]
    # The new metrics retain exact deficits only on flagged chains.
    # Reference summary decimal_text values are already six-place rounded and are NOT
    # a valid exact deficit oracle. Their later quality projection is checked below.
    expected_metrics = {
        "underweight_chain_count": int(under),
        "underweight_total_gap": minimum - total if under else D(0),
        "overweight_chain_count": int(over),
        "overweight_total_excess": total - maximum if over else D(0),
        # The reference does not consume target_weight. This is a separate new metric.
        "chain_target_weight_deviation": abs(total - target),
    }
    assert metrics == expected_metrics, (
        total,
        roles,
        minimum,
        maximum,
        target,
        expected_metrics,
        metrics,
    )
    assert tuple(item.severity for item in new.violations) == (
        ((minimum - total,) if under else ()) + ((total - maximum,) if over else ())
    )


configurations = tuple(
    tuple(D(value) for value in row)
    for row in (
        ("700", "2000", "2000"),
        ("0", "2000", "0"),
        ("100", "100", "100"),
        ("10", "20", "30"),
        ("10", "20", "0"),
        ("0", "0.000001", "0"),
    )
)
offsets = tuple(
    D(value)
    for value in (
        "-0.0000011",
        "-0.000001",
        "-0.0000005",
        "0",
        "0.0000005",
        "0.000001",
        "0.0000011",
    )
)
fixed_totals = {D(value) for value in ("0.0000001", "1", "100", "700", "1000", "2000", "2400")}
count = 0
for minimum, maximum, target in configurations:
    totals = sorted(
        fixed_totals
        | {
            bound + offset
            for bound, offset in product((minimum, maximum), offsets)
            if bound + offset > 0
        }
    )
    for total, roles in product(totals, role_patterns):
        compare(total, roles, minimum, maximum, target)
        count += 1

# A documented function 7 / 5.19 projection requirement, not a current plan evaluator test.
reference_rules = rules_for(D("700"), D("2000"), D("2000"))
weights = (D("699.99999851"), D("699.99999851"))
chains = [
    reference["Chain"]([old_node(index, weight)], "period") for index, weight in enumerate(weights)
]
old_plan = reference["evaluate_plan"](chains, reference_rules)
assert old_plan.quality[2:4] == (2, 0.000002)
naive_sum_then_round = round(float(sum((D("700") - weight for weight in weights), D(0))), 6)
assert naive_sum_then_round == 0.000003
within_tolerance = reference["evaluate_plan"](
    [reference["Chain"]([old_node(0, D("699.999999"))], "period")], reference_rules
)
assert within_tolerance.quality[2:4] == (0, 0.0)
assert within_tolerance.chain_evaluations[0].summary["underweight_gap"] == "0.000001"

# The old implementation leaks an underweight quality term after disabling the rule.
disabled_old = reference["evaluate_plan"](
    [reference["Chain"]([old_node(0, D("600"))], "period")],
    replace(reference_rules, enabled={}),
)
assert disabled_old.violations == () and disabled_old.quality[2:4] == (1, 100.0)
disabled_rule = ChainWeightRangeRule(
    rule_id=rule_id,
    name="链重范围与目标",
    scope=RuleScope.CHAIN,
    enabled=False,
    version="1",
    parameters={},
)
disabled_node = Node(
    node_id="disabled",
    source_order_id="disabled",
    source_resource_id="disabled",
    source_period="period",
    weight=D("600"),
    width=None,
    thickness=None,
    min_temperature=None,
    max_temperature=None,
    grade="",
    material_role=MaterialRole.NORMAL_REAL,
    rule_attributes={},
)
disabled_new = disabled_rule.evaluate(
    ChainRuleSubject("C", Chain("C", (disabled_node,), "period")), context
)
assert disabled_new.violations == disabled_new.metrics == ()
print(
    json.dumps(
        {
            "status": "pass",
            "compared_cases": count,
            "configurations": len(configurations),
            "role_patterns": len(role_patterns),
            "first_difference": None,
            "exact_gap_metrics_not_compared_to_rounded_reference_summary": True,
            "reference_per_chain_rounding_gap": old_plan.quality[3],
            "naive_sum_then_round_gap": naive_sum_then_round,
            "intentional_disabled_rule_fix_checked": True,
            "target_metric_checked_separately": True,
            "reference_sha256": source_hash,
        }
    )
)
