"""Optional retained-baseline count comparison; no full search or external inventory."""

import argparse
import json
import math
import runpy
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import Chain, MaterialRole, Node, VirtualLineage, VirtualPurpose
from apsgo_scheduler.core.rules.base import ChainRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import ReverseWidthCountRule

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", required=True, type=Path)
source = parser.parse_args().reference
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_reverse_width_count_differential")
inputs = Path(__file__).resolve().parents[6] / "tests/baselines/gqga4/inputs"
paths = (inputs / "resolved_rules.json", inputs / "rule_context.json")
before_hashes = tuple(sha256(path.read_bytes()).hexdigest() for path in paths)
resolved, rule_context = (json.loads(path.read_text(encoding="utf-8")) for path in paths)
rule_id, metric = "max_reverse_width_count", "reverse_width_count"
record = next(item for item in resolved["rule_snapshot"]["dsl_rules"] if item["rule_id"] == rule_id)
assert record["enabled"] and record["params"] == {"max_count": 2}
assert record["reason"]["code"] == "reverse_width"
assert rule_context == resolved["rule_context"] and rule_context[rule_id] == 2
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
normal, actual, virtual = tuple(MaterialRole)
roles_catalog = (normal, actual, virtual)
limits = (0, 1, 2, 5)
counts = {
    "enabled_equivalent": 0,
    "disabled_metric_omission": 0,
    "overflow_projection_protection": 0,
}
first_expected_differences = {}


def compare(widths, limit, roles=None, enabled=True, overflow_index=None, expected_count=None):
    roles = roles or tuple(
        roles_catalog[index % len(roles_catalog)] for index in range(len(widths))
    )
    label = f"{widths}:{limit}:{tuple(role.value for role in roles)}:{enabled}"
    old_nodes, new_nodes = [], []
    for index, (width, role) in enumerate(zip(widths, roles)):
        name = f"n-{index}"
        is_virtual = role is virtual
        old_nodes.append(
            replace(
                reference["_sentinel_node"](),
                node_id=name,
                source_order_id=name,
                source_period="period",
                weight=Decimal(1),
                width=reference["optional_float"](width),
                hot_roll_grade="NON_CARRIER",
                actual_transition_material=role is actual,
                node_type="virtual_sphc" if is_virtual else "real",
            )
        )
        new_nodes.append(
            Node(
                name,
                None if is_virtual else name,
                None if is_virtual else name,
                None if is_virtual else "period",
                Decimal(1),
                width,
                None,
                None,
                None,
                "OTHER",
                role,
                {"hot_roll_grade": "NON_CARRIER"},
                VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1)
                if is_virtual
                else None,
            )
        )
    book = reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [dict(record, enabled=enabled, params={"max_count": limit})]
            }
        },
        dict(
            rule_context,
            max_reverse_width_count=limit,
            allow_consecutive_reverse_width=True,
            reverse_width_carrier_grades=["*"],
        ),
        {"period_order": ["period"]},
    )
    old_chain = reference["Chain"](old_nodes, "period")
    before_chain = (tuple(old_chain.nodes), old_chain.assigned_period)
    old = reference["evaluate_chain"](old_chain, 0, book, "C")
    assert (tuple(old_chain.nodes), old_chain.assigned_period) == before_chain, label
    assert all(item["rule_id"] == rule_id for item in old.violations), (label, old.violations)
    for item in old.violations:
        assert item["scope"] == "chain" and item["prohibited"] and item["subject_id"] == "C"
        assert item["positions"] == [0, len(widths) - 1]
    if expected_count is not None:
        assert old.summary[metric] == expected_count, label
    rule = ReverseWidthCountRule(
        rule_id, "链内逆宽次数", RuleScope.CHAIN, enabled, "1", {"max_count": limit}
    )
    subject = ChainRuleSubject("C", Chain("C", tuple(new_nodes), "period"))
    if overflow_index is not None and enabled:
        assert old_nodes[overflow_index].width is None
        try:
            rule.evaluate(subject, context)
        except ValueError as error:
            assert f"n-{overflow_index}.width" in str(error) and "finite float" in str(error)
        else:
            raise AssertionError((label, "overflow projection must be rejected"))
        counts["overflow_projection_protection"] += 1
        first_expected_differences.setdefault(
            "overflow_projection",
            {
                "case": label,
                "reference_width_after_optional_float": None,
                "reference_count": old.summary[metric],
                "target": f"ValueError at n-{overflow_index}.width",
                "boundary": "reference optional_float plus evaluate_chain, not full input validation",
            },
        )
        return
    new = rule.evaluate(subject, context)
    for violation in new.violations:
        assert violation.rule_id == rule_id and violation.scope is RuleScope.CHAIN
        assert violation.subject_id == "C" and violation.reason_code == "reverse_width"
        assert violation.disposition is RuleDisposition.PROHIBITED
    assert tuple(item.severity for item in new.violations) == tuple(
        Decimal(str(item["severity_value"])) for item in old.violations
    ), (label, old.violations, new.violations)
    if enabled:
        assert tuple((item.metric_key, item.value) for item in new.metrics) == (
            (metric, old.summary[metric]),
        ), label
        counts["enabled_equivalent"] += 1
    else:
        assert not old.violations and not new.violations and not new.metrics, label
        counts["disabled_metric_omission"] += 1
        first_expected_differences.setdefault(
            "disabled_metric",
            {
                "case": label,
                "reference_count": old.summary[metric],
                "reference_count_ok": old.summary["reverse_width_count_ok"],
                "target_metrics": [],
            },
        )


