"""Optional reference comparison with explicitly separated, approved differences."""

import json
import math
import runpy
import sys
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.core.contracts import ControlledSplitMode, RuleScope
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import ChainRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import ConsecutiveReverseWidthRule

source = Path(sys.argv[1])
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_consecutive_width_differential")
D = Decimal
EPSILON = 1e-9
rule_id = "forbid_consecutive_reverse_width"
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
rule = ConsecutiveReverseWidthRule(rule_id, "连续逆宽", RuleScope.CHAIN, True, "1", {})
disabled_rule = replace(rule, enabled=False)


def reference_rules(enabled=True, allow=False):
    return reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [
                    {
                        "rule_id": rule_id,
                        "enabled": enabled,
                        "params": {"allow_consecutive_reverse_width": allow},
                    }
                ]
            }
        },
        {"allow_consecutive_reverse_width": allow, "reverse_width_carrier_grades": ["SPHC"]},
        {"period_order": ["period"]},
    )


def evaluate(widths, roles=None, grade="SPHC", enabled=True, allow=False, split=False):
    roles = roles or (MaterialRole.NORMAL_REAL,) * len(widths)
    old_nodes, new_nodes = [], []
    for index, (width, role) in enumerate(zip(widths, roles)):
        virtual = role is MaterialRole.GENERATED_VIRTUAL
        piece = split and index > 0
        order_id = "parent-order" if piece else f"o-{index}"
        resource_id = "parent-resource" if piece else f"r-{index}"
        old_nodes.append(
            replace(
                reference["_sentinel_node"](),
                node_id=f"n-{index}",
                source_order_id=order_id,
                source_period="period",
                weight=D(1),
                width=None if width is None else float(width),
                hot_roll_grade=grade,
                actual_transition_material=role is MaterialRole.ACTUAL_TRANSITION,
                node_type="virtual_sphc" if virtual else "real",
                split_piece_index=index if piece else 0,
                split_piece_count=2 if piece else 1,
                split_authorized=piece,
            )
        )
        new_nodes.append(
            Node(
                node_id=f"n-{index}",
                source_order_id=None if virtual else order_id,
                source_resource_id=None if virtual else resource_id,
                source_period=None if virtual else "period",
                weight=D(1),
                width=None if width is None else D(str(width)),
                thickness=None,
                min_temperature=None,
                max_temperature=None,
                grade="",
                material_role=role,
                rule_attributes={"hot_roll_grade": grade},
                virtual_lineage=VirtualLineage(
                    "prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1
                )
                if virtual
                else None,
                split_lineage=SplitLineage(
                    "partition",
                    "parent",
                    order_id,
                    resource_id,
                    "period",
                    "period",
                    ControlledSplitMode.SAME_PERIOD_SPLIT,
                    "period",
                    1,
                    D(2),
                    index,
                    2,
                    "split-rule",
                    "1",
                    "decision",
                    "same_period_split",
                )
                if piece
                else None,
            )
        )
    old = reference["evaluate_chain"](
        reference["Chain"](old_nodes, "period"), 0, reference_rules(enabled, allow), "C"
    )
    new = (rule if enabled else disabled_rule).evaluate(
        ChainRuleSubject("C", Chain("C", tuple(new_nodes), "period")), context
    )
    # A local triple oracle is independent of the implementation's edge-state scan.
    expected_positions = (
        tuple(
            index
            for index in range(2, len(widths))
            if all(width is not None for width in widths[index - 2 : index + 1])
            and float(widths[index - 1]) > float(widths[index - 2]) + EPSILON
            and float(widths[index]) > float(widths[index - 1]) + EPSILON
        )
        if enabled
        else ()
    )
    assert tuple(
        (
            item.subject_id,
            item.rule_id,
            item.scope,
            item.reason_code,
            item.disposition,
            item.severity,
        )
        for item in new.violations
    ) == tuple(
        (
            f"C:{rule_id}:{index - 1}-{index}",
            rule_id,
            RuleScope.CHAIN,
            "consecutive_reverse_width",
            RuleDisposition.PROHIBITED,
            D(1),
        )
        for index in expected_positions
    ), (widths, roles, enabled, new)
    assert tuple((item.metric_key, item.value) for item in new.metrics) == (
        (("consecutive_reverse_width_violation_count", len(expected_positions)),) if enabled else ()
    )
    # Never suppress unrelated reference failures: this isolated configuration may
    # emit ONLY consecutive-width or carrier failures, both with their old identity.
    old_positions, carrier_positions = [], []
    for item in old.violations:
        assert item["rule_id"] == "reverse_width_limit" and item["scope"] == "chain_edge"
        assert item["prohibited"] and item["severity_value"] == 1.0
        left, right = item["positions"]
        assert left == right - 1
        if item["subject_id"] == f"C:consecutive_reverse:{left}-{right}":
            assert item["reason"] == "consecutive reverse-width transitions are disabled"
            old_positions.append(right)
        else:
            assert item["subject_id"] == f"C:reverse_carrier:{right}"
            assert item["reason"] == f"reverse-width carrier grade {grade!r} is not allowed"
            carrier_positions.append(right)
    assert old.summary["reverse_width_violation_count"] == len(old.violations)
    return tuple(old_positions), expected_positions, tuple(carrier_positions), old.summary


