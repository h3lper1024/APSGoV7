"""Optional small check: reference inactivity versus the explicitly added audit metric."""

import argparse
import json
import runpy
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import PlanRuleSubject, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import FutureFillWeightTargetRule

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", required=True, type=Path)
source = parser.parse_args().reference
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_future_fill_probe")
inputs = Path(__file__).resolve().parents[6] / "tests/baselines/gqga4/inputs"
paths = (inputs / "resolved_rules.json", inputs / "solver_config.json")
before_hashes = tuple(sha256(path.read_bytes()).hexdigest() for path in paths)
resolved, policy = (json.loads(path.read_text(), parse_float=Decimal) for path in paths)
rules = {item["rule_id"]: item for item in resolved["rule_snapshot"]["dsl_rules"]}
rule_id = "future_fill_weight_target"
chain_rule, future_rule = rules["chain_weight_range"], rules[rule_id]
assert (
    chain_rule["params"]
    == resolved["chain_weight"]
    == {
        "min_weight": Decimal(700),
        "max_weight": Decimal(2000),
        "target_weight": Decimal(2000),
    }
)
assert future_rule["enabled"] and future_rule["params"][rule_id] == Decimal(1200)
assert (
    resolved["resource_policy"][rule_id]
    == policy["historical_solver_policy"]["resource_policy"][rule_id]
    == Decimal(1200)
)
periods = tuple(policy["period_order"])

# Keep only these two frozen rule definitions; no other rule or search policy is being tested.
variants = [(str(value), True, value) for value in (0, 700, 1200, 2000, 5000)] + [
    ("disabled", False, 1200),
    ("missing_parameter", True, None),
    ("removed_rule", False, None),
]
reference_calls = 0
for weights in (("600", "1600"), ("1200", "2000"), ("1199.9999999",)):
    baseline = None
    for label, enabled, target in variants:
        definitions = [chain_rule]
        if label != "removed_rule":
            definitions.append(
                dict(
                    future_rule,
                    enabled=enabled,
                    params={rule_id: target} if target is not None else {},
                )
            )
        book = reference["parse_rule_book"](
            {"rule_snapshot": {"dsl_rules": definitions}},
            {},
            {"period_order": periods},
        )
        chains = [
            reference["Chain"](
                [
                    replace(
                        reference["_sentinel_node"](),
                        node_id=f"n-{index}",
                        source_order_id=f"o-{index}",
                        source_period=periods[0],
                        weight=Decimal(weight),
                    )
                ],
                periods[0],
            )
            for index, weight in enumerate(weights)
        ]
        actual = reference["evaluate_plan"](chains, book)
        assert len(actual.quality) == 7
        assert not any("future_fill" in key or "target_gap" in key for key in actual.result_metrics)
        assert all(item["rule_id"] == "chain_weight_range" for item in actual.violations)
        if baseline is None:
            baseline = actual
        assert actual == baseline, (weights, label, baseline, actual)
        reference_calls += 1


def make_plan(parts):
    roles = (
        MaterialRole.NORMAL_REAL,
        MaterialRole.ACTUAL_TRANSITION,
        MaterialRole.GENERATED_VIRTUAL,
    )
    chains = []
    for chain_index, weights in enumerate(parts):
        nodes = []
        for index, weight in enumerate(weights):
            name, virtual = f"n-{chain_index}-{index}", index == 2
            nodes.append(
                Node(
                    name,
                    None if virtual else name,
                    None if virtual else name,
                    None if virtual else periods[0],
                    Decimal(weight),
                    None,
                    None,
                    None,
                    None,
                    "",
                    roles[index],
                    {},
                    VirtualLineage("prototype", VirtualPurpose.WEIGHT_FILL, None, chain_index + 1)
                    if virtual
                    else None,
                )
            )
        chains.append(Chain(f"c-{chain_index}", tuple(nodes), periods[0]))
    return SchedulePlan(tuple(chains))


# This is a new product metric with explicit expected values, not reference numeric equivalence.
cases = (
    ((("700",),), "1200", "500"),
    ((("1200",),), "1200", "0"),
    ((("1600",),), "1200", "0"),
    ((("700",), ("1600",)), "1200", "500"),
    ((("600", "100", "50"),), "1200", "450"),
    ((("1199.9999999",),), "1200", "0.0000001"),
    ((("700",),), "0", "0"),
)
context = RuleEvaluationContext(periods, dict(zip(periods, range(len(periods)))), ("prototype",))
for parts, target, expected in cases:
    plan = make_plan(parts)
    rule = FutureFillWeightTargetRule(
        rule_id, "未来填充目标", RuleScope.PLAN, True, "1", {rule_id: Decimal(target)}
    )
    contribution = rule.evaluate(PlanRuleSubject("plan", plan, None), context)
    assert contribution.violations == ()
    assert tuple((item.metric_key, item.value) for item in contribution.metrics) == (
        ("future_fill_total_gap", Decimal(expected)),
    )
    assert plan == make_plan(parts)
disabled = replace(rule, enabled=False).evaluate(PlanRuleSubject("plan", plan, None), context)
assert disabled.violations == disabled.metrics == ()
assert tuple(sha256(path.read_bytes()).hexdigest() for path in paths) == before_hashes
assert sha256(source.read_bytes()).hexdigest() == source_hash
print(
    json.dumps(
        {
            "status": "pass",
            "reference_evaluate_plan_calls": reference_calls,
            "reference_configuration_variants": len(variants),
            "reference_plan_samples": 3,
            "reference_result_and_seven_level_key_unchanged": True,
            "reference_target_gap_metric_absent": True,
            "new_metric_cases": len(cases),
            "disabled_target_cases": 1,
            "new_metric_is_product_addition_not_reference_equivalence": True,
            "first_unexpected_difference": None,
            "reference_and_frozen_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "scope": "small rule audit only; no search or quality-gate acceptance",
        },
        ensure_ascii=False,
    )
)