# Short width sequences exercise retained baseline, plateaus, decreases and null resets.
width_values = (None, Decimal(990), Decimal(1000), Decimal(1005), Decimal(1010))
for length in range(1, 5):
    for widths in product(width_values, repeat=length):
        for limit in limits:
            compare(widths, limit)

goldens = (
    ((1000, 1010, 1005), 2),
    ((1000, 1010, 1010), 2),
    ((1000, 1010, 990), 1),
    ((1000, None, 1010), 0),
    ((None, 1000, 1010), 1),
)
for roles in product(roles_catalog, repeat=3):
    if roles != (virtual,) * 3:
        for (raw_widths, expected), limit in product(goldens, limits):
            compare(
                tuple(None if value is None else Decimal(value) for value in raw_widths),
                limit,
                roles=roles,
                expected_count=expected,
            )

# Use adjacent IEEE float values around baseline+epsilon, including the 1e16 precision trap.
for base in (Decimal("1e-9"), Decimal(1), Decimal(1000), Decimal("1e16")):
    threshold = float(base) + 1e-9
    for value, limit in product(
        (math.nextafter(threshold, -math.inf), threshold, math.nextafter(threshold, math.inf)),
        limits,
    ):
        width = Decimal.from_float(value)
        for widths in (
            (base, width),
            (base, width, width),
            (width, base, width),
            (base, None, width),
            (base, width, None, base, width),
        ):
            compare(widths, limit)
for widths, limit in product(
    (
        (Decimal("1e-1000"), Decimal("2e-1000"), Decimal("1e-8")),
        (Decimal("1e16"), Decimal("10000000000000001"), Decimal("10000000000000002")),
    ),
    limits,
):
    compare(widths, limit)

for (raw_widths, _), limit in product(goldens[:3], limits):
    compare(tuple(Decimal(value) for value in raw_widths), limit, enabled=False)

# A finite Decimal may overflow float. Compare against the reference's actual field converter,
# then isolate the target's defensive rejection; do not claim reference full validation accepts it.
for index, role in product(range(3), roles_catalog):
    widths, roles = [Decimal(1000), Decimal(1010), Decimal(1005)], [normal] * 3
    widths[index], roles[index] = Decimal("1e1000"), role
    for enabled in (True, False):
        compare(tuple(widths), 2, roles=tuple(roles), enabled=enabled, overflow_index=index)

assert tuple(sha256(path.read_bytes()).hexdigest() for path in paths) == before_hashes
assert sha256(source.read_bytes()).hexdigest() == source_hash
print(
    json.dumps(
        {
            "status": "pass",
            "counts": counts,
            "evaluate_chain_calls": sum(counts.values()),
            "disabled_overflow_skip_cases": 9,
            "first_expected_differences": first_expected_differences,
            "first_unexpected_difference": None,
            "reference_and_frozen_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "scope": "isolated actual evaluate_chain; no carrier/consecutive checks, full search, V3 or inventory",
        },
        ensure_ascii=False,
    )
)
