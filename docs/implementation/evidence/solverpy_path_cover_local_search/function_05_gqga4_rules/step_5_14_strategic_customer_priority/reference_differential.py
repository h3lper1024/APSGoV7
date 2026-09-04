"""Optional customer-name projection comparison, not complete input or solver acceptance."""

import argparse
import json
import runpy
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import MaterialRole, Node, VirtualLineage, VirtualPurpose
from apsgo_scheduler.core.rules.base import NodeRuleSubject, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import StrategicCustomerPriorityRule

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", required=True, type=Path)
source = parser.parse_args().reference
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_strategic_priority_differential")
inputs = Path(__file__).resolve().parents[6] / "tests/baselines/gqga4/inputs"
paths = tuple(
    inputs / name
    for name in ("resolved_rules.json", "rule_context.json", "optimization_problem.json")
)
before_hashes = tuple(sha256(path.read_bytes()).hexdigest() for path in paths)
resolved, rule_context, encoded_problem = (
    json.loads(path.read_text(encoding="utf-8")) for path in paths
)
problem = reference["decode_contract"](encoded_problem)["optimization_problem"]
rule_id = "strategic_customer_priority_objective"
record = next(item for item in resolved["rule_snapshot"]["dsl_rules"] if item["rule_id"] == rule_id)
assert record["enabled"] and record["params"] == {
    "contains_any": ["上汽", "吉利", "宝马"],
    "rank": 0,
    "default_rank": 1,
    "field": "node.customer_name",
}
assert rule_context == resolved["rule_context"]
params = {key: value for key, value in record["params"].items() if key != "field"}
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
missing = object()
counts = {
    "enabled_equivalent": 0,
    "disabled_neutral_representation": 0,
    "custom_rank_honored": 0,
    "nontext_name_protection": 0,
    "target_virtual_contract": 0,
}
first_expected_differences = {}
normalize_calls = 0


def target_node(attributes, role, identity="node"):
    virtual = role is MaterialRole.GENERATED_VIRTUAL
    return Node(
        identity,
        None if virtual else identity,
        None if virtual else identity,
        None if virtual else "period",
        Decimal(1),
        None,
        None,
        None,
        None,
        "",
        role,
        attributes,
        VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, 1) if virtual else None,
    )


def raw_node(name=missing, actual=False):
    features = {"actual_transition_material": actual, "customer_grade": "宝马战略客户"}
    if name is not missing:
        features["customer_name"] = name
    return {
        "node_id": "synthetic",
        "source_resource_id": "order",
        "planning_period_id": "period",
        "material_role": "actual_transition" if actual else "normal_real",
        "measures": {"values": [{"metric_key": "weight", "value": "1"}]},
        "physical_spec": {"width": {"millimeters": "1000"}, "thickness": {"millimeters": "1"}},
        "classifications": {"soft_or_hard": "soft", "hot_rolled_grade": "SPHC"},
        "rule_features": [{"key": key, "value": value} for key, value in features.items()],
    }