counts = {"equivalent": 0, "adjacent_definition_difference": 0}
first_differences = {}


def compare_definition(widths, roles=None, split=False):
    old, new, carriers, summary = evaluate(widths, roles, split=split)
    assert carriers == ()
    category = "equivalent" if old == new else "adjacent_definition_difference"
    counts[category] += 1
    if old != new and category not in first_differences:
        first_differences[category] = {
            "widths": [None if width is None else str(width) for width in widths],
            "reference_current_edge_positions": old,
            "target_current_edge_positions": new,
            "reference_anchored_reverse_width_count": summary["reverse_width_count"],
        }
    return old, new, summary


width_values = (None, D("1000"), D("1005"), D("1010"), D("1015"))
exhaustive_cases = 0
for length in range(1, 6):
    for widths in product(width_values, repeat=length):
        compare_definition(widths)
        exhaustive_cases += 1

role_cases = 0
skipped_all_virtual = 0
role_widths = (
    (1000, 1010, 1020),
    (1000, 1010, 1005),
    (1000, 1010, 1010),
    (1000, None, 1020),
)
for roles, widths in product(product(tuple(MaterialRole), repeat=3), role_widths):
    if all(role is MaterialRole.GENERATED_VIRTUAL for role in roles):
        skipped_all_virtual += 1
        continue
    compare_definition(widths, roles)
    role_cases += 1

boundary_cases = 0
for start in (1.0, 1000.0, 1e16):
    boundary = start + EPSILON
    for middle in (
        math.nextafter(boundary, -math.inf),
        boundary,
        math.nextafter(boundary, math.inf),
    ):
        next_boundary = middle + EPSILON
        for end in (next_boundary, math.nextafter(next_boundary, math.inf)):
            compare_definition((start, middle, end))
            boundary_cases += 1

# Genuine same-source split pieces: equal widths do not constitute adjacent increases.
split_cases = 0
for widths in ((1000, 1010, 1010), (1020, 1010, 1010)):
    compare_definition(widths, split=True)
    split_cases += 1

# Isolate disabling from the definition change: both definitions flag the same edges.
disabled_cases = 0
for widths in ((1000, 1010, 1020), (1000, 1010, 1020, 1030)):
    before, target_enabled, _, _ = evaluate(widths)
    assert before == target_enabled and before
    old, new, carriers, _ = evaluate(widths, enabled=False)
    assert old == before and new == carriers == ()
    first_differences.setdefault(
        "disabled_rule_difference",
        {
            "widths": widths,
            "reference_current_edge_positions": old,
            "target_current_edge_positions": new,
            "target_metrics": [],
        },
    )
    disabled_cases += 1

# Isolate carrier removal: the context permits consecutive widths, and these cases
# contain no consecutive adjacent increases. Do not label this as an enable-flag fix.
carrier_cases = 0
for role in MaterialRole:
    for widths in ((1000, 1010), (1000, 1010, 1005)):
        roles = (MaterialRole.NORMAL_REAL,) + (role,) * (len(widths) - 1)
        for enabled in (True, False):
            old, new, carriers, _ = evaluate(widths, roles, "DC01", enabled, allow=True)
            assert old == new == ()
            expected = (
                () if role is MaterialRole.GENERATED_VIRTUAL else tuple(range(1, len(widths)))
            )
            assert carriers == expected
            if carriers:
                first_differences.setdefault(
                    "carrier_filter_removal",
                    {
                        "widths": widths,
                        "grade": "DC01",
                        "rule_enabled": enabled,
                        "reference_carrier_positions": carriers,
                        "target_violation_count": 0,
                    },
                )
            carrier_cases += 1

# This is a reference observation for later function 5.12, not a new count-rule test.
old, new, _, summary = evaluate((1000, 1010, 1005))
assert old == (2,) and new == () and summary["reverse_width_count"] == 2
assert set(first_differences) == {
    "adjacent_definition_difference",
    "disabled_rule_difference",
    "carrier_filter_removal",
}
print(
    json.dumps(
        {
            "status": "pass",
            "definition_comparisons": sum(counts.values()),
            "definition_partitions": counts,
            "exhaustive_width_cases": exhaustive_cases,
            "role_cases": role_cases,
            "skipped_all_virtual": skipped_all_virtual,
            "boundary_cases": boundary_cases,
            "split_cases": split_cases,
            "disabled_cases": disabled_cases,
            "carrier_cases": carrier_cases,
            "first_unexpected_difference": None,
            "first_expected_differences": first_differences,
            "scope_separation_example": {
                "reference_anchored_count": 2,
                "target_consecutive_count": 0,
            },
            "scope": "isolated chain rule, not full GQGA4 scheduling or future count/edge rules",
            "reference_sha256": source_hash,
        },
        ensure_ascii=False,
    )
)
