"""Optional local reference check; never imported by production or portable tests."""

import json
import runpy
import sys
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import permutations, product
from pathlib import Path

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import Chain, MaterialRole, Node, VirtualLineage, VirtualPurpose
from apsgo_scheduler.core.rules.base import ChainRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import SameSpecContinuousRealWeightRule

source = Path(sys.argv[1])
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_same_spec_weight_differential")
rule_id = "chain_same_spec_real_weight_lte"
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
D = Decimal
default_fields = ("thickness", "width", "grade")
# raw grade, width, thickness, weight, material role
choices = (
    ("DX51D", D("1200"), D("1"), D("600"), "real"),
    ("DX51D", D("1300"), D("1"), D("450"), "real"),
    ("DX51D", D("1200"), D("2"), D("600"), "real"),
    ("OTHER", D("1200"), D("1"), D("550"), "real"),
    ("DX51D", None, None, D("600"), "real"),
    ("DX51D", D("1200"), D("1"), D("600"), "actual"),
    ("DX51D", D("1200"), D("1"), D("600"), "virtual"),
)


def compare(sequence, limit, fields=default_fields):
    old_nodes, new_nodes = [], []
    for index, (grade, width, thickness, weight, role) in enumerate(sequence):
        virtual = role == "virtual"
        actual = role == "actual"
        old_nodes.append(
            replace(
                reference["_sentinel_node"](),
                node_id=f"n-{index}",
                grade=reference["normalize_grade"](grade),
                width=None if width is None else float(width),
                thickness=None if thickness is None else float(thickness),
                weight=weight,
                actual_transition_material=actual,
                node_type="virtual_sphc" if virtual else "real",
            )
        )
        new_nodes.append(
            Node(
                node_id=f"n-{index}",
                source_order_id=None if virtual else f"o-{index}",
                source_resource_id=None if virtual else f"r-{index}",
                source_period=None if virtual else "period",
                weight=weight,
                width=width,
                thickness=thickness,
                min_temperature=None,
                max_temperature=None,
                grade=grade,
                material_role=MaterialRole.GENERATED_VIRTUAL
                if virtual
                else MaterialRole.ACTUAL_TRANSITION
                if actual
                else MaterialRole.NORMAL_REAL,
                rule_attributes={},
                virtual_lineage=VirtualLineage(
                    "prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1
                )
                if virtual
                else None,
            )
        )
    old, old_maximum, _ = reference["_segment_violations"](
        "C",
        old_nodes,
        rule_id,
        lambda node: not node.is_virtual and not node.actual_transition_material,
        lambda node: node.weight,
        limit,
        "same-spec real run weight",
        group_key=lambda node: tuple(getattr(node, field) for field in fields),
    )
    rule = SameSpecContinuousRealWeightRule(
        rule_id=rule_id,
        name="同规格连续真实重量",
        scope=RuleScope.CHAIN,
        enabled=True,
        version="1",
        parameters={"group_by_fields": fields, "max_real_weight": limit},
    )
    result = rule.evaluate(ChainRuleSubject("C", Chain("C", tuple(new_nodes), "period")), context)
    expected = (
        tuple((item["subject_id"], Decimal(str(item["severity_value"]))) for item in old),
        old_maximum,
    )
    observed = (
        tuple((item.subject_id, item.severity) for item in result.violations),
        result.metrics[0].value,
    )
    assert (
        len(result.metrics) == 1 and result.metrics[0].metric_key == "max_same_spec_real_run_weight"
    )
    assert all(
        item.reason_code == "same_spec_run_weight"
        and item.disposition is RuleDisposition.PROHIBITED
        for item in result.violations
    )
    assert observed == expected, (sequence, limit, fields, expected, observed)


field_orders = tuple(
    order
    for length in range(1, len(default_fields) + 1)
    for order in permutations(default_fields, length)
)
exhaustive_count = skipped = boundary_count = 0
for length in range(1, 4):
    for sequence in product(choices, repeat=length):
        for fields in field_orders:
            for limit in (D("0"), D("1000")):
                if all(item[-1] == "virtual" for item in sequence):
                    skipped += 1
                    continue
                compare(sequence, limit, fields)
                exhaustive_count += 1
for weights in (
    ("1000",),
    ("1000.000001",),
    ("1000.0000011",),
    ("500", "500"),
    ("500", "500.000001"),
    ("500", "500.0000011"),
):
    compare(tuple(("DX51D", D("1200"), D("1"), D(weight), "real") for weight in weights), D("1000"))
    boundary_count += 1
for weight in ("0.0000009", "0.000001", "0.0000011"):
    compare((("DX51D", D("1200"), D("1"), D(weight), "real"),), D("0"))
    boundary_count += 1
base = choices[0]
for grade, width, thickness in (
    ("DX51D", D("1200.00000000000001"), D("1")),
    ("DX51D", D("1200.0000000000002"), D("1")),
    ("DX51D", D("1200"), D("1.00000000000000001")),
    ("DX51D", D("1200"), D("1.0000000000000002")),
    (" dx51d ", D("1200"), D("1")),
    ("DX51D+Z", D("1200"), D("1")),
    ("DX51D", None, D("1")),
    ("DX51D", D("1200"), None),
):
    compare((base, (grade, width, thickness, D("600"), "real")), D("1000"))
    boundary_count += 1
compare((choices[4], choices[4]), D("1000"))
boundary_count += 1
compare((base, base, choices[3], choices[3]), D("1000"))
boundary_count += 1
compare((base,) * 7, D("1000"))
boundary_count += 1
print(
    json.dumps(
        {
            "status": "pass",
            "compared_cases": exhaustive_count + boundary_count,
            "exhaustive_cases": exhaustive_count,
            "boundary_cases": boundary_count,
            "group_field_orders": len(field_orders),
            "skipped_all_virtual_cases": skipped,
            "first_difference": None,
            "reference_sha256": source_hash,
        }
    )
)