def compare(raw_nodes, parameters=params, enabled=True):
    global normalize_calls
    book = reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [
                    dict(
                        record, enabled=enabled, params=dict(parameters, field="node.customer_name")
                    )
                ]
            }
        },
        rule_context,
        {"period_order": ["period"]},
    )
    before = json.dumps(raw_nodes, sort_keys=True, default=str)
    old_nodes = reference["normalize_nodes"]({"nodes": raw_nodes}, book)
    normalize_calls += 1
    assert json.dumps(raw_nodes, sort_keys=True, default=str) == before
    assert len(old_nodes) == len(raw_nodes)
    rule = StrategicCustomerPriorityRule(
        rule_id, "战略客户构造优先级", RuleScope.NODE, enabled, "1", parameters
    )
    for raw, old in zip(raw_nodes, old_nodes):
        attributes = reference["feature_map"](raw)
        role = (
            MaterialRole.ACTUAL_TRANSITION
            if old.actual_transition_material
            else MaterialRole.NORMAL_REAL
        )
        node = target_node(
            {
                key: attributes[key]
                for key in ("customer_name", "customer_grade")
                if key in attributes
            },
            role,
            old.node_id,
        )
        name = attributes.get("customer_name")
        subject = NodeRuleSubject(node.node_id, node)
        if enabled and name is not None and not isinstance(name, str):
            for operation in (
                lambda: rule.construction_priority(node),
                lambda: rule.evaluate(subject, context),
            ):
                try:
                    operation()
                except ValueError as error:
                    assert "customer_name" in str(error)
                else:
                    raise AssertionError("nontext customer_name must be rejected")
            counts["nontext_name_protection"] += 1
            first_expected_differences.setdefault(
                "nontext_name",
                {
                    "input_type": type(name).__name__,
                    "reference_normalized_name": old.customer_name,
                    "target": "ValueError for customer_name",
                },
            )
            continue
        priority = rule.construction_priority(node)
        result = rule.evaluate(subject, context)
        assert result.violations == ()
        if not enabled:
            assert book.strategic_keywords == () and old.strategic_rank == 1
            assert priority == () and result.metrics == ()
            counts["disabled_neutral_representation"] += 1
            first_expected_differences.setdefault(
                "disabled",
                {
                    "reference_rank": 1,
                    "target_priority": [],
                    "target_metrics": [],
                    "meaning": "both neutral; actual reference parser removes keywords before normalization",
                },
            )
            continue
        expected = parameters["rank" if old.strategic_rank == 0 else "default_rank"]
        assert priority == (expected,)
        assert tuple((item.metric_key, item.value) for item in result.metrics) == (
            ("strategic_customer_rank", expected),
        )
        if expected == old.strategic_rank:
            counts["enabled_equivalent"] += 1
        else:
            counts["custom_rank_honored"] += 1
            first_expected_differences.setdefault(
                "custom_rank",
                {
                    "customer_name": old.customer_name,
                    "reference_rank": old.strategic_rank,
                    "target_rank": expected,
                    "parameters": parameters,
                    "reason": "reference hardcodes 0/1; target uses configured signed integer ranks",
                },
            )
    return old_nodes


# Real frozen input passes through the actual reference normalizer; only customer fields are
# projected into target leaf-rule nodes. This does not claim the new input adapter exists.
frozen_nodes = compare(problem["nodes"])
assert len(frozen_nodes) == 531 and sum(node.strategic_rank == 0 for node in frozen_nodes) == 58
compare(problem["nodes"], enabled=False)

names = (missing, None, "", "   ", "普通客户", "上汽", " 吉利供应商 ", "宝马公司", "bmw", "BMW")
synthetic = [raw_node(name, actual) for name, actual in product(names, (False, True))]
for keywords in (params["contains_any"], [], ["BMW"], [" 宝马 "], ["吉利", "吉利"]):
    compare(synthetic, dict(params, contains_any=keywords))
for rank, default in ((-3, 8), (8, -3), (7, 7)):
    compare(synthetic, dict(params, rank=rank, default_rank=default))
compare(synthetic, enabled=False)
invalid_names = [raw_node(name) for name in (123, False, Decimal("1.5"))]
compare(invalid_names)
compare(invalid_names, enabled=False)

# Generated virtual nodes are not supported raw orders for reference normalize_nodes.
# Verify target role-independent attribute semantics separately, without fabricating a reference path.
for attributes, expected in (
    ({}, 1),
    ({"customer_name": "宝马"}, 0),
    ({"customer_name": None, "customer_grade": "宝马"}, 1),
):
    node = target_node(attributes, MaterialRole.GENERATED_VIRTUAL)
    for enabled in (True, False):
        rule = StrategicCustomerPriorityRule(
            rule_id, "战略客户构造优先级", RuleScope.NODE, enabled, "1", params
        )
        result = rule.evaluate(NodeRuleSubject("virtual", node), context)
        assert result.violations == ()
        assert rule.construction_priority(node) == ((expected,) if enabled else ())
        assert tuple((item.metric_key, item.value) for item in result.metrics) == (
            (("strategic_customer_rank", expected),) if enabled else ()
        )
        counts["target_virtual_contract"] += 1

assert tuple(sha256(path.read_bytes()).hexdigest() for path in paths) == before_hashes
assert sha256(source.read_bytes()).hexdigest() == source_hash
print(
    json.dumps(
        {
            "status": "pass",
            "counts": counts,
            "normalize_nodes_calls": normalize_calls,
            "frozen_input": {
                "orders": 531,
                "matched_customer_names": 58,
                "default_rank_orders": 473,
            },
            "first_expected_differences": first_expected_differences,
            "first_unexpected_difference": None,
            "reference_and_frozen_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "scope": "customer field projection only; no complete input adapter, full search, V3 or inventory",
        },
        ensure_ascii=False,
    )
)
